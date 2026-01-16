"""Core LocalSandbox implementation."""

import asyncio
import atexit
import base64
import json
import re
import subprocess
import tempfile
import time
import weakref
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from localsandbox.exceptions import (
    CommandError,
    ExecutionLimitError,
    FileNotFoundError,
    PermissionError,
    SubprocessCrashed,
)


def _get_shim_path() -> Path:
    """Get the path to the TypeScript shim CLI (runs via Deno)."""
    package_dir = Path(__file__).parent.parent
    shim_path = package_dir / "shim" / "src" / "cli.ts"
    if not shim_path.exists():
        raise RuntimeError(f"Shim not found at {shim_path}.")
    return shim_path


class ExecutionPreset(Enum):
    """Execution limits presets for DOS protection."""

    STRICT = "strict"  # 100 loop iterations, 500 commands max
    NORMAL = "normal"  # 1,000 loop iterations, 5,000 commands max
    PERMISSIVE = "permissive"  # 10,000 loop iterations, 50,000 commands max


# Preset limit values
_PRESET_LIMITS: dict[ExecutionPreset, dict[str, int]] = {
    ExecutionPreset.STRICT: {
        "maxLoopIterations": 100,
        "maxCommandCount": 500,
    },
    ExecutionPreset.NORMAL: {
        "maxLoopIterations": 1000,
        "maxCommandCount": 5000,
    },
    ExecutionPreset.PERMISSIVE: {
        "maxLoopIterations": 10000,
        "maxCommandCount": 50000,
    },
}

# Global registry of active LocalSandbox instances for atexit cleanup
# Uses weak references so instances can be garbage collected normally
_active_instances: weakref.WeakSet["LocalSandbox"] = weakref.WeakSet()
_atexit_registered = False


def _cleanup_all_instances() -> None:
    """Clean up all active LocalSandbox instances at process exit."""
    for instance in list(_active_instances):
        try:
            instance.destroy()
        except Exception:
            pass  # Ignore errors during cleanup


def _register_atexit() -> None:
    """Register the atexit cleanup handler once."""
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(_cleanup_all_instances)
        _atexit_registered = True


@dataclass
class BashResult:
    """Result of a bash command execution."""

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float


@dataclass
class PythonResult:
    """Result of a Python code execution."""

    stdout: str
    stderr: str
    exit_code: int
    error: str | None = None


@dataclass
class HistoryEntry:
    """A recorded bash command execution."""

    id: int
    name: str
    started_at: int
    completed_at: int
    parameters: dict[str, str | int] | None
    result: dict[str, int] | None


class KVStore:
    """
    Key-value store for persisting agent state.

    All values are stored as strings. This store is separate from the
    filesystem and persists in the same SQLite database.
    """

    def __init__(self, sandbox: "LocalSandbox") -> None:
        self._sandbox = sandbox

    def _check_destroyed(self) -> None:
        if self._sandbox._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

    def get(self, key: str) -> str | None:
        """
        Get a value by key.

        Args:
            key: The key to look up.

        Returns:
            The value as a string, or None if not found.

        Raises:
            RuntimeError: If the sandbox has been destroyed.
        """
        self._check_destroyed()

        result = self._sandbox._run_shim(
            "kv-get",
            {"db": str(self._sandbox._db_path), "key": key},
        )

        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            if result.returncode != 0:
                raise SubprocessCrashed(f"KV get failed: {result.stderr}")
            raise SubprocessCrashed(f"Failed to parse output: {result.stdout[:500]}")

        return output.get("value")

    def set(self, key: str, value: str) -> None:
        """
        Set a value by key.

        Args:
            key: The key to set.
            value: The string value to store.

        Raises:
            RuntimeError: If the sandbox has been destroyed.
        """
        self._check_destroyed()

        result = self._sandbox._run_shim(
            "kv-set",
            {"db": str(self._sandbox._db_path), "key": key, "value": value},
        )

        if result.returncode != 0:
            raise SubprocessCrashed(f"KV set failed: {result.stderr}")

    def delete(self, key: str) -> None:
        """
        Delete a key-value pair.

        Args:
            key: The key to delete.

        Raises:
            RuntimeError: If the sandbox has been destroyed.
        """
        self._check_destroyed()

        result = self._sandbox._run_shim(
            "kv-delete",
            {"db": str(self._sandbox._db_path), "key": key},
        )

        if result.returncode != 0:
            raise SubprocessCrashed(f"KV delete failed: {result.stderr}")

    def keys(self, prefix: str = "") -> list[str]:
        """
        List all keys with an optional prefix filter.

        Args:
            prefix: Optional prefix to filter keys by.

        Returns:
            List of keys matching the prefix.

        Raises:
            RuntimeError: If the sandbox has been destroyed.
        """
        self._check_destroyed()

        result = self._sandbox._run_shim(
            "kv-keys",
            {"db": str(self._sandbox._db_path), "prefix": prefix},
        )

        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            if result.returncode != 0:
                raise SubprocessCrashed(f"KV keys failed: {result.stderr}")
            raise SubprocessCrashed(f"Failed to parse output: {result.stdout[:500]}")

        return output.get("keys", [])

    # Async methods
    async def aget(self, key: str) -> str | None:
        """Async version of get()."""
        return await asyncio.to_thread(self.get, key)

    async def aset(self, key: str, value: str) -> None:
        """Async version of set()."""
        await asyncio.to_thread(self.set, key, value)

    async def adelete(self, key: str) -> None:
        """Async version of delete()."""
        await asyncio.to_thread(self.delete, key)

    async def akeys(self, prefix: str = "") -> list[str]:
        """Async version of keys()."""
        return await asyncio.to_thread(self.keys, prefix)


