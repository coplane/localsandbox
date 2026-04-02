"""Core LocalSandbox implementation."""

import asyncio
import atexit
import base64
import concurrent.futures
import inspect
import json
import re
import subprocess
import tempfile
import threading
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from localsandbox.exceptions import (
    CommandError,
    ExecutionLimitError,
    FileNotFoundError,
    PermissionError,
    SubprocessCrashed,
    TimeoutError,
)


def _get_server_path() -> Path:
    """Get the path to the TypeScript server (runs via Deno)."""
    package_dir = Path(__file__).parent
    server_path = package_dir / "shim" / "src" / "server.ts"
    if not server_path.exists():
        raise RuntimeError(f"Server not found at {server_path}.")
    return server_path


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


JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
ToolHandler = Callable[[dict[str, JsonValue]], JsonValue | Awaitable[JsonValue]]


@dataclass(frozen=True)
class ToolDefinition:
    """Definition of a host tool callable from Pyodide."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    timeout_ms: int = 30_000


@dataclass(frozen=True)
class PythonToolset:
    """Collection of tool definitions and handlers for Python execution."""

    definitions: list[ToolDefinition]
    handlers: dict[str, ToolHandler]


def _json_type_name(value: object) -> str:
    """Return JSON Schema primitive type name for a Python value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _validate_json_schema(
    value: JsonValue,
    schema: dict[str, Any],
    path: str = "$",
) -> None:
    """
    Validate a JSON-like value against a small JSON Schema subset.

    Supported keys:
    - type
    - properties
    - required
    - additionalProperties
    - items
    - enum
    """
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']!r}")

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        allowed_types = schema_type
    elif schema_type is None:
        allowed_types = None
    else:
        allowed_types = [schema_type]

    if allowed_types is not None:
        matches_type = False
        for allowed_type in allowed_types:
            if allowed_type == "number":
                matches_type = isinstance(value, (int, float)) and not isinstance(
                    value, bool
                )
            elif allowed_type == "integer":
                matches_type = isinstance(value, int) and not isinstance(value, bool)
            elif allowed_type == "boolean":
                matches_type = isinstance(value, bool)
            elif allowed_type == "string":
                matches_type = isinstance(value, str)
            elif allowed_type == "null":
                matches_type = value is None
            elif allowed_type == "array":
                matches_type = isinstance(value, list)
            elif allowed_type == "object":
                matches_type = isinstance(value, dict)
            if matches_type:
                break

        if not matches_type:
            expected = allowed_types if len(allowed_types) > 1 else allowed_types[0]
            raise ValueError(
                f"{path} must be of type {expected!r}, got {_json_type_name(value)!r}"
            )

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional_properties = schema.get("additionalProperties", True)

        for key in required:
            if key not in value:
                raise ValueError(f"{path}.{key} is required")

        for key, item in value.items():
            if key in properties:
                child_schema = properties[key]
                _validate_json_schema(item, child_schema, f"{path}.{key}")
            elif additional_properties is False:
                raise ValueError(f"{path}.{key} is not allowed")
            elif isinstance(additional_properties, dict):
                _validate_json_schema(item, additional_properties, f"{path}.{key}")
    elif isinstance(value, list) and "items" in schema:
        item_schema = schema["items"]
        for index, item in enumerate(value):
            _validate_json_schema(item, item_schema, f"{path}[{index}]")


def _ensure_json_value(value: Any) -> JsonValue:
    """Verify a handler result is JSON-serializable and return it unchanged."""
    try:
        json.dumps(value)
    except TypeError as exc:
        raise ValueError(f"Value is not JSON-serializable: {exc}") from exc
    return value


def _run_awaitable(awaitable: Awaitable[JsonValue]) -> JsonValue:
    """Run an awaitable in a fresh event loop inside a worker thread."""

    async def _wrapper() -> JsonValue:
        return await awaitable

    return asyncio.run(_wrapper())


