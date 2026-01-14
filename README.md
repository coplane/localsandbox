# BashFS

A Python SDK for sandboxed filesystem operations, built on [just-bash](https://github.com/nicholasgriffintn/just-bash) and [AgentFS](https://github.com/tursodatabase/agentfs). Provides AI agents with a persistent, isolated bash environment backed by SQLite.

## Features

- **Sandboxed Bash Execution**: Run bash commands in an isolated environment
- **Persistent Filesystem**: All file operations persist across commands in SQLite
- **Key-Value Store**: Separate KV API for agent state management
- **Command History**: Track all executed bash commands with timestamps and results
- **Snapshot & Resume**: Export/restore complete sandbox state
- **Execution Limits**: Configurable DOS protection (loop iterations, command counts)
- **Async Support**: Full async API via `asyncio.to_thread`
- **Context Manager**: Clean resource management with `with` statement

## Installation

```bash
pip install bashfs-py
```

### Prerequisites

The package requires Node.js to run the TypeScript shim. After installing, build the shim:

```bash
cd shim && pnpm install && pnpm build
```

## Quick Start

```python
from bashfs import BashFS

# Basic usage
with BashFS() as sandbox:
    result = sandbox.bash('echo "Hello, World!"')
    print(result.stdout)  # Hello, World!

# Seed initial files
with BashFS(files={"/app/main.py": 'print("hello")'}) as sandbox:
    result = sandbox.bash("python /app/main.py")
    print(result.stdout)  # hello

# Use file helpers
with BashFS() as sandbox:
    sandbox.write_file("/data/config.json", '{"key": "value"}')
    content = sandbox.read_file("/data/config.json")
    exists = sandbox.exists("/data/config.json")
    files = sandbox.list_files("/data")

# Key-value store
with BashFS() as sandbox:
    sandbox.kv.set("user_id", "12345")
    user_id = sandbox.kv.get("user_id")
    all_keys = sandbox.kv.keys()
```

## API Reference

### BashFS

```python
BashFS(
    files: dict[str, str | Path | bytes] | None = None,
    snapshot: bytes | None = None,
    cwd: str = "/home/user",
    preset: ExecutionPreset = ExecutionPreset.NORMAL,
)
```

**Parameters:**
- `files`: Initial filesystem contents. Supports string content, `Path` objects (read at creation), or `bytes` for binary files.
- `snapshot`: Restore from a previously exported snapshot (mutually exclusive with `files`).
- `cwd`: Initial working directory (default: `/home/user`).
- `preset`: Execution limits preset (`STRICT`, `NORMAL`, or `PERMISSIVE`).

### Methods

#### Bash Execution

```python
sandbox.bash(command: str) -> BashResult
```

Execute a bash command. Returns `BashResult` with `stdout`, `stderr`, `exit_code`, and `duration_ms`.

Raises:
- `CommandError`: Non-zero exit code
- `FileNotFoundError`: File/directory not found (with `.path` attribute)
- `PermissionError`: Permission denied (with `.path` attribute)
- `ExecutionLimitError`: Execution limits exceeded
- `SubprocessCrashed`: Node subprocess failure

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

Get the history of bash commands executed on this sandbox. Returns a list of `HistoryEntry` objects with:
- `id`: Unique identifier
- `name`: Tool name (always "bash")
- `started_at`: Unix timestamp when command started
- `completed_at`: Unix timestamp when command finished
- `parameters`: Dict with `command` and `cwd`
- `result`: Dict with `exitCode`

```python
from bashfs import BashFS

with BashFS() as sandbox:
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
new_sandbox = BashFS(snapshot=snapshot)
```

#### Lifecycle

```python
sandbox.destroy()  # Clean up resources (called automatically by context manager)
```

### Async API

All methods have async versions prefixed with `a`:

```python
import asyncio
from bashfs import BashFS

async def main():
    sandbox = BashFS()
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

| Preset | Max Loop Iterations | Max Commands |
|--------|---------------------|--------------|
| STRICT | 100 | 500 |
| NORMAL | 1,000 | 5,000 |
| PERMISSIVE | 10,000 | 50,000 |

```python
from bashfs import BashFS, ExecutionPreset

# For untrusted input
sandbox = BashFS(preset=ExecutionPreset.STRICT)

# For complex operations
sandbox = BashFS(preset=ExecutionPreset.PERMISSIVE)
```

## Exception Hierarchy

```
BashFSError (base)
├── CommandError (non-zero exit)
│   ├── FileNotFoundError (with .path)
│   └── PermissionError (with .path)
├── ExecutionLimitError (with .limit_type, .limit_value)
├── SubprocessCrashed
└── TimeoutError
```

## Architecture

BashFS uses a TypeScript shim that bridges Python to:
- **just-bash**: A bash interpreter/simulator
- **AgentFS**: SQLite-based virtual filesystem

Each operation spawns a Node subprocess that:
1. Opens the SQLite database
2. Executes the operation via just-bash
3. Persists changes back to SQLite
4. Returns JSON results

This architecture provides strong isolation while maintaining state persistence.

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

# Build shim
cd shim && pnpm install && pnpm build
```

## License

MIT
