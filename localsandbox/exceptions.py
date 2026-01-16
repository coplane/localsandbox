"""LocalSandbox exception hierarchy."""


class LocalSandboxError(Exception):
    """Base exception for all LocalSandbox errors."""


class CommandError(LocalSandboxError):
    """Bash command returned non-zero exit code."""

    def __init__(
        self,
        message: str,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class FileNotFoundError(CommandError):
    """File or directory not found."""

    def __init__(
        self,
        message: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        path: str,
    ) -> None:
        super().__init__(message, exit_code, stdout, stderr)
        self.path = path


class PermissionError(CommandError):
    """Permission denied accessing file or directory."""

    def __init__(
        self,
        message: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        path: str,
    ) -> None:
        super().__init__(message, exit_code, stdout, stderr)
        self.path = path


class TimeoutError(LocalSandboxError):
    """Command exceeded time limit."""

    def __init__(self, message: str, timeout_ms: int) -> None:
        super().__init__(message)
        self.timeout_ms = timeout_ms


class SubprocessCrashed(LocalSandboxError):
    """Node subprocess terminated unexpectedly (OOM, segfault, killed)."""

    def __init__(self, message: str, signal: int | None = None) -> None:
        super().__init__(message)
        self.signal = signal


class ExecutionLimitError(LocalSandboxError):
    """Loop iteration or command count limit exceeded."""

    def __init__(
        self,
        message: str,
        limit_type: str,
        limit_value: int,
    ) -> None:
        super().__init__(message)
        self.limit_type = limit_type
        self.limit_value = limit_value
