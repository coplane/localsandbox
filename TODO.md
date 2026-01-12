# BashFS Implementation Plan

Incremental phases - each phase is functional and testable before moving to the next.

---

## Phase 1: Package Scaffold & Subprocess Proof-of-Concept

**Goal**: Verify Python can invoke just-bash via Node subprocess and get output back.

- [ ] Create package structure (`bashfs/__init__.py`, `bashfs/core.py`, `bashfs/exceptions.py`)
- [ ] Create `pyproject.toml` with Python 3.12+ requirement
- [ ] Implement minimal `BashFS` class with `__init__` and `destroy`
- [ ] Implement `bash(command)` that spawns `node -e "..."` to run just-bash
- [ ] Return raw stdout string (no structured result yet)
- [ ] Add basic test: `sandbox.bash('echo hello')` returns `"hello\n"`

**Testable**: Can execute simple bash commands and get stdout.

---

## Phase 2: Structured BashResult

**Goal**: Return structured results instead of raw strings.

- [ ] Create `BashResult` dataclass with `stdout`, `stderr`, `exit_code`, `duration_ms`
- [ ] Update subprocess call to capture stderr separately
- [ ] Parse exit code from just-bash output
- [ ] Measure execution duration
- [ ] Add test: verify all BashResult fields populated correctly

**Testable**: `result.exit_code`, `result.stderr`, `result.duration_ms` all work.

---

## Phase 3: Basic Exception Handling

**Goal**: Raise exceptions on command failures instead of returning error results.

- [ ] Create exception hierarchy in `exceptions.py`:
  - `BashFSError` (base)
  - `CommandError` (non-zero exit)
  - `TimeoutError`
  - `SubprocessCrashed`
- [ ] Raise `CommandError` when exit_code != 0
- [ ] Raise `SubprocessCrashed` when Node process dies unexpectedly
- [ ] Add test: `sandbox.bash('exit 1')` raises `CommandError`
- [ ] Add test: verify `CommandError` contains stdout/stderr/exit_code

**Testable**: Failed commands raise appropriate exceptions with context.

---

## Phase 4: Initial Filesystem Seeding (String Content)

**Goal**: Support `files` dict with string content values.

- [ ] Accept `files: dict[str, str]` in constructor
- [ ] Generate Node script that initializes just-bash with `files` option
- [ ] JSON-serialize the files dict for Node consumption
- [ ] Add test: seed file, then `cat` it back
- [ ] Add test: seed multiple files in nested directories

**Testable**: `BashFS(files={'/app/main.py': 'print(1)'})` then `bash('cat /app/main.py')` works.

---

## Phase 5: AgentFS SQLite Integration

**Goal**: Persist filesystem state across bash() calls via AgentFS.

- [ ] Generate unique SQLite file path in temp directory per instance
- [ ] Update Node script to use agentfs-sdk with SQLite backend
- [ ] Hydrate from SQLite on each subprocess spawn
- [ ] Persist changes back to SQLite after each command
- [ ] Add test: create file in one bash() call, read it in another
- [ ] Add test: modify file across multiple calls, verify final state

**Testable**: State persists across multiple `bash()` invocations.

---

## Phase 6: Path References & Binary Files

**Goal**: Support `Path` objects and binary content in files dict.

- [ ] Detect `Path` instances in files dict, read content at sandbox creation
- [ ] Detect `bytes` instances, base64-encode for transport to Node
- [ ] Update Node script to decode base64 content for binary files
- [ ] Add test: seed from local file via `Path('./test.txt')`
- [ ] Add test: seed binary file via `bytes`, verify with `md5sum`

**Testable**: `files={'/img.png': Path('./local.png')}` and `files={'/bin': b'...'}` work.

---

## Phase 7: File Helper Methods

**Goal**: Direct filesystem access without bash.

- [ ] Implement `read_file(path) -> str` - query AgentFS directly
- [ ] Implement `write_file(path, content)` - write to AgentFS directly
- [ ] Implement `list_files(path) -> list[str]` - list directory
- [ ] Implement `exists(path) -> bool` - check existence
- [ ] Implement `delete_file(path)` - remove file
- [ ] Add tests for each method
- [ ] Add test: write via helper, read via bash (and vice versa)

**Testable**: All file helpers work and are consistent with bash operations.

---

## Phase 8: Typed Exceptions (FileNotFound, Permission)

**Goal**: Parse common errors into specific exception types.

