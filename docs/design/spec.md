# LocalSandbox Python SDK Specification

A Python SDK that wraps [just-bash](https://github.com/vercel-labs/just-bash)
and [AgentFS](https://github.com/tursodatabase/agentfs) to provide sandboxed
filesystem operations for AI agents.

## Overview

LocalSandbox enables Python-based AI agents to safely execute bash commands
within an isolated, persistent filesystem. The SDK bridges Python to the
JavaScript-based just-bash and AgentFS libraries, providing:

- Sandboxed bash command execution (no network, no binary execution)
- Persistent filesystem state via AgentFS SQLite backend
- Structured results and typed exceptions
- Automatic audit logging of all operations
- Key-value store for agent state
- Snapshot/restore for cross-session persistence

## Architecture

### Persistent Server Model

LocalSandbox uses a long-lived Deno server process per `LocalSandbox` instance.
The Python SDK talks to that server over newline-delimited JSON on stdio. Bash,
file helpers, KV, history, and Python execution all go through the same server.

```
┌─────────────────┐   NDJSON over stdio  ┌──────────────────────────────┐
│   Python SDK    │ ───────────────────► │ Deno Server (`server.ts`)    │
│   `core.py`     │ ◄─────────────────── │ just-bash + AgentFS          │
└─────────────────┘                      └──────────────┬───────────────┘
                                                        │
                                                        │ optional
                                                        ▼
                                         ┌──────────────────────────────┐
                                         │ Python Runner                │
                                         │ (`python-runner.ts`)         │
                                         │ Pyodide + bridge module      │
                                         └──────────────┬───────────────┘
                                                        │
                                                        ▼
                                         ┌──────────────────────────────┐
                                         │ AgentFS SQLite database      │
                                         │ files + KV + audit trail     │
                                         └──────────────────────────────┘
```

**How it works:**

1. Python creates a temp SQLite database file per `LocalSandbox` instance.
2. The first operation starts `shim/src/server.ts` with that database path.
3. The server opens AgentFS once and keeps it open for the sandbox lifetime.
4. Bash, file, KV, history, and snapshot checkpoint requests are handled directly in the server process.
5. The first `execute_python()` call starts a restricted runner subprocess.
6. Compatible Python calls reuse the warmed runner. If preload packages or the tool manifest change, the runner is restarted.
7. The server exits when the sandbox is destroyed.

**Why this model:**

- AgentFS provides a filesystem adapter for just-bash (`agentfs-sdk/just-bash`)
- All state (files, KV, audit trail) lives in a single SQLite file
- Snapshots are just the SQLite file bytes - trivially portable
- A persistent server amortizes Deno startup across many operations
- A persistent Python runner amortizes Pyodide startup across compatible calls

### Concurrency Model

Each `LocalSandbox` instance owns:

- one SQLite database file
- one Deno server subprocess
- zero or one Python runner subprocess

Calls on a single sandbox are serialized by the Python SDK with a re-entrant lock, so concurrent callers are safe but not parallel. Different sandboxes are fully isolated from each other.

## Project Structure

```
localsandbox/
├── localsandbox/                 # Python package
│   ├── __init__.py
│   ├── core.py            # LocalSandbox class
│   └── exceptions.py      # Exception hierarchy
├── shim/                   # TypeScript server + runner
│   ├── deno.json
│   ├── deno.lock
│   └── src/
│       ├── bridge-types.ts
│       ├── python-runner.ts
│       └── server.ts
├── tests/
└── pyproject.toml
```

## Dependencies

**Python package dependencies:**

- Python 3.12+

**Bundled with package:**

- TypeScript shim (`shim/`) - executed by Deno

**Runtime prerequisites** (user must have installed):

- Deno (with npm compatibility)
- AgentFS CLI if you want the Linux FUSE fast path for Python execution

The server and runner are run directly from TypeScript; Deno caches npm dependencies locally.

## Core API

### LocalSandbox Class

```python
from localsandbox import LocalSandbox, ExecutionPreset
from pathlib import Path

# Create a sandbox with initial files
# All paths use the /data prefix for consistency with bash and Python
sandbox = LocalSandbox(
    files={
        "/data/main.py": 'print("hello")',
        "/data/data.json": Path("./local/data.json"),  # snapshot from local
        "/data/image.png": b"\\x89PNG...",  # binary content
    },
    preset=ExecutionPreset.NORMAL,  # or STRICT, PERMISSIVE
    cwd="/data",  # default working directory
)

# Execute bash commands
result = sandbox.bash("ls -la")
result = sandbox.bash("cat main.py | head -5")
result = sandbox.bash('echo "new content" > output.txt')

# Clean up
sandbox.destroy()
```

### Constructor Parameters

| Parameter  | Type                              | Default   | Description                                                                                                                                                                       |
| ---------- | --------------------------------- | --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `files`    | `dict[str, str \| Path \| bytes]` | `{}`      | Initial filesystem contents. String values are file content, `Path` values are read and snapshotted at creation, `bytes` are written as binary. Paths should use `/data/` prefix. |
| `preset`   | `ExecutionPreset`                 | `NORMAL`  | Execution limits preset                                                                                                                                                           |
| `cwd`      | `str`                             | `'/data'` | Initial working directory                                                                                                                                                         |
| `snapshot` | `bytes \| None`                   | `None`    | Resume from a previously exported AgentFS snapshot. Mutually exclusive with `files`.                                                                                              |

### ExecutionPreset

```python
class ExecutionPreset(Enum):
    STRICT = "strict"  # 100 loop iterations, 500 commands max
    NORMAL = "normal"  # 1,000 loop iterations, 5,000 commands max
    PERMISSIVE = "permissive"  # 10,000 loop iterations, 50,000 commands max
```

`STRICT` is ultra-conservative - fail fast, never hang. Use for untrusted agent
inputs.

### BashResult

```python
@dataclass
class BashResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float
```

On success, `exit_code == 0`. On failure, a structured exception is raised
instead of returning.

### Python Execution

```python
result = sandbox.execute_python(
    "print('hello')",
    cwd="/data",
    preload_packages=["pillow"],
    toolset=toolset,
)
```

The sandbox filesystem is mounted at `/data` in both bash and Python
environments. All paths should use the `/data` prefix for consistency across all
operations.

Python execution uses a persistent Pyodide runner. Compatible calls reuse the same interpreter. Interpreter globals may therefore persist across calls, but that should be treated as an optimization, not a contract.

Optional package preloading:

```python
result = sandbox.execute_python(
    "from PIL import Image; print(Image.__name__)",
    preload_packages=["pillow"],
)
```

Optional host tool bridge:

```python
from localsandbox import LocalSandbox, PythonToolset, ToolDefinition


def echo(payload):
    return {"echo": payload["text"]}


toolset = PythonToolset(
    definitions=[
        ToolDefinition(
            name="echo",
            description="Echo text back to the caller.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"echo": {"type": "string"}},
                "required": ["echo"],
                "additionalProperties": False,
            },
            timeout_ms=5_000,
        )
    ],
    handlers={"echo": echo},
)

result = sandbox.execute_python(
    """
from host_tools import call
print(call("echo", {"text": "hello"})["echo"])
""",
    toolset=toolset,
)
```

Tool bridge behavior:

- Inputs and outputs are validated against a small JSON Schema subset.
- Per-tool timeout is defined in `ToolDefinition.timeout_ms`.
- Timeouts are best-effort. They return control to the caller promptly, but a
  synchronous host handler may continue running in the background until it
  finishes.
- Tool-enabled Python calls are logged as both `python` and
  `python_tool_call` history entries.

### PythonResult

```python
@dataclass
class PythonResult:
    stdout: str
    stderr: str
    exit_code: int
    error: str | None
```

## Exceptions

```python
class LocalSandboxError(Exception):
    """Base exception for all LocalSandbox errors"""


class CommandError(LocalSandboxError):
    """Bash command returned non-zero exit code"""

    exit_code: int
    stdout: str
    stderr: str


class FileNotFoundError(CommandError):
    """File or directory not found"""

    path: str


class PermissionError(CommandError):
    """Permission denied"""

    path: str


class TimeoutError(LocalSandboxError):
    """Command exceeded time limit"""

    timeout_ms: int


class ExecutionLimitError(LocalSandboxError):
    """Loop iteration or command count limit exceeded"""

    limit_type: str  # 'loop_iterations' | 'command_count'
    limit_value: int


class SubprocessCrashed(LocalSandboxError):
    """Shim subprocess terminated unexpectedly (OOM, segfault, killed)"""

    signal: int | None
```

Common errors (file not found, permission denied, timeout) are parsed from
stderr and raised as typed exceptions. Unknown errors raise `CommandError` with
raw stderr.

Timeouts can come from several layers:

- tool handler timeout (`ToolDefinition.timeout_ms`)
- server startup timeout
- generic request timeout
- bash request timeout
- Python execution timeout

## File Helper Methods

Convenience methods for direct filesystem access without bash:

```python
# Read file contents
content: str = sandbox.read_file("/data/main.py")

# Write file contents
sandbox.write_file("/data/output.txt", "new content")

# List directory
files: list[str] = sandbox.list_files("/data")

# Check existence
exists: bool = sandbox.exists("/data/main.py")

# Delete file
sandbox.delete_file("/data/temp.txt")
```

All paths use the `/data` prefix for consistency with bash and Python execution.
These methods invoke the shim with specific operations (not bash commands) for
direct AgentFS access.

## Snapshot & Resume

Export the sandbox state to persist across sessions:

```python
# Export current state as bytes (the AgentFS SQLite database)
snapshot: bytes = sandbox.export_snapshot()

# Store it however you want
with open("agent_state.db", "wb") as f:
    f.write(snapshot)
# Or upload to S3, Redis, database, etc.

# Later, resume from the snapshot
with open("agent_state.db", "rb") as f:
    saved_snapshot = f.read()

resumed_sandbox = LocalSandbox(snapshot=saved_snapshot)

# Continues where it left off - all files and KV state preserved
result = resumed_sandbox.bash("ls /data")
```

The `snapshot` parameter is mutually exclusive with `files`. If both are
provided, a `ValueError` is raised.

The exported snapshot includes:

- All filesystem contents
- KV store data
- Audit trail history

**Implementation:** `export_snapshot()` simply reads the SQLite database file as
bytes. Resuming writes those bytes to a new temp file and opens AgentFS with
that path.

## Key-Value Store

Separate API for agent state persistence:

```python
# String values only
sandbox.kv.set("conversation_id", "abc123")
value: str | None = sandbox.kv.get("conversation_id")
sandbox.kv.delete("conversation_id")

# List all keys
keys: list[str] = sandbox.kv.keys()
```

Values are strings only. Users must serialize/deserialize complex objects
themselves.

KV operations are sent directly to the persistent Deno server, which uses
AgentFS's built-in KV store.

## Async Support

Core API is synchronous. Async wrapper provided for async frameworks:

```python
from localsandbox import LocalSandbox
import asyncio

sandbox = LocalSandbox(files={...})

# Sync usage
result = sandbox.bash("ls")

# Async wrapper (runs in thread pool)
result = await sandbox.abash("ls")
await sandbox.adestroy()
```

The async methods use `asyncio.to_thread()` internally.

## Audit Logging

Operations are automatically logged to AgentFS's toolcall audit trail:

- `bash` executions
- direct file helper operations
- KV operations
- `python` executions
- `python_tool_call` entries for host tool bridge activity

No opt-out. This provides debugging and observability for agent workflows.

## Lifecycle

### Creation

```python
sandbox = LocalSandbox(files={...})
```

- SQLite database file created in temp directory
- If `files` provided: server seeds initial files into AgentFS
- If `snapshot` provided: bytes written to temp file, AgentFS opens it
- Local `Path` references read immediately (not lazily)

### Usage

```python
result = sandbox.bash("...")
```

- First call starts the persistent Deno server
- Server opens AgentFS with the SQLite database path
- Bash runs inside just-bash against AgentFS
- File/KV/history calls execute directly in the server
- Python execution starts or reuses the Pyodide runner
- Changes persist to SQLite automatically

### Destruction

```python
sandbox.destroy()
```

- SQLite database file deleted
- Server subprocess stopped
- Python runner subprocess stopped if present
- Instance marked as destroyed
- Subsequent operations raise `RuntimeError`

Instances are **one-shot only**. After `destroy()`, create a new `LocalSandbox`
instance.

### Expected Lifespan

Designed for **per-conversation** usage: create a sandbox at conversation start,
accumulate state across agent turns, destroy at conversation end.

For **cross-session persistence**, export a snapshot before destroying and
resume from it later.

## Network Access

**Disabled in bash and disabled by default in Python.** No `curl`, `wget`, or
network commands in bash execution. The Python runner has no network access
unless `preload_packages` is provided, in which case network access is limited
to fetching Pyodide packages.

If agents need network access during tool-enabled Python execution, expose that
capability explicitly through a host tool handler.

## Limitations

### No Custom Commands

just-bash's `defineCommand()` for custom TypeScript handlers is not exposed. Use
the 70+ built-in commands only.

### No Binary Execution

just-bash cannot execute actual binaries or WASM. Commands like `grep`, `sed`,
`awk` are TypeScript reimplementations.

### Warmup Costs

The first operation on a sandbox starts the Deno server. The first compatible
Python execution also pays Pyodide startup cost. Later calls are much cheaper,
but changing preload packages or the Python tool manifest restarts the runner.

### SQLite File Location

AgentFS SQLite files are created in the system temp directory. Users cannot
currently specify a custom location.

## Server Protocol

The Python SDK sends NDJSON request envelopes to `server.ts`:

- `bash`
- `seed`
- `read_file`
- `write_file`
- `list_files`
- `exists`
- `delete_file`
- `kv_get`
- `kv_set`
- `kv_delete`
- `kv_keys`
- `checkpoint`
- `history`
- `execute_python`
- `shutdown`

The server responds with `result` or `error` envelopes. Python execution can
also interleave `tool_call` envelopes before a final `result`.

## Usage with Agent Frameworks

LocalSandbox methods are plain Python functions. Users wrap them with their
framework's tool decorators:

### With LangChain

```python
from langchain.tools import tool
from localsandbox import LocalSandbox

sandbox = LocalSandbox(files={...})


@tool
def execute_bash(command: str) -> str:
    """Execute a bash command in the sandbox."""
    result = sandbox.bash(command)
    return result.stdout


@tool
def read_sandbox_file(path: str) -> str:
    """Read a file from the sandbox."""
    return sandbox.read_file(path)
```

### With Claude/Anthropic

```python
tools = [
    {
        "name": "bash",
        "description": "Execute bash commands in sandbox",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }
]


def handle_tool_call(name, input):
    if name == "bash":
        return sandbox.bash(input["command"]).stdout
```

## Example: Code Analysis Agent

```python
from localsandbox import LocalSandbox, ExecutionPreset
from pathlib import Path

# Create sandbox with project files
# All paths use /data prefix for consistency
sandbox = LocalSandbox(
    files={
        "/data/project/src/main.py": Path("./src/main.py"),
        "/data/project/src/utils.py": Path("./src/utils.py"),
        "/data/project/tests/test_main.py": Path("./tests/test_main.py"),
    },
    preset=ExecutionPreset.NORMAL,
)

# Agent can now explore
result = sandbox.bash('find /data/project -name "*.py" | head -20')
result = sandbox.bash('grep -r "def " /data/project/src | wc -l')
result = sandbox.bash("cat /data/project/src/main.py | head -50")

# Agent can write analysis
sandbox.bash('echo "# Analysis Report" > /data/project/analysis.md')
sandbox.bash(
    'echo "Found $(grep -r "TODO" /data/project | wc -l) TODOs" >> /data/project/analysis.md'
)

# Retrieve results
report = sandbox.read_file("/data/project/analysis.md")

sandbox.destroy()
```

## Example: Persistent Agent State

```python
import redis
from localsandbox import LocalSandbox

r = redis.Redis()


def get_or_create_sandbox(session_id: str) -> LocalSandbox:
    """Resume existing sandbox or create new one."""
    snapshot = r.get(f"sandbox:{session_id}")
    if snapshot:
        return LocalSandbox(snapshot=snapshot)
    return LocalSandbox(files={"/data/workspace/notes.txt": ""})


def save_sandbox(session_id: str, sandbox: LocalSandbox):
    """Persist sandbox state to Redis."""
    snapshot = sandbox.export_snapshot()
    r.set(f"sandbox:{session_id}", snapshot, ex=86400)  # 24h TTL
    sandbox.destroy()


# Usage in an agent loop
sandbox = get_or_create_sandbox("user-123")
sandbox.bash('echo "Meeting notes from today" >> /data/workspace/notes.txt')
save_sandbox("user-123", sandbox)

# Later, in another process/session
sandbox = get_or_create_sandbox("user-123")
result = sandbox.bash("cat /data/workspace/notes.txt")  # Contains previous notes
```

## Testing

No mock implementation provided. Test against real sandboxes:

```python
import pytest
from localsandbox import LocalSandbox


def test_file_creation():
    sandbox = LocalSandbox()
    sandbox.bash('echo "test" > /data/test.txt')

    content = sandbox.read_file("/data/test.txt")
    assert content.strip() == "test"

    sandbox.destroy()


@pytest.fixture
def sandbox():
    s = LocalSandbox(files={"/data/data.txt": "initial"})
    yield s
    s.destroy()


def test_with_fixture(sandbox):
    result = sandbox.bash("cat /data/data.txt")
    assert result.stdout.strip() == "initial"
```

## Future Considerations (Not in v1)

- Custom SQLite file locations
- Network access with URL allowlists
- Custom command definitions via JS files
- Lazy file loading for large filesystems
- Mock implementation for faster tests
- Streaming output for long-running commands
- Persistent shim process for reduced latency (IPC instead of subprocess per
  call)
