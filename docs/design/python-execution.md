# Python Execution in BashFS

## Overview

Add Python execution capability to BashFS so both bash and Python operate on the
same virtual filesystem.

```python
with BashFS() as sandbox:
    sandbox.bash('echo "hello" > /data.txt')
    result = sandbox.execute_python('print(open("/data.txt").read())')  # prints "hello"
```

## Motivation

1. **BashFS owns the filesystem** - It already manages AgentFS SQLite. Python
   execution sharing that filesystem keeps the abstraction clean.

2. **Mirrors container behavior** - In container sandboxes (Modal, Docker),
   Python and bash naturally share one filesystem.

3. **Simpler agent integration** - Agents just call `session.execute_python()`
   or `session.bash()` and both operate on the same state.

## Implementation: Dual Strategy

We implement a dual strategy that prioritizes performance where possible
(Linux/FUSE) while maintaining broad compatibility via a fallback (Sync).

### Architecture

```
Python call → Shim (node process)
                 ↓
      [Platform Check]
      /             \
  [Linux]         [Other/Fallback]
     ↓                   ↓
FUSE Mount           Sync to Temp Dir
     ↓                   ↓
Spawn Deno Runner    Spawn Deno Runner
(Restricted Perms)   (Restricted Perms)
     ↓                   ↓
Pyodide (NODEFS)     Pyodide (NODEFS)
```

### 1. Primary: FUSE Mount (Linux)

Uses `agentfs mount` to mount the SQLite-backed filesystem directly to a
temporary directory.

**Pros:**

- Zero-copy file access
- Real-time updates
- No sync overhead

**Cons:**

- Requires FUSE (Linux only)
- Requires `agentfs` binary

### 2. Fallback: Sync Strategy (macOS/Windows)

Syncs the entire AgentFS content to a temporary directory before execution, and
syncs changes back after.

**Steps:**

1. Create temp directory
2. Export AgentFS files to temp directory
3. Run Python (Pyodide) with NODEFS mount of temp directory
4. Sync modified files back to AgentFS
5. Cleanup temp directory

**Pros:**

- Works everywhere
- Simple implementation

**Cons:**

- O(n) sync cost
- Double storage usage during execution

## Security Model

Python execution happens in a separate, isolated Deno subprocess
(`python-runner.ts`) with strictly limited permissions:

1. **Process Isolation**: The runner is a separate process from the main Shim.
2. **Filesystem Sandbox**:
   - `deno run` is invoked with `--allow-write=<temp-dir>` only.
   - Can only modify the mounted filesystem, not the host system.
3. **Network Isolation**: No `--allow-net` flag
4. **Environment Isolation**: Minimal environment variables passed.

## Implementation Details

### The Shim (`shim/src/python.ts`)

Orchestrates the execution:

1. Determines platform/FUSE availability.
2. Prepares filesystem (Mount or Sync).
3. Spawns the isolated runner.
4. Handles cleanup and syncing back results.

### The Runner (`shim/src/python-runner.ts`)

A lightweight Deno script that:

1. Loads Pyodide.
2. Mounts the provided directory using `NODEFS`.
3. Sets up `cwd`.
4. Executes the code and captures stdout/stderr.

```typescript
// shim/src/python-runner.ts (Simplified)
async function runPython(input: RunnerInput) {
     const py = await getPyodide();
     const mountPoint = "/agent";

     // Mount the directory (FUSE mount or Synced temp dir)
     py.FS.mount(py.FS.filesystems.NODEFS, { root: input.fsRoot }, mountPoint);

     // Run code
     await py.runPythonAsync(input.code);
}
```

## Performance Considerations

**Pyodide Loading:**

- The Deno runner caches downloaded Pyodide artifacts.
- Startup time is dominated by Pyodide initialization (~0.5s - 2s).

**Filesystem Sync (Fallback):**

- Small filesystems (< 100 files): Negligible overhead (< 50ms).
- Large filesystems: Linear degradation. FUSE recommended for large datasets.

## Package Installation

Pyodide supports `micropip` for installing pure Python packages. These are
installed into the Pyodide environment's virtual filesystem (MEMFS), effectively
ephemeral unless we sync `site-packages` (not currently implemented).

```python
import micropip
await micropip.install('requests')
import requests
```

## Implementation Status

- [x] Add `pyodide` dependency
- [x] Implement `python-runner.ts` (Isolated Deno process)
- [x] Implement FUSE support (Linux)
- [x] Implement Sync fallback (macOS/Other)
- [x] Implement `execute-python` shim command
- [x] Add `execute_python()` method to BashFS (Python SDK)
- [ ] Add tests

## Future Optimizations

1. **Persistent Runner**: Keep the Deno/Pyodide process alive to avoid startup
   cost.
2. **Selective Sync**: Only sync files that are actually modified (using
   mtime/hash).
3. **Package Caching**: Cache downloaded micropip packages across runs.
