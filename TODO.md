# BashFS Implementation Plan

Incremental phases - each phase is functional and testable before moving to the next.

---

## Phase 1: TypeScript Shim & Basic Bash Execution

**Goal**: Create the TypeScript shim CLI and verify Python can invoke it to execute bash commands with AgentFS persistence.

### Shim Setup
- [ ] Create `shim/` directory with `package.json`, `tsconfig.json`
- [ ] Install dependencies: `just-bash`, `agentfs-sdk`
- [ ] Create `shim/src/cli.ts` with basic CLI argument parsing
- [ ] Implement `bash` command: opens AgentFS with `--db` path, creates Bash instance, executes command
- [ ] Return JSON result: `{ stdout, stderr, exitCode }`
- [ ] Build shim with `pnpm build`

### Python Integration
- [ ] Create package structure (`bashfs/__init__.py`, `bashfs/core.py`, `bashfs/exceptions.py`)
- [ ] Create `pyproject.toml` with Python 3.12+ requirement
- [ ] Implement minimal `BashFS` class with `__init__` and `destroy`
- [ ] Generate temp SQLite file path in `__init__`
- [ ] Implement `bash(command)` that spawns shim CLI subprocess
- [ ] Parse JSON response into `BashResult` dataclass
- [ ] Add basic test: `sandbox.bash('echo hello')` returns correct stdout

**Testable**: Can execute bash commands, state persists across calls (create file, read it back).

---

## Phase 2: Initial Filesystem Seeding

**Goal**: Support `files` dict to seed initial filesystem contents.

### Shim
- [ ] Implement `seed` command: accepts `--files` JSON, writes files to AgentFS
- [ ] Handle nested directory creation (mkdir -p equivalent)

### Python
- [ ] Accept `files: dict[str, str]` in constructor
- [ ] Call shim `seed` command during `__init__` with files dict
- [ ] Add test: seed file, then `cat` it back
- [ ] Add test: seed multiple files in nested directories

**Testable**: `BashFS(files={'/app/main.py': 'print(1)'})` then `bash('cat /app/main.py')` works.

---

## Phase 3: Exception Handling

**Goal**: Raise exceptions on command failures instead of returning error results.

- [ ] Create exception hierarchy in `exceptions.py`:
  - `BashFSError` (base)
  - `CommandError` (non-zero exit)
  - `TimeoutError`
  - `SubprocessCrashed`
- [ ] Raise `CommandError` when exit_code != 0
- [ ] Raise `SubprocessCrashed` when Node process dies unexpectedly
- [ ] Add subprocess timeout handling (default 120s)
- [ ] Add test: `sandbox.bash('exit 1')` raises `CommandError`
- [ ] Add test: verify `CommandError` contains stdout/stderr/exit_code

**Testable**: Failed commands raise appropriate exceptions with context.

---

## Phase 4: Path References & Binary Files

**Goal**: Support `Path` objects and binary content in files dict.

### Python
- [ ] Detect `Path` instances in files dict, read content at sandbox creation
- [ ] Detect `bytes` instances, base64-encode for transport to shim
- [ ] Update files dict serialization to indicate binary content

### Shim
- [ ] Detect base64-encoded binary content in seed command
- [ ] Decode and write as binary files

### Tests
- [ ] Add test: seed from local file via `Path('./test.txt')`
- [ ] Add test: seed binary file via `bytes`, verify content preserved

**Testable**: `files={'/img.png': Path('./local.png')}` and `files={'/data': b'...'}` work.

---

## Phase 5: Execution Presets

**Goal**: Configurable DOS protection limits.

### Python
- [ ] Create `ExecutionPreset` enum (STRICT, NORMAL, PERMISSIVE)
- [ ] Define limit values for each preset:
  - STRICT: 100 loops, 500 commands
  - NORMAL: 1000 loops, 5000 commands
  - PERMISSIVE: 10000 loops, 50000 commands
- [ ] Accept `preset` parameter in constructor (default: NORMAL)
- [ ] Pass limits to shim via CLI args

### Shim
- [ ] Accept `--limits` JSON arg for execution limits
- [ ] Pass limits to just-bash `executionLimits` config

### Python Exceptions
- [ ] Add `ExecutionLimitError(BashFSError)` with `limit_type`, `limit_value`
- [ ] Parse limit errors from shim output
- [ ] Add test: infinite loop with STRICT preset raises `ExecutionLimitError`

**Testable**: Presets correctly limit runaway commands.

---

## Phase 6: Typed Exceptions (FileNotFound, Permission)

**Goal**: Parse common errors into specific exception types.

- [ ] Add `FileNotFoundError(CommandError)` with `path` attribute
- [ ] Add `PermissionError(CommandError)` with `path` attribute
- [ ] Parse stderr patterns to detect error types
- [ ] Add test: `cat /nonexistent` raises `FileNotFoundError`
- [ ] Add test: verify `FileNotFoundError.path` is set correctly

**Testable**: Specific exceptions raised for common error conditions.