def _execute_tool_handler(
    definition: ToolDefinition,
    handler: ToolHandler,
    payload: dict[str, JsonValue],
) -> JsonValue:
    """Execute a tool handler with best-effort timeout and JSON checks."""

    def invoke() -> JsonValue:
        result = handler(payload)
        if inspect.isawaitable(result):
            result = _run_awaitable(result)
        return _ensure_json_value(result)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(invoke)

    try:
        return future.result(timeout=definition.timeout_ms / 1000)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise TimeoutError(
            f"Tool {definition.name!r} timed out after {definition.timeout_ms} ms",
            timeout_ms=definition.timeout_ms,
        ) from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


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
        result = self._sandbox._send_request({"type": "kv_get", "key": key})
        return result.get("value")

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
        self._sandbox._send_request({"type": "kv_set", "key": key, "value": value})

    def delete(self, key: str) -> None:
        """
        Delete a key-value pair.

        Args:
            key: The key to delete.

        Raises:
            RuntimeError: If the sandbox has been destroyed.
        """
        self._check_destroyed()
        self._sandbox._send_request({"type": "kv_delete", "key": key})

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
        result = self._sandbox._send_request({"type": "kv_keys", "prefix": prefix})
        return result.get("keys", [])

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

    Each instance owns a persistent Deno server subprocess that opens the
    AgentFS database once and serves bash, file, KV, history, and Python
    requests over stdio. All filesystem state persists in a SQLite database
    file.
    """

    def __init__(
        self,
        files: dict[str, str | Path | bytes] | None = None,
        snapshot: bytes | None = None,
        cwd: str = "/data",
        preset: ExecutionPreset = ExecutionPreset.NORMAL,
    ) -> None:
        """
        Create a new LocalSandbox.

        Args:
            files: Initial filesystem contents. String values are file content,
                   Path values are read and snapshotted at creation,
                   bytes are written as binary. All paths should use /data prefix.
            snapshot: Restore from a previously exported snapshot (mutually
                      exclusive with `files`).
            cwd: Initial working directory (default: /data).
            preset: Execution limits preset (STRICT, NORMAL, or PERMISSIVE).

        Raises:
            ValueError: If both `files` and `snapshot` are provided.
            RuntimeError: If the shim is not built.
        """
        if files is not None and snapshot is not None:
            raise ValueError("Cannot provide both 'files' and 'snapshot'")

        self._server_path = _get_server_path()
        self._cwd = cwd
        self._preset = preset
        self._limits = _PRESET_LIMITS[preset]
        self._destroyed = False

        # Persistent server subprocess
        self._server_proc: subprocess.Popen[str] | None = None
        self._server_stderr_thread: threading.Thread | None = None
        self._server_stderr_lines: list[str] = []
        self._server_lock = threading.RLock()
        self._request_counter = 0
        self._server_startup_timeout_ms = 10_000
        self._default_request_timeout_ms = 60_000
        self._bash_timeout_ms = 30_000
        self._python_timeout_ms = 60_000

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

    def _next_request_id(self) -> str:
        self._request_counter += 1
        return f"r{self._request_counter}"

    def _ensure_server(self) -> None:
        """Start the persistent server subprocess if not already running."""
        with self._server_lock:
            if self._server_proc is not None and self._server_proc.poll() is None:
                return

            proc = subprocess.Popen(
                [
                    "deno",
                    "run",
                    "--allow-read",
                    "--allow-write",
                    "--allow-env",
                    "--allow-ffi",
                    "--allow-run",
                    str(self._server_path),
                    "--db",
                    str(self._db_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            if proc.stdin is None or proc.stdout is None or proc.stderr is None:
                proc.kill()
                raise SubprocessCrashed("Failed to open server subprocess pipes")

            stderr_stream = proc.stderr
            self._server_stderr_lines = []

            def collect_stderr() -> None:
                for line in stderr_stream:
                    self._server_stderr_lines.append(line)

            self._server_stderr_thread = threading.Thread(
                target=collect_stderr, daemon=True
            )
            self._server_stderr_thread.start()
            self._server_proc = proc

            # Wait for the server to signal readiness.
            ready = self._read_server_envelope(
                proc,
                eof_message="Server exited during startup",
                timeout_ms=self._server_startup_timeout_ms,
                timeout_context="Server startup",
            )

            if ready.get("type") != "ready":
                self._stop_server()
                raise SubprocessCrashed(f"Unexpected server handshake: {ready!r}")

    def _stop_server(self, *, force: bool = False) -> None:
        """Gracefully stop the persistent server subprocess."""
        with self._server_lock:
            proc = self._server_proc
            if proc is None:
                return
            try:
                if proc.poll() is None:
                    if force:
                        proc.kill()
                    else:
                        try:
                            if proc.stdin:
                                shutdown = json.dumps(
                                    {"id": self._next_request_id(), "type": "shutdown"}
                                )
                                proc.stdin.write(shutdown + "\n")
                                proc.stdin.flush()
                                proc.stdin.close()
                        except OSError:
                            pass
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
            except OSError:
                if proc.poll() is None:
                    proc.kill()
            finally:
                if self._server_stderr_thread:
                    self._server_stderr_thread.join(timeout=1)
                self._server_proc = None
                self._server_stderr_thread = None
                self._server_stderr_lines = []

    def _readline_with_timeout(
        self,
        stream: Any,
        *,
        timeout_ms: int,
        timeout_context: str,
    ) -> str:
        """Read one line from a blocking text stream with a deadline."""
        line: str | None = None
        error: BaseException | None = None

        def reader() -> None:
            nonlocal line, error
            try:
                line = stream.readline()
            except BaseException as exc:  # noqa: BLE001 - propagate stream errors
                error = exc

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout_ms / 1000)

        if thread.is_alive():
            self._stop_server(force=True)
            thread.join(timeout=1)
            raise TimeoutError(
                f"{timeout_context} timed out after {timeout_ms} ms",
                timeout_ms=timeout_ms,
            )

        if error is not None:
            self._stop_server(force=True)
            raise SubprocessCrashed(
                f"{timeout_context} failed while reading server output: {error}"
            ) from error

        return line or ""

    def _read_server_envelope(
        self,
        proc: subprocess.Popen[str],
        *,
        eof_message: str,
        timeout_ms: int,
        timeout_context: str,
    ) -> dict[str, Any]:
        """Read and decode one NDJSON envelope from the server."""
        assert proc.stdout is not None

        line = self._readline_with_timeout(
            proc.stdout,
            timeout_ms=timeout_ms,
            timeout_context=timeout_context,
        )

        if line == "":
            stderr_output = "".join(self._server_stderr_lines).strip()
            self._stop_server(force=True)
            raise SubprocessCrashed(f"{eof_message}: {stderr_output}")

        try:
            envelope = json.loads(line)
        except json.JSONDecodeError as exc:
            self._stop_server()
            raise SubprocessCrashed(f"Invalid server response: {line}") from exc

        if not isinstance(envelope, dict):
            self._stop_server()
            raise SubprocessCrashed(f"Invalid server response envelope: {line}")

        return envelope

    def _remaining_timeout_ms(
        self,
        deadline: float,
        total_timeout_ms: int,
        timeout_context: str,
    ) -> int:
        """Return time left until deadline or raise TimeoutError."""
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms > 0:
            return remaining_ms

        self._stop_server(force=True)
        raise TimeoutError(
            f"{timeout_context} timed out after {total_timeout_ms} ms",
            timeout_ms=total_timeout_ms,
        )

    def _require_matching_id(
        self,
        envelope: dict[str, Any],
        request_id: str,
        *,
        key: str = "id",
    ) -> None:
        """Fail fast if the server responded for a different request."""
        envelope_id = envelope.get(key)
        if envelope_id == request_id:
            return

        self._stop_server()
        raise SubprocessCrashed(
            "Server protocol error: "
            f"expected {key}={request_id!r}, got {envelope_id!r} "
            f"for envelope type {envelope.get('type')!r}"
        )

    def _send_request(
        self,
        request: dict[str, Any],
        *,
        timeout_ms: int | None = None,
        timeout_context: str = "Server request",
    ) -> dict[str, Any]:
        """Send a request to the server and return the response data.

        Raises:
            SubprocessCrashed: If the server exits or returns an error.
        """
        effective_timeout_ms = (
            self._default_request_timeout_ms if timeout_ms is None else timeout_ms
        )
        with self._server_lock:
            self._ensure_server()
            proc = self._server_proc
            assert proc is not None and proc.stdin and proc.stdout

            request_id = self._next_request_id()
            request["id"] = request_id
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()

            response = self._read_server_envelope(
                proc,
                eof_message="Server exited unexpectedly",
                timeout_ms=effective_timeout_ms,
                timeout_context=timeout_context,
            )
            self._require_matching_id(response, request_id)

            if response.get("type") == "error":
                error_type = response.get("error_type", "")
                message = response.get("error", "Unknown error")
                self._raise_for_error(error_type, message, request)

            return response.get("data", {})

    def _raise_for_error(
        self,
        error_type: str,
        message: str,
        request: dict[str, Any],
    ) -> None:
        """Map a server error response to the appropriate exception."""
        if error_type == "file_not_found":
            path = request.get("path", "")
            raise FileNotFoundError(
                f"File not found: {path}",
                exit_code=1,
                stdout="",
                stderr=message,
                path=path,
            )
        raise SubprocessCrashed(message)

    def _validate_toolset(
        self,
        toolset: PythonToolset,
    ) -> tuple[dict[str, ToolDefinition], dict[str, ToolHandler]]:
        """Validate tool definitions and handlers and return name-indexed maps."""
        definitions: dict[str, ToolDefinition] = {}
        for definition in toolset.definitions:
            if definition.name in definitions:
                raise ValueError(f"Duplicate tool definition: {definition.name}")
            if definition.timeout_ms <= 0:
                raise ValueError(
                    f"Tool {definition.name!r} must have a positive timeout_ms"
                )
            definitions[definition.name] = definition

        handlers = dict(toolset.handlers)
        missing_handlers = [name for name in definitions if name not in handlers]
        if missing_handlers:
            raise ValueError(
                f"Missing handlers for tools: {', '.join(sorted(missing_handlers))}"
            )

        extra_handlers = [name for name in handlers if name not in definitions]
        if extra_handlers:
            raise ValueError(
                f"Handlers declared without matching tool definitions: "
                f"{', '.join(sorted(extra_handlers))}"
            )

        return definitions, handlers

    def _handle_tool_call(
        self,
        envelope: dict[str, Any],
        definitions: dict[str, ToolDefinition],
        handlers: dict[str, ToolHandler],
    ) -> dict[str, Any]:
        """Process a tool_call envelope and return the response envelope."""
        tool_id = envelope.get("id")
        tool_name = envelope.get("name")
        payload = envelope.get("payload")

        try:
            if not isinstance(tool_name, str):
                raise ValueError("Tool call is missing a valid name")
            if not isinstance(payload, dict):
                raise ValueError(f"Tool {tool_name!r} payload must be an object")
            if tool_name not in definitions:
                return {
                    "type": "tool_error",
                    "id": tool_id,
                    "error_type": "permission_error",
                    "message": f"Tool {tool_name!r} is not declared",
                }

            definition = definitions[tool_name]
            _validate_json_schema(
                payload, definition.input_schema, path=f"$[{tool_name}]"
            )
            result = _execute_tool_handler(definition, handlers[tool_name], payload)
            if definition.output_schema is not None:
                _validate_json_schema(
                    result, definition.output_schema, path=f"$[{tool_name}]"
                )
            return {"type": "tool_result", "id": tool_id, "payload": result}
        except TimeoutError as exc:
            return {
                "type": "tool_error",
                "id": tool_id,
                "error_type": "timeout",
                "message": str(exc),
            }
        except ValueError as exc:
            return {
                "type": "tool_error",
                "id": tool_id,
                "error_type": "validation_error",
                "message": str(exc),
            }
        except Exception as exc:
            return {
                "type": "tool_error",
                "id": tool_id,
                "error_type": "internal_error",
                "message": str(exc),
            }

    def _execute_python_via_server(
        self,
        code: str,
        cwd: str,
        preload_packages: list[str] | None,
        toolset: PythonToolset | None,
    ) -> PythonResult:
        """Execute Python code via the server, handling tool call relay."""
        definitions: dict[str, ToolDefinition] = {}
        handlers: dict[str, ToolHandler] = {}
        tool_manifest: list[dict[str, Any]] = []

        if toolset is not None:
            definitions, handlers = self._validate_toolset(toolset)
            tool_manifest = [
                {
                    "name": d.name,
                    "description": d.description,
                    "input_schema": d.input_schema,
                    "output_schema": d.output_schema,
                    "timeout_ms": d.timeout_ms,
                }
                for d in toolset.definitions
            ]

        with self._server_lock:
            self._ensure_server()
            proc = self._server_proc
            assert proc is not None and proc.stdin and proc.stdout

            deadline = time.monotonic() + (self._python_timeout_ms / 1000)
            request_id = self._next_request_id()
            request = {
                "id": request_id,
                "type": "execute_python",
                "code": code,
                "cwd": cwd,
                "preload_packages": preload_packages or [],
                "tools": tool_manifest,
            }
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()

            while True:
                envelope = self._read_server_envelope(
                    proc,
                    eof_message="Server exited during Python execution",
                    timeout_ms=self._remaining_timeout_ms(
                        deadline, self._python_timeout_ms, "Python execution"
                    ),
                    timeout_context="Python execution",
                )
                envelope_type = envelope.get("type")

                if envelope_type == "tool_call":
                    self._require_matching_id(envelope, request_id, key="request_id")
                    response = self._handle_tool_call(envelope, definitions, handlers)
                    proc.stdin.write(json.dumps(response) + "\n")
                    proc.stdin.flush()
                    continue

                self._require_matching_id(envelope, request_id)

                if envelope_type == "result":
                    data = envelope.get("data", {})
                    return PythonResult(
                        stdout=data.get("stdout", ""),
                        stderr=data.get("stderr", ""),
                        exit_code=data.get("exit_code", 0),
                        error=data.get("error"),
                    )

                if envelope_type == "error":
                    raise SubprocessCrashed(
                        f"Python execution error: {envelope.get('error', '')}"
                    )

                raise SubprocessCrashed(f"Unexpected envelope: {envelope_type!r}")

    def _seed_files(self, files: dict[str, str | Path | bytes]) -> None:
        """Seed initial files into the sandbox."""
        resolved: dict[str, str | dict[str, str]] = {}
        for file_path, content in files.items():
            if isinstance(content, Path):
                try:
                    resolved[file_path] = content.read_text()
                except UnicodeDecodeError:
                    resolved[file_path] = {
                        "base64": base64.b64encode(content.read_bytes()).decode("ascii")
                    }
            elif isinstance(content, bytes):
                resolved[file_path] = {
                    "base64": base64.b64encode(content).decode("ascii")
                }
            else:
                resolved[file_path] = content

        self._send_request({"type": "seed", "files": resolved})

    def _parse_execution_limit_error(
        self,
        error_message: str,
        *,
        exit_code: int | None = None,
    ) -> ExecutionLimitError | None:
        """Parse execution limit failures from current just-bash stderr output."""
        # Current just-bash loop iteration wording:
        # "bash: for loop: too many iterations (100), increase executionLimits.maxLoopIterations"
        loop_match = re.search(
            r"too many iterations\s*\((\d+)\).*executionLimits\.maxLoopIterations",
            error_message,
            re.IGNORECASE,
        )
        if loop_match:
            return ExecutionLimitError(
                error_message,
                limit_type="loop_iterations",
                limit_value=int(loop_match.group(1)),
            )

        # Current just-bash command count wording:
        # "bash: too many commands executed (>500), increase executionLimits.maxCommandCount"
        cmd_match = re.search(
            r"too many commands executed\s*\(>?(\d+)\).*executionLimits\.maxCommandCount",
            error_message,
            re.IGNORECASE,
        )
        if cmd_match:
            return ExecutionLimitError(
                error_message,
                limit_type="command_count",
                limit_value=int(cmd_match.group(1)),
            )

        # Alternate just-bash command count wording from top-level command guard:
        # "bash: maximum command count (500) exceeded ..."
        cmd_match = re.search(
            r"maximum command count\s*\((\d+)\).*executionLimits\.maxCommandCount",
            error_message,
            re.IGNORECASE,
        )
        if cmd_match:
            return ExecutionLimitError(
                error_message,
                limit_type="command_count",
                limit_value=int(cmd_match.group(1)),
            )

        # just-bash uses exit code 126 for execution-limit failures. Keep this
        # as a fallback when stderr wording is less specific than expected.
        if exit_code == 126 or "executionlimits." in error_message.lower():
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
            SubprocessCrashed: If the server subprocess crashes.
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        start_time = time.perf_counter()

        output = self._send_request(
            {
                "type": "bash",
                "command": command,
                "cwd": self._cwd,
                "limits": self._limits,
            },
            timeout_ms=self._bash_timeout_ms,
            timeout_context="Bash execution",
        )

        duration_ms = (time.perf_counter() - start_time) * 1000

        bash_result = BashResult(
            stdout=output.get("stdout", ""),
            stderr=output.get("stderr", ""),
            exit_code=output.get("exitCode", 0),
            duration_ms=duration_ms,
        )

        if bash_result.exit_code != 0:
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

            limit_error = self._parse_execution_limit_error(
                bash_result.stderr,
                exit_code=bash_result.exit_code,
            )
            if limit_error:
                raise limit_error

            msg = f"Command failed with exit code {bash_result.exit_code}"
            if bash_result.stderr:
                msg += f": {bash_result.stderr.strip()}"
            raise CommandError(
                msg,
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

        result = self._send_request({"type": "read_file", "path": path})
        return result.get("content", "")

    def read_file_bytes(self, path: str) -> bytes:
        """
        Read file contents as bytes directly without bash.

        Args:
            path: Absolute path to the file.

        Returns:
            The file contents as bytes.

        Raises:
            FileNotFoundError: If the file does not exist.
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        result = self._send_request({"type": "read_file", "path": path, "binary": True})
        return base64.b64decode(result.get("content", ""))

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

        self._send_request({"type": "write_file", "path": path, "content": content})

    def write_file_bytes(self, path: str, data: bytes) -> None:
        """
        Write binary data to a file directly without bash.

        Args:
            path: Absolute path to the file.
            data: Binary data to write to the file.

        Raises:
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        self._send_request(
            {
                "type": "write_file",
                "path": path,
                "content": base64.b64encode(data).decode("ascii"),
                "binary": True,
            }
        )

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

        result = self._send_request({"type": "list_files", "path": path})
        return result.get("files", [])

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

        result = self._send_request({"type": "exists", "path": path})
        return result.get("exists", False)

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

        self._send_request({"type": "delete_file", "path": path})

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

        try:
            self._send_request({"type": "checkpoint"})
        except SubprocessCrashed:
            pass

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

        output = self._send_request({"type": "history", "limit": limit})
        return [
            HistoryEntry(
                id=e.get("id", 0),
                name=e.get("name", ""),
                started_at=e.get("started_at", 0),
                completed_at=e.get("completed_at", 0),
                parameters=e.get("parameters"),
                result=e.get("result"),
            )
            for e in output.get("entries", [])
        ]

    def execute_python(
        self,
        code: str,
        cwd: str | None = None,
        preload_packages: list[str] | None = None,
        toolset: PythonToolset | None = None,
    ) -> PythonResult:
        """
        Execute Python code in the sandbox using Pyodide.

        The Python code runs in a WebAssembly sandbox with access to the
        sandbox's filesystem. File changes made by Python are persisted back
        to the sandbox. Compatible executions may reuse a warmed interpreter.

        Args:
            code: The Python code to execute.
            cwd: Working directory for Python (default: sandbox cwd).
            preload_packages: Optional list of Pyodide packages to preload.
            toolset: Optional set of host tools available to Python code.

        Returns:
            PythonResult with stdout, stderr, exit_code, and optional error.

        Raises:
            SubprocessCrashed: If Python execution fails at the shim level.
            RuntimeError: If the sandbox has been destroyed.
        """
        if self._destroyed:
            raise RuntimeError("LocalSandbox instance has been destroyed")

        effective_cwd = cwd if cwd is not None else self._cwd

        return self._execute_python_via_server(
            code=code,
            cwd=effective_cwd,
            preload_packages=preload_packages,
            toolset=toolset,
        )

    def destroy(self) -> None:
        """
        Destroy the sandbox and clean up resources.

        After calling destroy(), the sandbox cannot be used again.
        Calling destroy() multiple times is safe (idempotent).
        """
        if self._destroyed:
            return

        self._stop_server()

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
            SubprocessCrashed: If the server subprocess crashes.
            RuntimeError: If the sandbox has been destroyed.
        """
        return await asyncio.to_thread(self.bash, command)

    async def aread_file(self, path: str) -> str:
        """Async version of read_file()."""
        return await asyncio.to_thread(self.read_file, path)

    async def aread_file_bytes(self, path: str) -> bytes:
        """Async version of read_file_bytes()."""
        return await asyncio.to_thread(self.read_file_bytes, path)

    async def awrite_file(self, path: str, content: str) -> None:
        """Async version of write_file()."""
        await asyncio.to_thread(self.write_file, path, content)

    async def awrite_file_bytes(self, path: str, data: bytes) -> None:
        """Async version of write_file_bytes()."""
        await asyncio.to_thread(self.write_file_bytes, path, data)

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

    async def aexecute_python(
        self,
        code: str,
        cwd: str | None = None,
        preload_packages: list[str] | None = None,
        toolset: PythonToolset | None = None,
    ) -> PythonResult:
        """Async version of execute_python()."""
        return await asyncio.to_thread(
            self.execute_python,
            code,
            cwd,
            preload_packages,
            toolset,
        )

    async def adestroy(self) -> None:
        """Async version of destroy()."""
        await asyncio.to_thread(self.destroy)