class LocalSandbox:
    """
    Sandboxed filesystem operations via just-bash and AgentFS.

    Each bash operation spawns the TypeScript shim CLI that opens the AgentFS
    database, executes the command via just-bash, and returns the result.
    All filesystem state persists in a SQLite database file.
    """

    def __init__(
        self,
        files: dict[str, str | Path | bytes] | None = None,
        snapshot: bytes | None = None,
        cwd: str = "/home/user",
        preset: ExecutionPreset = ExecutionPreset.NORMAL,
    ) -> None:
        """
        Create a new LocalSandbox.

        Args:
            files: Initial filesystem contents. String values are file content,
                   Path values are read and snapshotted at creation,
                   bytes are written as binary.
            snapshot: Restore from a previously exported snapshot (mutually
                      exclusive with `files`).
            cwd: Initial working directory.
            preset: Execution limits preset (STRICT, NORMAL, or PERMISSIVE).

        Raises:
            ValueError: If both `files` and `snapshot` are provided.
            RuntimeError: If the shim is not built.
        """
        if files is not None and snapshot is not None:
            raise ValueError("Cannot provide both 'files' and 'snapshot'")

        self._shim_path = _get_shim_path()
        self._cwd = cwd
        self._preset = preset
        self._limits = _PRESET_LIMITS[preset]
        self._destroyed = False

        # Create temp directory for database
        self._temp_dir = Path(tempfile.mkdtemp(prefix="localsandbox_"))
        self._db_path = self._temp_dir / "localsandbox.db"

        # Initialize KV store
        self.kv = KVStore(self)

        # Register for atexit cleanup
        _register_atexit()
        _active_instances.add(self)

        # Restore from snapshot if provided
        if snapshot is not None:
            self._db_path.write_bytes(snapshot)

        # Seed initial files if provided
        if files:
            self._seed_files(files)

    def _run_shim(
        self,
        command: str,
        args: dict[str, str | None],
        timeout: int = 60,
    ) -> subprocess.CompletedProcess[str]:
        """
        Run a shim command via Deno.

        Args:
            command: The shim command name (e.g., 'bash', 'seed', 'read-file').
            args: Dict of argument name to value. None values are skipped.
            timeout: Timeout in seconds.

        Returns:
            The subprocess CompletedProcess result.

        Raises:
            SubprocessCrashed: If the command times out.
        """
        cmd_list = [
            "deno",
            "run",
            "--allow-read",
            "--allow-write",
            "--allow-env",
            "--allow-ffi",
            "--allow-run",
        ]
        cmd_list.extend([str(self._shim_path), command])
        for key, value in args.items():
            if value is not None:
                cmd_list.extend([f"--{key}", value])

        try:
            return subprocess.run(
                cmd_list,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise SubprocessCrashed(f"{command} operation timed out: {e}")

    def _seed_files(self, files: dict[str, str | Path | bytes]) -> None:
        """Seed initial files into the sandbox."""
        # Resolve Path objects and prepare files dict
        # Format: { "path": "content" } for text, { "path": {"base64": "..."} } for binary
        resolved_files: dict[str, str | dict[str, str]] = {}
        for path, content in files.items():
            if isinstance(content, Path):
                # Check if binary by trying to read as text
                try:
                    resolved_files[path] = content.read_text()
                except UnicodeDecodeError:
                    # Binary file - read as bytes and base64 encode
                    binary_content = content.read_bytes()
                    resolved_files[path] = {
                        "base64": base64.b64encode(binary_content).decode("ascii")
                    }
            elif isinstance(content, bytes):
                # Binary content - base64 encode
                resolved_files[path] = {
                    "base64": base64.b64encode(content).decode("ascii")
                }
            else:
                resolved_files[path] = content

        files_json = json.dumps(resolved_files)

        result = self._run_shim(
            "seed",
            {"db": str(self._db_path), "files": files_json},
            timeout=60,
        )

        if result.returncode != 0:
            raise SubprocessCrashed(
                f"Failed to seed files: {result.stderr or result.stdout}"
            )

    def _parse_execution_limit_error(
        self, error_message: str
    ) -> ExecutionLimitError | None:
        """Parse execution limit errors from shim output."""
        # Match patterns like "Loop iteration limit (100) exceeded"
        # or "Command count limit (500) exceeded"
        loop_match = re.search(
            r"[Ll]oop.*(?:iteration|limit).*\((\d+)\).*exceeded", error_message
        )
        if loop_match:
            return ExecutionLimitError(
                error_message,
                limit_type="loop_iterations",
                limit_value=int(loop_match.group(1)),
            )

        cmd_match = re.search(
            r"[Cc]ommand.*(?:count|limit).*\((\d+)\).*exceeded", error_message
        )
        if cmd_match:
            return ExecutionLimitError(
                error_message,
                limit_type="command_count",
                limit_value=int(cmd_match.group(1)),
            )

        # Also check for generic limit messages
        if "limit" in error_message.lower() and "exceeded" in error_message.lower():
            return ExecutionLimitError(
                error_message,
                limit_type="unknown",
                limit_value=0,
            )

        return None

    def _parse_file_not_found(self, stderr: str) -> str | None:
        """Parse file not found errors from stderr and return the path if found."""
        # Patterns: "cmd: /path: No such file or directory"
        # or "cannot access '/path': No such file or directory"
        patterns = [
            r":\s*([^\s:]+):\s*No such file or directory",
            r"cannot (?:access|open|stat) '([^']+)'.*No such file or directory",
            r"([^\s:]+):\s*not found",
        ]
        for pattern in patterns:
            match = re.search(pattern, stderr, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _parse_permission_error(self, stderr: str) -> str | None:
        """Parse permission denied errors from stderr and return the path if found."""
        # Patterns: "cmd: /path: Permission denied"
        # or "cannot access '/path': Permission denied"
        patterns = [
            r":\s*([^\s:]+):\s*Permission denied",
            r"cannot (?:access|open|stat) '([^']+)'.*Permission denied",
        ]
        for pattern in patterns:
            match = re.search(pattern, stderr, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def bash(self, command: str) -> BashResult:
        """
        Execute a bash command in the sandbox.

        Args:
            command: The bash command to execute.

        Returns:
            BashResult with stdout, stderr, exit_code, and duration_ms.

        Raises:
            CommandError: If the command returns non-zero exit code.
            ExecutionLimitError: If execution limits are exceeded.
            SubprocessCrashed: If the Node subprocess crashes.
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        start_time = time.perf_counter()
        limits_json = json.dumps(self._limits)

        result = self._run_shim(
            "bash",
            {
                "db": str(self._db_path),
                "cwd": self._cwd,
                "command": command,
                "limits": limits_json,
            },
            timeout=120,
        )

        duration_ms = (time.perf_counter() - start_time) * 1000

        if result.returncode != 0 and not result.stdout:
            # Check if it's an execution limit error
            limit_error = self._parse_execution_limit_error(result.stderr)
            if limit_error:
                raise limit_error

            raise SubprocessCrashed(
                f"Node subprocess crashed: {result.stderr}",
                signal=result.returncode if result.returncode < 0 else None,
            )

        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise SubprocessCrashed(
                f"Failed to parse Node output: {result.stdout[:500]}"
            )

        # Check for shim-level errors
        if "error" in output:
            # Check if it's an execution limit error
            limit_error = self._parse_execution_limit_error(output["error"])
            if limit_error:
                raise limit_error
            raise SubprocessCrashed(f"Shim error: {output['error']}")

        bash_result = BashResult(
            stdout=output.get("stdout", ""),
            stderr=output.get("stderr", ""),
            exit_code=output.get("exitCode", 0),
            duration_ms=duration_ms,
        )

        if bash_result.exit_code != 0:
            # Check for typed exceptions first
            path = self._parse_file_not_found(bash_result.stderr)
            if path:
                raise FileNotFoundError(
                    f"File not found: {path}",
                    exit_code=bash_result.exit_code,
                    stdout=bash_result.stdout,
                    stderr=bash_result.stderr,
                    path=path,
                )

            path = self._parse_permission_error(bash_result.stderr)
            if path:
                raise PermissionError(
                    f"Permission denied: {path}",
                    exit_code=bash_result.exit_code,
                    stdout=bash_result.stdout,
                    stderr=bash_result.stderr,
                    path=path,
                )

            raise CommandError(
                f"Command failed with exit code {bash_result.exit_code}",
                exit_code=bash_result.exit_code,
                stdout=bash_result.stdout,
                stderr=bash_result.stderr,
            )

        return bash_result

    def read_file(self, path: str) -> str:
        """
        Read file contents directly without bash.

        Args:
            path: Absolute path to the file.

        Returns:
            The file contents as a string.

        Raises:
            FileNotFoundError: If the file does not exist.
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        result = self._run_shim(
            "read-file",
            {"db": str(self._db_path), "path": path},
        )

        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            if result.returncode != 0:
                raise SubprocessCrashed(f"Read file failed: {result.stderr}")
            raise SubprocessCrashed(f"Failed to parse output: {result.stdout[:500]}")

        if "error" in output:
            raise FileNotFoundError(
                f"File not found: {path}",
                exit_code=1,
                stdout="",
                stderr=output["error"],
                path=path,
            )

        return output.get("content", "")

    def write_file(self, path: str, content: str) -> None:
        """
        Write file contents directly without bash.

        Args:
            path: Absolute path to the file.
            content: Content to write to the file.

        Raises:
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        result = self._run_shim(
            "write-file",
            {"db": str(self._db_path), "path": path, "content": content},
        )

        if result.returncode != 0:
            raise SubprocessCrashed(f"Write file failed: {result.stderr}")

    def list_files(self, path: str) -> list[str]:
        """
        List files in a directory.

        Args:
            path: Absolute path to the directory.

        Returns:
            List of file/directory names in the directory.

        Raises:
            FileNotFoundError: If the directory does not exist.
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        result = self._run_shim(
            "list-files",
            {"db": str(self._db_path), "path": path},
        )

        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            if result.returncode != 0:
                raise SubprocessCrashed(f"List files failed: {result.stderr}")
            raise SubprocessCrashed(f"Failed to parse output: {result.stdout[:500]}")

        if "error" in output:
            raise FileNotFoundError(
                f"Directory not found: {path}",
                exit_code=1,
                stdout="",
                stderr=output["error"],
                path=path,
            )

        return output.get("files", [])

    def exists(self, path: str) -> bool:
        """
        Check if a file or directory exists.

        Args:
            path: Absolute path to check.

        Returns:
            True if the path exists, False otherwise.

        Raises:
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        result = self._run_shim(
            "exists",
            {"db": str(self._db_path), "path": path},
        )

        if result.returncode != 0:
            raise SubprocessCrashed(f"Exists check failed: {result.stderr}")

        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise SubprocessCrashed(f"Failed to parse output: {result.stdout[:500]}")

        return output.get("exists", False)

    def delete_file(self, path: str) -> None:
        """
        Delete a file.

        Args:
            path: Absolute path to the file to delete.

        Raises:
            FileNotFoundError: If the file does not exist.
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        result = self._run_shim(
            "delete-file",
            {"db": str(self._db_path), "path": path},
        )

        try:
            output = json.loads(result.stdout)
            if "error" in output:
                raise FileNotFoundError(
                    f"File not found: {path}",
                    exit_code=1,
                    stdout="",
                    stderr=output["error"],
                    path=path,
                )
        except json.JSONDecodeError:
            if result.returncode != 0:
                raise SubprocessCrashed(f"Delete file failed: {result.stderr}")

    def export_snapshot(self) -> bytes:
        """
        Export the current sandbox state as a snapshot.

        The snapshot can be used to restore the sandbox state later by
        passing it to the `snapshot` parameter in the constructor.

        Returns:
            The snapshot as bytes (SQLite database contents).

        Raises:
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        if not self._db_path.exists():
            return b""

        # Checkpoint WAL to ensure all data is in the main database file
        try:
            self._run_shim("checkpoint", {"db": str(self._db_path)})
        except SubprocessCrashed:
            pass  # Continue even if checkpoint times out

        return self._db_path.read_bytes()

    def history(self, limit: int = 100) -> list[HistoryEntry]:
        """
        Get the history of bash commands executed on this sandbox.

        Args:
            limit: Maximum number of entries to return (default 100).

        Returns:
            List of HistoryEntry objects, most recent first.

        Raises:
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        result = self._run_shim(
            "history",
            {"db": str(self._db_path), "limit": str(limit)},
        )

        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            if result.returncode != 0:
                raise SubprocessCrashed(f"History command failed: {result.stderr}")
            raise SubprocessCrashed(f"Failed to parse output: {result.stdout[:500]}")

        if "error" in output:
            raise SubprocessCrashed(f"History error: {output['error']}")

        entries = output.get("entries", [])
        return [
            HistoryEntry(
                id=e.get("id", 0),
                name=e.get("name", ""),
                started_at=e.get("started_at", 0),
                completed_at=e.get("completed_at", 0),
                parameters=e.get("parameters"),
                result=e.get("result"),
            )
            for e in entries
        ]

    def execute_python(self, code: str, cwd: str | None = None) -> PythonResult:
        """
        Execute Python code in the sandbox using Pyodide.

        The Python code runs in a WebAssembly sandbox with access to the
        sandbox's filesystem. File changes made by Python are persisted back
        to the sandbox.

        Args:
            code: The Python code to execute.
            cwd: Working directory for Python (default: sandbox cwd).

        Returns:
            PythonResult with stdout, stderr, exit_code, and optional error.

        Raises:
            SubprocessCrashed: If Python execution fails at the shim level.
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        effective_cwd = cwd if cwd is not None else self._cwd

        result = self._run_shim(
            "execute-python",
            {
                "db": str(self._db_path),
                "code": code,
                "cwd": effective_cwd,
            },
            timeout=300,  # Python can be slow, especially first load
        )

        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            if result.returncode != 0:
                raise SubprocessCrashed(f"Python execution failed: {result.stderr}")
            raise SubprocessCrashed(f"Failed to parse output: {result.stdout[:500]}")

        if "error" in output and output.get("exitCode") is None:
            raise SubprocessCrashed(f"Python execution error: {output['error']}")

        return PythonResult(
            stdout=output.get("stdout", ""),
            stderr=output.get("stderr", ""),
            exit_code=output.get("exitCode", 0),
            error=output.get("error"),
        )

    def destroy(self) -> None:
        """
        Destroy the sandbox and clean up resources.

        After calling destroy(), the sandbox cannot be used again.
        Calling destroy() multiple times is safe (idempotent).
        """
        if self._destroyed:
            return

        # Remove from global registry
        _active_instances.discard(self)

        # Delete database file and associated WAL/SHM files
        for suffix in ["", "-wal", "-shm"]:
            db_file = Path(str(self._db_path) + suffix)
            if db_file.exists():
                try:
                    db_file.unlink()
                except OSError:
                    pass

        # Try to remove temp directory
        if self._temp_dir.exists():
            try:
                self._temp_dir.rmdir()
            except OSError:
                pass

        self._destroyed = True

    def __enter__(self) -> "LocalSandbox":
        """Context manager entry - returns self."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Context manager exit - destroys the sandbox."""
        self.destroy()

    # Async methods
    async def abash(self, command: str) -> BashResult:
        """
        Async version of bash().

        Args:
            command: The bash command to execute.

        Returns:
            BashResult with stdout, stderr, exit_code, and duration_ms.

        Raises:
            CommandError: If the command returns non-zero exit code.
            ExecutionLimitError: If execution limits are exceeded.
            SubprocessCrashed: If the Node subprocess crashes.
            RuntimeError: If the sandbox has been destroyed.
        """
        return await asyncio.to_thread(self.bash, command)

    async def aread_file(self, path: str) -> str:
        """Async version of read_file()."""
        return await asyncio.to_thread(self.read_file, path)

    async def awrite_file(self, path: str, content: str) -> None:
        """Async version of write_file()."""
        await asyncio.to_thread(self.write_file, path, content)

    async def alist_files(self, path: str) -> list[str]:
        """Async version of list_files()."""
        return await asyncio.to_thread(self.list_files, path)

    async def aexists(self, path: str) -> bool:
        """Async version of exists()."""
        return await asyncio.to_thread(self.exists, path)

    async def adelete_file(self, path: str) -> None:
        """Async version of delete_file()."""
        await asyncio.to_thread(self.delete_file, path)

    async def aexport_snapshot(self) -> bytes:
        """Async version of export_snapshot()."""
        return await asyncio.to_thread(self.export_snapshot)

    async def ahistory(self, limit: int = 100) -> list[HistoryEntry]:
        """Async version of history()."""
        return await asyncio.to_thread(self.history, limit)

    async def aexecute_python(self, code: str, cwd: str | None = None) -> PythonResult:
        """Async version of execute_python()."""
        return await asyncio.to_thread(self.execute_python, code, cwd)

    async def adestroy(self) -> None:
        """Async version of destroy()."""
        await asyncio.to_thread(self.destroy)
