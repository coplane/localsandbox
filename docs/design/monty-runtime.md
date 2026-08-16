# Monty Python Runtime

## Public contract

`LocalSandbox` owns one Python backend for its lifetime:

```python
LocalSandbox(python_runtime="pyodide")  # default
LocalSandbox(python_runtime="monty")
```

Keeping selection on the constructor prevents interpreter state, filesystem
semantics, and tool APIs from changing between calls on one sandbox.

Monty is an optional dependency installed through `localsandbox[monty]`.
Selecting it without the extra raises an actionable `ImportError` during
construction. Pyodide users do not import or install Monty.

## Filesystem lifecycle

AgentFS remains the durable source of truth. Before each Monty execution, the
Deno shim synchronizes AgentFS into an isolated temporary directory. LocalSandbox
mounts only that directory into Monty at `/data` in `read-write` mode. After the
execution, including a user-code failure, the shim synchronizes changes back to
AgentFS and records the Python history entry.

Monty code must use `open()` or `pathlib` with absolute `/data` paths. Monty
does not expose a working-directory API, so custom `cwd` values are rejected.

## Host tools

The existing `PythonToolset` remains the public tool contract. For Monty, each
definition is injected as a direct external function. The adapter converts
positional and keyword arguments into the declared JSON object, applies the
same input/output schema validation and timeout behavior as Pyodide, and calls
the existing handler.

Monty has no native equivalent of `host_tools.search()`, so LocalSandbox always
injects `tool_search(query, detail="brief", limit=10)`. Tool discovery is a
single Python SDK implementation; Pyodide's `host_tools.search()` relays to it
through the host bridge. Both runtimes therefore use the same BM25 fields,
weights, score threshold, and result formatting. `tool_search` is reserved and
cannot be supplied by a caller toolset.

## Deliberate incompatibilities

- `preload_packages` is rejected; Monty has no third-party package runtime.
- Tools are called directly instead of through `host_tools.call()`.
- Only Monty's Python language and standard-library subset is available.
- Relative filesystem paths, per-call working directories, and `os` filesystem
  helpers are unavailable.
- Network, arbitrary host filesystem, and host environment access remain
  blocked.

`LocalSandbox.get_python_hint()` exposes the most important version of these
constraints for callers to include in agent prompts.
