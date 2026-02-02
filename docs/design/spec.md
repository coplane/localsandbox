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

### TypeScript Shim Model

LocalSandbox uses a Deno-based TypeScript CLI shim (`localsandbox-shim`) that
bridges Python to just-bash and AgentFS:

```
┌─────────────────┐     subprocess      ┌─────────────────────────────────┐
│   Python SDK    │ ─────────────────── │  TypeScript Shim (localsandbox-shim)  │
│   (localsandbox.py)   │     JSON stdio      │  just-bash + agentfs-sdk        │
└─────────────────┘                     └─────────────────────────────────┘
        │                                              │
        │                                              │
        ▼                                              ▼
┌─────────────────┐                     ┌─────────────────────────────────┐
│  Temp SQLite    │◄────────────────────│  AgentFS (filesystem + KV +     │
│  Database File  │   persistence       │  audit trail)                   │
└─────────────────┘                     └─────────────────────────────────┘
```

**How it works:**

1. Python creates a temp SQLite database file per `LocalSandbox` instance
2. Each `bash()` call invokes the shim CLI with the database path
3. The shim opens AgentFS with that database, creates a just-bash instance with
   the AgentFS filesystem
4. Command executes, changes persist to SQLite automatically
5. Shim returns JSON result to Python

**Why this model:**

- AgentFS provides a filesystem adapter for just-bash (`agentfs-sdk/just-bash`)
- All state (files, KV, audit trail) lives in a single SQLite file
- Snapshots are just the SQLite file bytes - trivially portable
- The shim is bundled with the Python package and run directly by Deno

### Concurrency Model

Each `LocalSandbox` instance owns its own SQLite database file. No sharing
between instances, no race conditions. Users requiring concurrent access should
create separate sandbox instances.

## Project Structure

```
localsandbox/
├── localsandbox/                 # Python package
│   ├── __init__.py
│   ├── core.py            # LocalSandbox class
│   └── exceptions.py      # Exception hierarchy
├── shim/                   # TypeScript CLI shim
│   ├── deno.json
│   ├── deno.lock
│   └── src/
│       ├── cli.ts         # CLI entry point
│       ├── python.ts      # Python execution orchestration
│       └── python-runner.ts # Isolated Python runner
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

The shim is run directly from TypeScript; Deno caches npm dependencies locally.

## Core API

### LocalSandbox Class

```python
from localsandbox import LocalSandbox, ExecutionPreset
from pathlib import Path

# Create a sandbox with initial files
# All paths use the /data prefix for consistency with bash and Python
sandbox = LocalSandbox(
    files={
        '/data/main.py': 'print("hello")',
        '/data/data.json': Path('./local/data.json'),  # snapshot from local
        '/data/image.png': b'\\x89PNG...',             # binary content
    },
    preset=ExecutionPreset.NORMAL,  # or STRICT, PERMISSIVE
    cwd='/data',  # default working directory
)

# Execute bash commands
result = sandbox.bash('ls -la')
result = sandbox.bash('cat main.py | head -5')
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
    STRICT = "strict"       # 100 loop iterations, 500 commands max
    NORMAL = "normal"       # 1,000 loop iterations, 5,000 commands max
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
result = sandbox.execute_python("print('hello')", cwd="/data")
```

The sandbox filesystem is mounted at `/data` in both bash and Python
environments. All paths should use the `/data` prefix for consistency across all
operations.

Optional package preloading:

```python
result = sandbox.execute_python(
    "from PIL import Image; print(Image.__name__)",
    preload_packages=["pillow"],
)
```

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

## File Helper Methods

Convenience methods for direct filesystem access without bash:

```python
# Read file contents
content: str = sandbox.read_file('/data/main.py')

# Write file contents
sandbox.write_file('/data/output.txt', 'new content')

# List directory
files: list[str] = sandbox.list_files('/data')

# Check existence
exists: bool = sandbox.exists('/data/main.py')

# Delete file
sandbox.delete_file('/data/temp.txt')
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
with open('agent_state.db', 'wb') as f:
    f.write(snapshot)
# Or upload to S3, Redis, database, etc.

# Later, resume from the snapshot
with open('agent_state.db', 'rb') as f:
    saved_snapshot = f.read()

resumed_sandbox = LocalSandbox(snapshot=saved_snapshot)

# Continues where it left off - all files and KV state preserved
result = resumed_sandbox.bash('ls /data')
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
sandbox.kv.set('conversation_id', 'abc123')
value: str | None = sandbox.kv.get('conversation_id')
sandbox.kv.delete('conversation_id')

# List all keys
keys: list[str] = sandbox.kv.keys()
```

Values are strings only. Users must serialize/deserialize complex objects
themselves.

KV operations invoke the shim with specific KV commands, which use AgentFS's
built-in KV store.

## Async Support

Core API is synchronous. Async wrapper provided for async frameworks:

```python
from localsandbox import LocalSandbox
import asyncio

sandbox = LocalSandbox(files={...})

# Sync usage
result = sandbox.bash('ls')

