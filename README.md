# LocalSandbox

A Python SDK for sandboxed filesystem operations, built on
[just-bash](https://github.com/vercel-labs/just-bash),
[AgentFS](https://github.com/tursodatabase/agentfs), and
[Pyodide](https://pyodide.org/), with an optional
[Monty](https://github.com/pydantic/monty) Python runtime. Provides AI agents
with a persistent, isolated environment backed by SQLite.

> ⚠️ **Warning**: This project is in beta. While its runtimes isolate executed
> code and bash is simulated, LocalSandbox has not been security audited and
> should **not** be relied upon as a fully secure sandbox for running untrusted
> code. Use at your own risk.

## Features

- **Sandboxed Execution**: Run bash commands in an isolated environment
- **Python Execution**: Choose Pyodide (WebAssembly) or Monty when constructing
  a sandbox; both use the same persistent virtual filesystem
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

Install the optional Monty backend with:

```bash
pip install "localsandbox[monty]"
# or
uv add "localsandbox[monty]"
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

# Seed initial files (all paths use /data prefix)
with LocalSandbox(files={"/data/app/main.py": 'print("hello")'}) as sandbox:
    result = sandbox.execute_python('exec(open("main.py").read())', cwd="/data/app")
    print(result.stdout)  # hello

# Use file helpers (all paths use /data prefix)
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

## Design Notes

- Python execution architecture: `docs/design/python-execution.md`
- Tool bridge design: `docs/design/pyodide-tool-bridge.md`

## API Reference

### LocalSandbox

```python
LocalSandbox(
    files: dict[str, str | Path | bytes] | None = None,
    snapshot: bytes | None = None,
    cwd: str = "/data",
    preset: ExecutionPreset = ExecutionPreset.NORMAL,
    python_runtime: PythonRuntime | str = PythonRuntime.PYODIDE,
)
```

**Parameters:**

- `files`: Initial filesystem contents. Supports string content, `Path` objects
  (read at creation), or `bytes` for binary files. All paths should use the
  `/data` prefix.
- `snapshot`: Restore from a previously exported snapshot (mutually exclusive
  with `files`).
- `cwd`: Initial working directory (default: `/data`).
- `preset`: Execution limits preset (`STRICT`, `NORMAL`, or `PERMISSIVE`).
- `python_runtime`: Python backend owned by this sandbox: `"pyodide"` (default)
  or `"monty"`. It cannot be changed between executions.

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
sandbox.execute_python(
    code: str,
    cwd: str | None = None,
    preload_packages: list[str] | None = None,
    toolset: PythonToolset | Sequence[Callable[..., object]] | None = None,
) -> PythonResult
```

Execute Python with the backend selected by the constructor. The sandbox
filesystem is mounted at `/data` in both bash and Python environments. All
paths should use the `/data` prefix for consistency across all operations.

If `preload_packages` is provided, those Pyodide packages are loaded before
execution. No network access is granted unless preloading is requested.

If `toolset` is provided, the sandbox code can call host-side tools via `from host_tools import call` and search the declared toolset via `host_tools.search()`.

The simplest form is to pass a list of typed Python callables. LocalSandbox infers the tool name, description, input schema, and output schema from the function signature, type hints, and docstring:

```python
from localsandbox import LocalSandbox


def web_search(query: str) -> dict[str, list[str]]:
    """Search the web for information."""
    return {"results": ["result1", "result2"]}


with LocalSandbox() as sandbox:
    result = sandbox.execute_python(
        """
from host_tools import call, search

print(search("web"))
response = call("web_search", {"query": "hello"})
print(response["results"])
""",
        toolset=[web_search],
    )
```

Alternatively you can pass an explicit `PythonToolset` if you need full control over schemas, names, or timeouts.

With `python_runtime="monty"`, injected tools are called directly by name and a
default `tool_search()` function provides the same tool discovery behavior as
`host_tools.search()`. Both call the same host-side search implementation:

```python
with LocalSandbox(python_runtime="monty") as sandbox:
    result = sandbox.execute_python(
        """
matches = tool_search("web")
response = web_search(query="hello")
print(response["results"])
""",
        toolset=[web_search],
    )
```

Monty does not support third-party packages or `preload_packages`. Its
filesystem interface uses `open()` and `pathlib` with absolute `/data` paths;
per-execution working directories and `os` filesystem helpers are unavailable.
The runtime is intentionally a limited Python/standard-library subset and is
not expected to be source-compatible with Pyodide.

Call `LocalSandbox.get_python_hint("monty")` or
`LocalSandbox.get_python_hint("pyodide")` to get concise capability guidance
suitable for an agent prompt.

`execute_python()` reuses a warmed interpreter. Files under `/data` always
persist across calls. Python interpreter state may also persist across
compatible calls, but should not be depended on. Pyodide reapplies the requested
working directory for each execution; Monty supports only `/data`. Create a new
`LocalSandbox` if you need a fresh interpreter.

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
    sandbox.bash("ls -la")

    history = sandbox.history()
    for entry in history:
        print(
            f"Command: {entry.parameters['command']}, Exit: {entry.result['exitCode']}"
        )
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
        await sandbox.awrite_file("/data/tmp/test.txt", "content")
        content = await sandbox.aread_file("/data/tmp/test.txt")
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
- **Pyodide**: Full-featured Python interpreter compiled to WebAssembly
- **Monty**: Optional minimal Python interpreter for fast agent-authored code

Each bash call spawns a Deno subprocess that runs just-bash against the AgentFS database. Python execution uses a persistent Deno/Pyodide subprocess across compatible calls, communicating via line-delimited JSON over stdio. When a toolset is provided, tool calls are relayed between the sandbox and host handlers through the same protocol.

Both bash and either Python runtime share the same virtual filesystem backed by
SQLite. Monty mounts a narrowly scoped materialization at `/data`, and changes
are synchronized back into AgentFS after each execution.

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
