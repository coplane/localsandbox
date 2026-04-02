# Pyodide Tool Bridge Design

## Context

The OpenAI computer-use guide's "code execution harness" example uses an
intentionally simple pattern where host helpers are injected into an `exec()`
global context. That is convenient, but the capability boundary is implicit and
harder to audit.

For LocalSandbox, we want a more explicit pattern: sandboxed Python imports a
known bridge module and invokes named tool calls through that bridge. This
keeps the current per-tool RPC model, but makes the crossing point explicit.

This proposal does **not** attempt to implement Cloudflare-style fixed-surface
discovery yet. The bridge should be compatible with adding helpers such as
`search_tools` later, but v1 keeps the existing per-tool model.

## Goals

1. **Explicit boundary**: tool calls happen through an importable bridge module,
   not implicit globals.
2. **Unified execution path**: all `execute_python()` calls use the same
   persistent subprocess, with or without tools.
3. **Persistent runtime**: the Pyodide process stays alive across calls within
   a sandbox, avoiding repeated cold starts.
4. **Least privilege dispatch**: sandbox code can call only declared tools.
5. **Schema validation**: validate input and output payloads around host
   execution.
6. **Deterministic audit trail**: record tool invocations as structured history.

## Non-goals

- Adding Cloudflare-style fixed-surface discovery in v1.
- Running arbitrary host Python objects inside Pyodide.
- Granting unrestricted `js` access to Deno or Node APIs.
- Supporting concurrent tool calls in v1.

## API Proposal (Python SDK)

Add an optional `toolset` argument to Python execution:

```python
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
ToolHandler = Callable[[dict[str, JsonValue]], JsonValue | Awaitable[JsonValue]]

@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    timeout_ms: int = 30_000

@dataclass(frozen=True)
class PythonToolset:
    definitions: list[ToolDefinition]
    handlers: dict[str, ToolHandler]

result = sandbox.execute_python(
    code=agent_code,
    toolset=toolset,
)
```

When `toolset` is not provided, `execute_python()` behaves exactly as it does
today.

## In-sandbox Usage

