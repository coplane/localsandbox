# LocalSandbox

A Python SDK for sandboxed filesystem operations, built on
[just-bash](https://github.com/nicholasgriffintn/just-bash),
[AgentFS](https://github.com/tursodatabase/agentfs), and
[Pyodide](https://pyodide.org/). Provides AI agents with a persistent, isolated
environment backed by SQLite.

> ⚠️ **Warning**: This project is in beta. While it provides isolation through
> WebAssembly and a simulated bash environment, it has not been security audited
> and should **not** be relied upon as a fully secure sandbox for running
> untrusted code. Use at your own risk.

## Features

- **Sandboxed Execution**: Run bash commands in an isolated environment
- **Python Execution**: Run Python via Pyodide (WebAssembly) on the same virtual
  filesystem
- **Persistent Filesystem**: All file operations persist across commands in
  SQLite
- **Key-Value Store**: Separate KV API for agent state management
- **Command History**: Track all executed commands with timestamps and results
- **Snapshot & Resume**: Export/restore complete sandbox state
- **Execution Limits**: Configurable DOS protection (loop iterations, command
  counts)
- **Async Support**: Full async API via `asyncio.to_thread`
- **Context Manager**: Clean resource management with `with` statement

## Installation

```bash
pip install localsandbox
# or
uv add localsandbox
```

### Prerequisites

The package requires Deno to run the TypeScript shim. Install Deno
(`brew install deno`) and ensure `deno` is on your PATH.

## Quick Start

```python
from localsandbox import LocalSandbox

# Basic usage with context manager (recommended)
with LocalSandbox() as sandbox:
    result = sandbox.bash('echo "Hello, World!"')
    print(result.stdout)  # Hello, World!

# Without context manager
sandbox = LocalSandbox()
try:
    result = sandbox.bash('echo "Hello!"')
    print(result.stdout)
finally:
    sandbox.destroy()

# Seed initial files
with LocalSandbox(files={"/app/main.py": 'print("hello")'}) as sandbox:
    result = sandbox.execute_python('exec(open("main.py").read())', cwd="/app")
    print(result.stdout)  # hello

# Use file helpers
with LocalSandbox() as sandbox:
    sandbox.write_file("/data/config.json", '{"key": "value"}')
    content = sandbox.read_file("/data/config.json")
    exists = sandbox.exists("/data/config.json")
    files = sandbox.list_files("/data")

# Key-value store
with LocalSandbox() as sandbox:
    sandbox.kv.set("user_id", "12345")
    user_id = sandbox.kv.get("user_id")
    all_keys = sandbox.kv.keys()
```

## Examples

More runnable scripts are in `examples/`.

## API Reference

### LocalSandbox

```python
LocalSandbox(
    files: dict[str, str | Path | bytes] | None = None,
    snapshot: bytes | None = None,
    cwd: str = "/home/user",
    preset: ExecutionPreset = ExecutionPreset.NORMAL,
)
```

**Parameters:**

- `files`: Initial filesystem contents. Supports string content, `Path` objects
  (read at creation), or `bytes` for binary files.
- `snapshot`: Restore from a previously exported snapshot (mutually exclusive
  with `files`).
- `cwd`: Initial working directory (default: `/home/user`).
- `preset`: Execution limits preset (`STRICT`, `NORMAL`, or `PERMISSIVE`).

### Methods

#### Bash Execution

```python
sandbox.bash(command: str) -> BashResult
```

Execute a bash command. Returns `BashResult` with `stdout`, `stderr`,
`exit_code`, and `duration_ms`.

Raises:

- `CommandError`: Non-zero exit code
- `FileNotFoundError`: File/directory not found (with `.path` attribute)
- `PermissionError`: Permission denied (with `.path` attribute)
- `ExecutionLimitError`: Execution limits exceeded
- `SubprocessCrashed`: Shim subprocess failure

#### Python Execution

```python
sandbox.execute_python(code: str, cwd: str | None = None) -> PythonResult
```

Execute Python via Pyodide. The sandbox filesystem is mounted at `/data` inside
Python; `cwd` controls where relative paths resolve. In bash, `/data` is also
available as an alias to the sandbox root, so `/data/...` and `/...` refer to
the same files.

#### File Operations

```python
sandbox.read_file(path: str) -> str
sandbox.write_file(path: str, content: str) -> None
sandbox.list_files(path: str) -> list[str]
sandbox.exists(path: str) -> bool
sandbox.delete_file(path: str) -> None
```

#### Key-Value Store

```python
sandbox.kv.get(key: str) -> str | None
sandbox.kv.set(key: str, value: str) -> None
sandbox.kv.delete(key: str) -> None
sandbox.kv.keys(prefix: str = "") -> list[str]
```

#### Command History

```python
sandbox.history(limit: int = 100) -> list[HistoryEntry]
```

Get the history of tool calls executed on this sandbox. Returns a list of
`HistoryEntry` objects with:

- `id`: Unique identifier
- `name`: Tool name (e.g., "bash" or "python")
- `started_at`: Unix timestamp when command started
- `completed_at`: Unix timestamp when command finished
- `parameters`: Dict with `command`/`cwd` (bash) or `codeLength`/`cwd` (python)
- `result`: Dict with `exitCode`

```python
from localsandbox import LocalSandbox

with LocalSandbox() as sandbox:
    sandbox.bash('echo "hello"')
    sandbox.bash('ls -la')

    history = sandbox.history()
    for entry in history:
        print(f"Command: {entry.parameters['command']}, Exit: {entry.result['exitCode']}")
```

#### Snapshot & Resume

```python
# Export current state
snapshot = sandbox.export_snapshot()

# Resume from snapshot
new_sandbox = LocalSandbox(snapshot=snapshot)
```

#### Lifecycle

```python
sandbox.destroy()  # Clean up resources (called automatically by context manager)
```

### Async API

All methods have async versions prefixed with `a`:

```python
import asyncio
from localsandbox import LocalSandbox

async def main():
    sandbox = LocalSandbox()
    try:
        result = await sandbox.abash('echo "async!"')
        await sandbox.awrite_file("/tmp/test.txt", "content")
        content = await sandbox.aread_file("/tmp/test.txt")
        await sandbox.kv.aset("key", "value")
        value = await sandbox.kv.aget("key")
    finally:
        await sandbox.adestroy()

asyncio.run(main())
```

### Execution Presets

| Preset     | Max Loop Iterations | Max Commands |
| ---------- | ------------------- | ------------ |
| STRICT     | 100                 | 500          |
| NORMAL     | 1,000               | 5,000        |
| PERMISSIVE | 10,000              | 50,000       |

```python
from localsandbox import LocalSandbox, ExecutionPreset

# For untrusted input
sandbox = LocalSandbox(preset=ExecutionPreset.STRICT)

# For complex operations
sandbox = LocalSandbox(preset=ExecutionPreset.PERMISSIVE)
```

## Architecture

LocalSandbox uses a TypeScript shim (running on Deno) that bridges Python to:

- **just-bash**: A bash interpreter/simulator written in TypeScript
- **AgentFS**: SQLite-based virtual filesystem
- **Pyodide**: Python interpreter compiled to WebAssembly for sandboxed Python
  execution

Each operation spawns a Deno subprocess that:

1. Opens the SQLite database
2. Executes the operation via just-bash or Pyodide
3. Persists changes back to SQLite
4. Returns JSON results

This architecture provides strong isolation while maintaining state persistence.
Both bash and Python share the same virtual filesystem backed by SQLite.

## Development

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest

# Type checking
uv run pyright

# Lint and format
uv run ruff check --fix && uv run ruff format

# Shim checks
cd shim && deno task check
```

## License

MIT
