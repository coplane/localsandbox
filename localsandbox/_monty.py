"""Optional Monty runtime adapter."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

try:
    from pydantic_monty import (
        CollectStreams,
        Monty,
        MontyCrashedError,
        MontyError,
        MountDir,
    )
except ImportError as exc:
    raise ImportError(
        "The Monty Python runtime requires the 'monty' extra. "
        "Install it with: pip install 'localsandbox[monty]'"
    ) from exc


class MontyWorkerCrashed(RuntimeError):
    """Raised when Monty's isolated worker process crashes."""


class MontyRuntime:
    """Own a persistent Monty pool session for one LocalSandbox."""

    def __init__(self) -> None:
        self._pool_context = Monty(request_timeout=60)
        self._pool = self._pool_context.__enter__()
        self._session_context = self._pool.checkout(type_check=False)
        self._session = self._session_context.__enter__()
        self._closed = False

    def execute(
        self,
        code: str,
        filesystem_root: Path,
        external_lookup: dict[str, Callable[..., Any]],
    ) -> tuple[str, str, int, str | None]:
        """Execute one code feed with the AgentFS materialization mounted."""
        streams = CollectStreams()
        mount = MountDir(
            host_path=filesystem_root,
            virtual_path="/data",
            mode="read-write",
        )

        try:
            self._session.feed_run(
                code,
                external_lookup=external_lookup,
                print_callback=streams,
                mount=mount,
                skip_type_check=True,
            )
        except MontyCrashedError as exc:
            raise MontyWorkerCrashed(str(exc)) from exc
        except MontyError as exc:
            stdout, stderr = self._collect_streams(streams.output)
            message = str(exc)
            stderr += f"{message}\n"
            return stdout, stderr, 1, message

        stdout, stderr = self._collect_streams(streams.output)
        return stdout, stderr, 0, None

    @staticmethod
    def _collect_streams(output: Sequence[tuple[str, str]]) -> tuple[str, str]:
        stdout = "".join(text for stream, text in output if stream == "stdout")
        stderr = "".join(text for stream, text in output if stream == "stderr")
        return stdout, stderr

    def close(self) -> None:
        """Release the Monty worker session and pool."""
        if self._closed:
            return
        try:
            self._session_context.__exit__(None, None, None)
        finally:
            self._pool_context.__exit__(None, None, None)
            self._closed = True