# Async wrapper (runs in thread pool)
result = await sandbox.abash('ls')
await sandbox.adestroy()
```

The async methods use `asyncio.to_thread()` internally.

## Audit Logging

All bash executions are automatically logged to AgentFS's toolcall audit trail:

- Command executed
- Timestamp (start/end)
- Duration
- Exit code
- Stdout/stderr (truncated if large)

No opt-out. This provides debugging and observability for agent workflows.

## Lifecycle

### Creation

```python
sandbox = LocalSandbox(files={...})
```

- SQLite database file created in temp directory
- If `files` provided: shim seeds initial files into AgentFS
- If `snapshot` provided: bytes written to temp file, AgentFS opens it
- Local `Path` references read immediately (not lazily)

### Usage

```python
result = sandbox.bash('...')
```

- Each call spawns the shim CLI subprocess
- Shim opens AgentFS with the SQLite database path
- Creates just-bash instance with AgentFS filesystem
- Command executes in just-bash sandbox
- Changes persist to SQLite automatically (AgentFS handles this)
- Shim returns JSON result, subprocess exits

### Destruction

```python
sandbox.destroy()
```

- SQLite database file deleted
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

**Disabled entirely.** No `curl`, `wget`, or network commands in bash or Python
execution. The sandbox is pure filesystem operations only.

If agents need network access, make HTTP calls in the host Python process and
write results to the sandbox filesystem.

## Limitations

### No Custom Commands

just-bash's `defineCommand()` for custom TypeScript handlers is not exposed. Use
the 70+ built-in commands only.

### No Binary Execution

just-bash cannot execute actual binaries or WASM. Commands like `grep`, `sed`,
`awk` are TypeScript reimplementations.

### Subprocess Latency

Each `bash()` call spawns a Deno process. Expect ~50-200ms overhead per call.
Batch operations when possible:

```python
# Prefer this
result = sandbox.bash('ls && cat file1.txt && cat file2.txt')

# Over this
sandbox.bash('ls')
sandbox.bash('cat file1.txt')
sandbox.bash('cat file2.txt')
```

### SQLite File Location

AgentFS SQLite files are created in the system temp directory. Users cannot
currently specify a custom location.

## Shim CLI Interface

The TypeScript shim (`localsandbox-shim`) accepts commands via CLI arguments:

```bash
# Execute bash command
deno run --allow-read --allow-write --allow-env --allow-ffi --allow-run \
  shim/src/cli.ts bash --db /tmp/localsandbox.db --cwd /home/user --command "ls -la"

# Seed initial files (called once at LocalSandbox creation)
deno run --allow-read --allow-write --allow-env --allow-ffi --allow-run \
  shim/src/cli.ts seed --db /tmp/localsandbox.db --files '{"path": "content", ...}'

# File operations
deno run --allow-read --allow-write --allow-env --allow-ffi --allow-run \
  shim/src/cli.ts read-file --db /tmp/localsandbox.db --path /home/user/file.txt
deno run --allow-read --allow-write --allow-env --allow-ffi --allow-run \
  shim/src/cli.ts write-file --db /tmp/localsandbox.db --path /home/user/file.txt --content "..."
deno run --allow-read --allow-write --allow-env --allow-ffi --allow-run \
  shim/src/cli.ts list-files --db /tmp/localsandbox.db --path /home/user
deno run --allow-read --allow-write --allow-env --allow-ffi --allow-run \
  shim/src/cli.ts exists --db /tmp/localsandbox.db --path /home/user/file.txt
deno run --allow-read --allow-write --allow-env --allow-ffi --allow-run \
  shim/src/cli.ts delete-file --db /tmp/localsandbox.db --path /home/user/file.txt

# KV operations
deno run --allow-read --allow-write --allow-env --allow-ffi --allow-run \
  shim/src/cli.ts kv-get --db /tmp/localsandbox.db --key mykey
deno run --allow-read --allow-write --allow-env --allow-ffi --allow-run \
  shim/src/cli.ts kv-set --db /tmp/localsandbox.db --key mykey --value myvalue
deno run --allow-read --allow-write --allow-env --allow-ffi --allow-run \
  shim/src/cli.ts kv-delete --db /tmp/localsandbox.db --key mykey
deno run --allow-read --allow-write --allow-env --allow-ffi --allow-run \
  shim/src/cli.ts kv-keys --db /tmp/localsandbox.db
```

All commands output JSON to stdout.

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
            "properties": {
                "command": {"type": "string"}
            },
            "required": ["command"]
        }
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
        '/data/project/src/main.py': Path('./src/main.py'),
        '/data/project/src/utils.py': Path('./src/utils.py'),
        '/data/project/tests/test_main.py': Path('./tests/test_main.py'),
    },
    preset=ExecutionPreset.NORMAL,
)

# Agent can now explore
result = sandbox.bash('find /data/project -name "*.py" | head -20')
result = sandbox.bash('grep -r "def " /data/project/src | wc -l')
result = sandbox.bash('cat /data/project/src/main.py | head -50')

# Agent can write analysis
sandbox.bash('echo "# Analysis Report" > /data/project/analysis.md')
sandbox.bash('echo "Found $(grep -r "TODO" /data/project | wc -l) TODOs" >> /data/project/analysis.md')

# Retrieve results
report = sandbox.read_file('/data/project/analysis.md')

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
    return LocalSandbox(files={'/data/workspace/notes.txt': ''})

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
result = sandbox.bash('cat /data/workspace/notes.txt')  # Contains previous notes
```

## Testing

No mock implementation provided. Test against real sandboxes:

```python
import pytest
from localsandbox import LocalSandbox

def test_file_creation():
    sandbox = LocalSandbox()
    sandbox.bash('echo "test" > /data/test.txt')

    content = sandbox.read_file('/data/test.txt')
    assert content.strip() == "test"

    sandbox.destroy()

@pytest.fixture
def sandbox():
    s = LocalSandbox(files={'/data/data.txt': 'initial'})
    yield s
    s.destroy()

def test_with_fixture(sandbox):
    result = sandbox.bash('cat /data/data.txt')
    assert result.stdout.strip() == 'initial'
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