---

## Phase 7: File Helper Methods

**Goal**: Direct filesystem access without bash.

### Shim
- [ ] Implement `read-file` command: `--db`, `--path` → file contents
- [ ] Implement `write-file` command: `--db`, `--path`, `--content`
- [ ] Implement `list-files` command: `--db`, `--path` → JSON array
- [ ] Implement `exists` command: `--db`, `--path` → boolean
- [ ] Implement `delete-file` command: `--db`, `--path`

### Python
- [ ] Implement `read_file(path) -> str`
- [ ] Implement `write_file(path, content)`
- [ ] Implement `list_files(path) -> list[str]`
- [ ] Implement `exists(path) -> bool`
- [ ] Implement `delete_file(path)`

### Tests
- [ ] Add tests for each method
- [ ] Add test: write via helper, read via bash (and vice versa)

**Testable**: All file helpers work and are consistent with bash operations.

---

## Phase 8: Key-Value Store

**Goal**: Separate KV API for agent state.

### Shim
- [ ] Implement `kv-get` command: `--db`, `--key` → value or null
- [ ] Implement `kv-set` command: `--db`, `--key`, `--value`
- [ ] Implement `kv-delete` command: `--db`, `--key`
- [ ] Implement `kv-keys` command: `--db` → JSON array

### Python
- [ ] Create `KVStore` class with `get`, `set`, `delete`, `keys` methods
- [ ] Expose as `sandbox.kv` property
- [ ] String values only (document this limitation)

### Tests
- [ ] Add test: set/get/delete/keys all work
- [ ] Add test: KV state persists across bash() calls

**Testable**: `sandbox.kv.set('key', 'value')` / `sandbox.kv.get('key')` works.

---

## Phase 9: Snapshot & Resume

**Goal**: Export and restore sandbox state.

### Python
- [ ] Implement `export_snapshot() -> bytes` - read SQLite file contents
- [ ] Accept `snapshot: bytes` in constructor
- [ ] Validate mutual exclusivity of `files` and `snapshot` params
- [ ] Write snapshot bytes to temp file, pass path to shim

### Tests
- [ ] Add test: create files, export, destroy, resume, verify files exist
- [ ] Add test: KV state preserved across snapshot/resume
- [ ] Add test: `ValueError` when both `files` and `snapshot` provided

**Testable**: Full round-trip: create → export → resume → verify state.

---

## Phase 10: Lifecycle Hardening

**Goal**: Robust handling of edge cases.

- [ ] Track `_destroyed` state, raise `RuntimeError` on use after destroy
- [ ] Ensure SQLite file deleted on destroy()
- [ ] Handle cleanup on Python process crash (atexit handler?)
- [ ] Add test: operations after destroy() raise RuntimeError
- [ ] Add test: destroy() is idempotent (can call twice safely)

**Testable**: Lifecycle edge cases handled gracefully.

---

## Phase 11: Async Wrappers

**Goal**: Async support for async frameworks.

- [ ] Implement `abash(command)` using `asyncio.to_thread(self.bash, command)`
- [ ] Implement `adestroy()` using `asyncio.to_thread(self.destroy)`
- [ ] Implement async versions of file helpers
- [ ] Implement async versions of KV methods
- [ ] Add async test: `await sandbox.abash('ls')` works
- [ ] Add test: concurrent abash calls work correctly

**Testable**: Async API works in asyncio event loop.

---

## Phase 12: Audit Logging

**Goal**: Automatic logging of all operations to AgentFS toolcall trail.

### Shim
- [ ] Configure AgentFS to record toolcalls
- [ ] Log each bash execution with: command, timestamp, duration, exit_code
- [ ] Truncate large stdout/stderr in logs

### Tests
- [ ] Verify logs are included in exported snapshots
- [ ] Add test: execute commands, verify audit entries exist (query SQLite directly?)

**Testable**: Audit trail populated and persisted.

---

## Phase 13: Documentation & Polish

**Goal**: Production-ready release.

- [ ] Write README.md with quick start guide
- [ ] Add docstrings to all public methods
- [ ] Add type hints throughout (verify with pyright)
- [ ] Create examples/ directory with usage examples
- [ ] Verify all spec requirements implemented
- [ ] Document shim bundling strategy for package distribution
- [ ] Release to PyPI

**Testable**: Package installable via pip, documentation complete.

---

## Testing Strategy

Each phase should have:
1. Unit tests for new functionality
2. Integration test verifying end-to-end behavior
3. Tests run against real shim subprocess (no mocks)

Run tests with: `uv run pytest tests/ -v`

---

## Development Commands

```bash
# Python
uv sync                          # Install Python dependencies
uv run pytest                    # Run tests
uv run pyright                   # Type check
uv run ruff check --fix          # Lint
uv run ruff format               # Format

# TypeScript Shim
cd shim
pnpm install                     # Install shim dependencies
pnpm build                       # Build shim
pnpm test                        # Test shim (if applicable)
```