The runner registers a bridge module named `hosttools` with a synchronous
entrypoint (backed by Pyodide's `run_sync`):

```python
from hosttools import call

results = call("web_search", {"query": "doctors in Vancouver"})
```

Optionally, LocalSandbox can generate a helper module for convenience:

```python
from localsandbox_tools import tools

results = tools.web_search("doctors in Vancouver")
```

The helper module is syntax sugar over `hosttools.call()` and does not change
the transport or security model.

## Session Architecture

### Shape

All Python execution (with or without tools) uses a **persistent subprocess
scoped to the `LocalSandbox` lifetime**:

1. The first `execute_python()` call starts the shim and runner subprocesses.
2. The runner loads Pyodide, mounts the filesystem, and registers `hosttools`.
3. Each `execute_python()` call sends code via the bridge protocol.
4. The runner executes the code and returns a `complete` envelope.
5. The subprocess stays alive for subsequent calls (same Pyodide instance).
6. When the sandbox is destroyed, the subprocess exits.

If the toolset changes between calls, the subprocess is restarted. Python
global state (variables, imports) persists across calls within the same
subprocess.

### Ownership

- **Python SDK**
  - Owns `ToolDefinition` and `ToolHandler`.
  - Validates payloads before and after handler execution.
  - Enforces per-tool timeout policy.
  - Manages the persistent subprocess lifecycle.
  - Returns the final `PythonResult`.
- **Shim**
  - Owns process lifecycle for the runner.
  - Relays bridge envelopes between SDK and runner.
  - Records history for the overall Python execution and per-tool calls.
  - Manages filesystem sync (AgentFS to/from temp dir) on each execution.
- **Runner**
  - Owns the Pyodide runtime (persistent singleton).
  - Registers `hosttools`.
  - Blocks Python execution via `run_sync` while waiting for each tool result.

## Wire Protocol

Use newline-delimited JSON envelopes over stdio.

### Envelope types

- `start` (sdk -> shim)
  - First request: `code`, `cwd`, `preload_packages`, and tool manifest.
- `execute` (sdk -> shim)
  - Subsequent request on an existing session: `code`, `cwd`,
    `preload_packages`. Reuses the tools from the `start` envelope.
- `tool_call` (runner -> sdk, forwarded by shim)
  - Sandbox requested tool execution.
- `tool_result` (sdk -> runner, forwarded by shim)
  - Successful tool return value.
- `tool_error` (sdk -> runner, forwarded by shim)
  - Typed error: `validation_error`, `permission_error`, `timeout`,
    `internal_error`.
- `complete` (runner -> sdk, forwarded by shim)
  - Final `stdout`, `stderr`, `exit_code`, and optional top-level error.
- `fatal_error` (shim -> sdk)
  - Bridge-level failure before normal completion.

### Example (multi-execution session)

```json
{"type":"start","code":"x = 1","cwd":"/data","tools":[{"name":"web_search"}]}
{"type":"complete","stdout":"","stderr":"","exit_code":0}
{"type":"execute","code":"print(x)","cwd":"/data"}
{"type":"complete","stdout":"1\n","stderr":"","exit_code":0}
```

Tool calls within an execution:

```json
{"type":"start","code":"...","cwd":"/data","tools":[{"name":"web_search"}]}
{"type":"tool_call","id":"t1","name":"web_search","payload":{"query":"doctors in Vancouver"}}
{"type":"tool_result","id":"t1","payload":{"results":[{"title":"..."}]}}
{"type":"complete","stdout":"","stderr":"","exit_code":0}
```

## Execution Flow

### First call (subprocess startup)

1. `LocalSandbox.execute_python(code)` starts a persistent shim subprocess.
2. The SDK sends a `start` envelope with code, cwd, preload packages, and
   tool definitions (empty if no toolset).
3. The shim mounts the filesystem (FUSE or sync) and starts `python-runner`.
4. The runner loads Pyodide, mounts NODEFS, and registers `hosttools`.
5. The runner executes the code. If tools are called, envelopes are relayed
   through the shim to the SDK and back.
6. The runner emits `complete`. The SDK returns `PythonResult`.
7. The subprocess stays alive.

### Subsequent calls (reuse)

1. `LocalSandbox.execute_python(code)` sends an `execute` envelope.
2. The runner resets stdout/stderr, updates cwd if needed, and runs the code.
3. Tool calls are relayed as before.
4. The runner emits `complete`. The subprocess stays alive.

### Toolset change

If `toolset` differs from the previous call, the SDK tears down the existing
subprocess and starts a new one with a fresh `start` envelope.

### Shutdown

When `LocalSandbox.destroy()` is called (or the context manager exits), the
SDK closes stdin on the subprocess. The runner unmounts the filesystem and
exits.

## Security Controls

1. **Allowlist-only dispatch**
   - Reject undeclared tool names before any handler executes.
2. **Schema checks**
   - Validate payload against `input_schema`.
   - Validate tool return value against `output_schema` when present.
3. **Per-tool timeout**
   - Each handler gets a configured timeout policy.
   - v1 documents best-effort behavior for synchronous handlers.
4. **Payload size limits**
   - Bound request and response byte size to prevent memory abuse.
5. **No host object injection**
   - Runner exposes only `hosttools.call(name, payload)` as the supported host
     bridge.
6. **Preserve existing Deno permission boundary**
   - The bridge does not replace the current filesystem and network isolation.
   - `import js` remains constrained by the runner's Deno permissions.
7. **History recording**
   - Record the overall Python execution.
   - Record each `python_tool_call` with tool name, latency, success/failure,
     and truncated payload metadata.

## History Model

The current history API records one top-level `"python"` event per execution.
Tool-enabled execution should add nested or related `python_tool_call` records.

For v1, keep the model simple:

- Continue recording one `"python"` entry for the overall execution.
- Add one `python_tool_call` entry per tool invocation.
- Include a shared `session_id` in history parameters so related entries can be
  grouped without changing the public `HistoryEntry` shape immediately.

This avoids blocking the bridge on a larger history API redesign.

## Open Questions

1. Should a future version require async handlers or a separate worker model for
   strict timeout enforcement?
2. What payload size limit should apply before the bridge returns
   `validation_error` or `internal_error`?
3. Should generated helper modules include schemas and docstrings for better
   agent ergonomics?
