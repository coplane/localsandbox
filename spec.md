# BashFS Python SDK Specification

A Python SDK that wraps [just-bash](https://github.com/vercel-labs/just-bash) and [AgentFS](https://github.com/tursodatabase/agentfs) to provide sandboxed filesystem operations for AI agents.

## Overview

BashFS enables Python-based AI agents to safely execute bash commands within an isolated, persistent filesystem. The SDK bridges Python to the JavaScript-based just-bash and AgentFS libraries, providing:

- Sandboxed bash command execution (no network, no binary execution)
- Persistent filesystem state via AgentFS SQLite backend
- Structured results and typed exceptions
- Automatic audit logging of all operations
- Key-value store for agent state
- Snapshot/restore for cross-session persistence

## Architecture

### TypeScript Shim Model

BashFS uses a TypeScript CLI shim (`bashfs-shim`) that bridges Python to the Node.js ecosystem:

```
┌─────────────────┐     subprocess      ┌─────────────────────────────────┐
│   Python SDK    │ ─────────────────── │  TypeScript Shim (bashfs-shim)  │
│   (bashfs.py)   │     JSON stdio      │  just-bash + agentfs-sdk        │
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
1. Python creates a temp SQLite database file per `BashFS` instance
2. Each `bash()` call invokes the shim CLI with the database path
3. The shim opens AgentFS with that database, creates a just-bash instance with the AgentFS filesystem
4. Command executes, changes persist to SQLite automatically
5. Shim returns JSON result to Python

**Why this model:**
- AgentFS provides a filesystem adapter for just-bash (`agentfs-sdk/just-bash`)
- All state (files, KV, audit trail) lives in a single SQLite file
- Snapshots are just the SQLite file bytes - trivially portable
- The shim is bundled with the Python package (no global npm install required)

### Concurrency Model

Each `BashFS` instance owns its own SQLite database file. No sharing between instances, no race conditions. Users requiring concurrent access should create separate sandbox instances.

## Project Structure

```
bashfs-py/
├── bashfs/                 # Python package
│   ├── __init__.py
│   ├── core.py            # BashFS class
│   └── exceptions.py      # Exception hierarchy
├── shim/                   # TypeScript CLI shim
│   ├── package.json
│   ├── tsconfig.json
│   └── src/
│       └── cli.ts         # CLI entry point
├── tests/
└── pyproject.toml
```

## Dependencies

**Python package dependencies:**
- Python 3.12+

**Bundled with package:**
- TypeScript shim (`shim/`) - installed via `pnpm install` during development
- Requires Node.js 18+ at runtime

**Runtime prerequisites** (user must have installed):
- Node.js 18+

The shim's node_modules are bundled or the shim is compiled to a standalone script, so users don't need to run npm/pnpm install.

## Core API

### BashFS Class

```python
from bashfs import BashFS, ExecutionPreset
from pathlib import Path

# Create a sandbox with initial files
sandbox = BashFS(
    files={
        '/home/user/main.py': 'print("hello")',
        '/home/user/data.json': Path('./local/data.json'),  # snapshot from local
        '/home/user/image.png': b'\\x89PNG...',             # binary content
    },
    preset=ExecutionPreset.NORMAL,  # or STRICT, PERMISSIVE
    cwd='/home/user',  # default
)

# Execute bash commands
result = sandbox.bash('ls -la')
result = sandbox.bash('cat main.py | head -5')
result = sandbox.bash('echo "new content" > output.txt')

# Clean up
sandbox.destroy()
```

### Constructor Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `files` | `dict[str, str \| Path \| bytes]` | `{}` | Initial filesystem contents. String values are file content, `Path` values are read and snapshotted at creation, `bytes` are written as binary. |
| `preset` | `ExecutionPreset` | `NORMAL` | Execution limits preset |
| `cwd` | `str` | `'/home/user'` | Initial working directory |
| `snapshot` | `bytes \| None` | `None` | Resume from a previously exported AgentFS snapshot. Mutually exclusive with `files`. |

### ExecutionPreset

```python
class ExecutionPreset(Enum):
    STRICT = "strict"       # 100 loop iterations, 500 commands max
    NORMAL = "normal"       # 1,000 loop iterations, 5,000 commands max
    PERMISSIVE = "permissive"  # 10,000 loop iterations, 50,000 commands max
```

`STRICT` is ultra-conservative - fail fast, never hang. Use for untrusted agent inputs.

### BashResult

```python
@dataclass
class BashResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float
```

On success, `exit_code == 0`. On failure, a structured exception is raised instead of returning.

## Exceptions

```python
class BashFSError(Exception):
    """Base exception for all BashFS errors"""

class CommandError(BashFSError):
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

class TimeoutError(BashFSError):
    """Command exceeded time limit"""
    timeout_ms: int

class ExecutionLimitError(BashFSError):
    """Loop iteration or command count limit exceeded"""
    limit_type: str  # 'loop_iterations' | 'command_count'
    limit_value: int

class SubprocessCrashed(BashFSError):
    """Node subprocess terminated unexpectedly (OOM, segfault, killed)"""
    signal: int | None
```

Common errors (file not found, permission denied, timeout) are parsed from stderr and raised as typed exceptions. Unknown errors raise `CommandError` with raw stderr.

## File Helper Methods

Convenience methods for direct filesystem access without bash:

```python
# Read file contents
content: str = sandbox.read_file('/home/user/main.py')

# Write file contents
sandbox.write_file('/home/user/output.txt', 'new content')

# List directory
files: list[str] = sandbox.list_files('/home/user')

# Check existence
exists: bool = sandbox.exists('/home/user/main.py')

# Delete file
sandbox.delete_file('/home/user/temp.txt')
```

These methods invoke the shim with specific operations (not bash commands) for direct AgentFS access.

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

resumed_sandbox = BashFS(snapshot=saved_snapshot)

# Continues where it left off - all files and KV state preserved
result = resumed_sandbox.bash('ls /home/user')
```

The `snapshot` parameter is mutually exclusive with `files`. If both are provided, a `ValueError` is raised.

The exported snapshot includes:
- All filesystem contents
- KV store data
- Audit trail history

**Implementation:** `export_snapshot()` simply reads the SQLite database file as bytes. Resuming writes those bytes to a new temp file and opens AgentFS with that path.

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

Values are strings only. Users must serialize/deserialize complex objects themselves.

KV operations invoke the shim with specific KV commands, which use AgentFS's built-in KV store.

## Async Support

Core API is synchronous. Async wrapper provided for async frameworks:

```python
from bashfs import BashFS
import asyncio

sandbox = BashFS(files={...})

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
sandbox = BashFS(files={...})
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

Instances are **one-shot only**. After `destroy()`, create a new `BashFS` instance.

### Expected Lifespan

Designed for **per-conversation** usage: create a sandbox at conversation start, accumulate state across agent turns, destroy at conversation end.

For **cross-session persistence**, export a snapshot before destroying and resume from it later.

## Network Access

**Disabled entirely.** No `curl`, `wget`, or network commands. The sandbox is pure filesystem operations only.

If agents need network access, make HTTP calls in Python and write results to the sandbox filesystem.

## Limitations

### No Custom Commands

just-bash's `defineCommand()` for custom TypeScript handlers is not exposed. Use the 70+ built-in commands only.

### No Binary Execution

just-bash cannot execute actual binaries or WASM. Commands like `grep`, `sed`, `awk` are TypeScript reimplementations.

### Subprocess Latency

Each `bash()` call spawns a Node process. Expect ~50-200ms overhead per call. Batch operations when possible:

```python
# Prefer this
result = sandbox.bash('ls && cat file1.txt && cat file2.txt')

# Over this
sandbox.bash('ls')
sandbox.bash('cat file1.txt')
sandbox.bash('cat file2.txt')
```

### SQLite File Location

AgentFS SQLite files are created in the system temp directory. Users cannot currently specify a custom location.

## Shim CLI Interface

The TypeScript shim (`bashfs-shim`) accepts commands via CLI arguments:

```bash
# Execute bash command
node shim/dist/cli.js bash --db /tmp/bashfs.db --cwd /home/user --command "ls -la"

# Seed initial files (called once at BashFS creation)
node shim/dist/cli.js seed --db /tmp/bashfs.db --files '{"path": "content", ...}'

# File operations
node shim/dist/cli.js read-file --db /tmp/bashfs.db --path /home/user/file.txt
node shim/dist/cli.js write-file --db /tmp/bashfs.db --path /home/user/file.txt --content "..."
node shim/dist/cli.js list-files --db /tmp/bashfs.db --path /home/user
node shim/dist/cli.js exists --db /tmp/bashfs.db --path /home/user/file.txt
node shim/dist/cli.js delete-file --db /tmp/bashfs.db --path /home/user/file.txt

# KV operations
node shim/dist/cli.js kv-get --db /tmp/bashfs.db --key mykey
node shim/dist/cli.js kv-set --db /tmp/bashfs.db --key mykey --value myvalue
node shim/dist/cli.js kv-delete --db /tmp/bashfs.db --key mykey
node shim/dist/cli.js kv-keys --db /tmp/bashfs.db
```

All commands output JSON to stdout.

## Usage with Agent Frameworks

BashFS methods are plain Python functions. Users wrap them with their framework's tool decorators:

### With LangChain

```python
from langchain.tools import tool
from bashfs import BashFS

sandbox = BashFS(files={...})

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
from bashfs import BashFS, ExecutionPreset
from pathlib import Path

# Create sandbox with project files
sandbox = BashFS(
    files={
        '/project/src/main.py': Path('./src/main.py'),
        '/project/src/utils.py': Path('./src/utils.py'),
        '/project/tests/test_main.py': Path('./tests/test_main.py'),
    },
    preset=ExecutionPreset.NORMAL,
)

# Agent can now explore
result = sandbox.bash('find /project -name "*.py" | head -20')
result = sandbox.bash('grep -r "def " /project/src | wc -l')
result = sandbox.bash('cat /project/src/main.py | head -50')

# Agent can write analysis
sandbox.bash('echo "# Analysis Report" > /project/analysis.md')
sandbox.bash('echo "Found $(grep -r "TODO" /project | wc -l) TODOs" >> /project/analysis.md')

# Retrieve results
report = sandbox.read_file('/project/analysis.md')

sandbox.destroy()
```

## Example: Persistent Agent State

```python
import redis
from bashfs import BashFS

r = redis.Redis()

def get_or_create_sandbox(session_id: str) -> BashFS:
    """Resume existing sandbox or create new one."""
    snapshot = r.get(f"sandbox:{session_id}")
    if snapshot:
        return BashFS(snapshot=snapshot)
    return BashFS(files={'/workspace/notes.txt': ''})

def save_sandbox(session_id: str, sandbox: BashFS):
    """Persist sandbox state to Redis."""
    snapshot = sandbox.export_snapshot()
    r.set(f"sandbox:{session_id}", snapshot, ex=86400)  # 24h TTL
    sandbox.destroy()

# Usage in an agent loop
sandbox = get_or_create_sandbox("user-123")
sandbox.bash('echo "Meeting notes from today" >> /workspace/notes.txt')
save_sandbox("user-123", sandbox)

# Later, in another process/session
sandbox = get_or_create_sandbox("user-123")
result = sandbox.bash('cat /workspace/notes.txt')  # Contains previous notes
```

## Testing

No mock implementation provided. Test against real sandboxes:

```python
import pytest
from bashfs import BashFS

def test_file_creation():
    sandbox = BashFS()
    sandbox.bash('echo "test" > /home/user/test.txt')

    content = sandbox.read_file('/home/user/test.txt')
    assert content.strip() == "test"

    sandbox.destroy()

@pytest.fixture
def sandbox():
    s = BashFS(files={'/home/user/data.txt': 'initial'})
    yield s
    s.destroy()

def test_with_fixture(sandbox):
    result = sandbox.bash('cat /home/user/data.txt')
    assert result.stdout.strip() == 'initial'
```

## Future Considerations (Not in v1)

- Custom SQLite file locations
- Network access with URL allowlists
- Custom command definitions via JS files
- Lazy file loading for large filesystems
- Mock implementation for faster tests
- Streaming output for long-running commands
- Persistent Node process for reduced latency (IPC instead of subprocess per call)