- [ ] Add `FileNotFoundError(CommandError)` with `path` attribute
- [ ] Add `PermissionError(CommandError)` with `path` attribute
- [ ] Add `ExecutionLimitError(BashFSError)` with `limit_type`, `limit_value`
- [ ] Parse stderr patterns to detect error types
- [ ] Add test: `cat /nonexistent` raises `FileNotFoundError`
- [ ] Add test: verify `FileNotFoundError.path` is set correctly

**Testable**: Specific exceptions raised for common error conditions.

---

## Phase 9: Execution Presets

**Goal**: Configurable DOS protection limits.

- [ ] Create `ExecutionPreset` enum (STRICT, NORMAL, PERMISSIVE)
- [ ] Define limit values for each preset:
  - STRICT: 100 loops, 500 commands
  - NORMAL: 1000 loops, 5000 commands
  - PERMISSIVE: 10000 loops, 50000 commands
- [ ] Pass limits to just-bash `executionLimits` config
- [ ] Accept `preset` parameter in constructor (default: NORMAL)
- [ ] Add test: infinite loop with STRICT preset raises `ExecutionLimitError`

**Testable**: Presets correctly limit runaway commands.

---

## Phase 10: Key-Value Store

**Goal**: Separate KV API for agent state.

- [ ] Create `KVStore` class with `get`, `set`, `delete`, `keys` methods
- [ ] Expose as `sandbox.kv` property
- [ ] Store KV data in AgentFS (separate from filesystem)
- [ ] String values only (document this limitation)
- [ ] Add test: set/get/delete/keys all work
- [ ] Add test: KV state persists across bash() calls

**Testable**: `sandbox.kv.set('key', 'value')` / `sandbox.kv.get('key')` works.

---

## Phase 11: Snapshot & Resume

**Goal**: Export and restore sandbox state.

- [ ] Implement `export_snapshot() -> bytes` - return SQLite file contents
- [ ] Accept `snapshot: bytes` in constructor
- [ ] Validate mutual exclusivity of `files` and `snapshot` params
- [ ] Write snapshot bytes to temp file, initialize AgentFS from it
- [ ] Add test: create files, export, destroy, resume, verify files exist
- [ ] Add test: KV state preserved across snapshot/resume
- [ ] Add test: `ValueError` when both `files` and `snapshot` provided

**Testable**: Full round-trip: create → export → resume → verify state.

---

## Phase 12: Async Wrappers

**Goal**: Async support for async frameworks.

- [ ] Implement `abash(command)` using `asyncio.to_thread(self.bash, command)`
- [ ] Implement `adestroy()` using `asyncio.to_thread(self.destroy)`
- [ ] Implement async versions of file helpers if needed
- [ ] Add async test: `await sandbox.abash('ls')` works
- [ ] Add test: concurrent abash calls work correctly

**Testable**: Async API works in asyncio event loop.

---

## Phase 13: Audit Logging

**Goal**: Automatic logging of all operations to AgentFS toolcall trail.

- [ ] Configure AgentFS to record toolcalls
- [ ] Log each bash() invocation with: command, timestamp, duration, exit_code
- [ ] Truncate large stdout/stderr in logs
- [ ] Verify logs are included in exported snapshots
- [ ] Add test: execute commands, verify audit entries exist in AgentFS

**Testable**: Audit trail populated and persisted.

---

## Phase 14: Lifecycle Hardening

**Goal**: Robust handling of edge cases.

- [ ] Track `_destroyed` state, raise `RuntimeError` on use after destroy
- [ ] Ensure SQLite file deleted on destroy()
- [ ] Handle cleanup on Python process crash (atexit handler?)
- [ ] Add test: operations after destroy() raise RuntimeError
- [ ] Add test: destroy() is idempotent (can call twice safely)

**Testable**: Lifecycle edge cases handled gracefully.

---

## Phase 15: Documentation & Polish

**Goal**: Production-ready release.

- [ ] Write README.md with quick start guide
- [ ] Add docstrings to all public methods
- [ ] Add type hints throughout (verify with mypy)
- [ ] Create examples/ directory with usage examples
- [ ] Verify all spec requirements implemented
- [ ] Release to PyPI

**Testable**: Package installable via pip, documentation complete.

---

## Testing Strategy

Each phase should have:
1. Unit tests for new functionality
2. Integration test verifying end-to-end behavior
3. Tests run against real Node subprocess (no mocks)

Run tests with: `pytest tests/ -v`
