# BashFS Implementation Plan

Incremental phases - each phase is functional and testable before moving to the next.

---

## Phase 1: TypeScript Shim & Basic Bash Execution ✅

**Goal**: Create the TypeScript shim CLI and verify Python can invoke it to execute bash commands with AgentFS persistence.

### Shim Setup
- [x] Create `shim/` directory with `package.json`, `tsconfig.json`
- [x] Install dependencies: `just-bash`, `agentfs-sdk`
- [x] Create `shim/src/cli.ts` with basic CLI argument parsing
- [x] Implement `bash` command: opens AgentFS with `--db` path, creates Bash instance, executes command
- [x] Return JSON result: `{ stdout, stderr, exitCode }`
- [x] Build shim with `pnpm build`

### Python Integration
- [x] Create package structure (`bashfs/__init__.py`, `bashfs/core.py`, `bashfs/exceptions.py`)
- [x] Create `pyproject.toml` with Python 3.12+ requirement
- [x] Implement minimal `BashFS` class with `__init__` and `destroy`
- [x] Generate temp SQLite file path in `__init__`
- [x] Implement `bash(command)` that spawns shim CLI subprocess
- [x] Parse JSON response into `BashResult` dataclass
- [x] Add basic test: `sandbox.bash('echo hello')` returns correct stdout

**Testable**: Can execute bash commands, state persists across calls (create file, read it back).

---

## Phase 2: Initial Filesystem Seeding ✅

**Goal**: Support `files` dict to seed initial filesystem contents.

### Shim
- [x] Implement `seed` command: accepts `--files` JSON, writes files to AgentFS
- [x] Handle nested directory creation (mkdir -p equivalent)

### Python
- [x] Accept `files: dict[str, str]` in constructor
- [x] Call shim `seed` command during `__init__` with files dict
- [x] Add test: seed file, then `cat` it back
- [x] Add test: seed multiple files in nested directories

**Testable**: `BashFS(files={'/app/main.py': 'print(1)'})` then `bash('cat /app/main.py')` works.

---

## Phase 3: Exception Handling ✅

**Goal**: Raise exceptions on command failures instead of returning error results.

- [x] Create exception hierarchy in `exceptions.py`:
  - `BashFSError` (base)
  - `CommandError` (non-zero exit)
  - `TimeoutError`
  - `SubprocessCrashed`
- [x] Raise `CommandError` when exit_code != 0
- [x] Raise `SubprocessCrashed` when Node process dies unexpectedly
- [x] Add subprocess timeout handling (default 120s)
- [x] Add test: `sandbox.bash('exit 1')` raises `CommandError`
- [x] Add test: verify `CommandError` contains stdout/stderr/exit_code

**Testable**: Failed commands raise appropriate exceptions with context.

---

## Phase 4: Path References & Binary Files ✅

**Goal**: Support `Path` objects and binary content in files dict.

### Python
- [x] Detect `Path` instances in files dict, read content at sandbox creation
- [x] Detect `bytes` instances, base64-encode for transport to shim
- [x] Update files dict serialization to indicate binary content

### Shim
- [x] Detect base64-encoded binary content in seed command
- [x] Decode and write as binary files

### Tests
- [x] Add test: seed from local file via `Path('./test.txt')`
- [x] Add test: seed binary file via `bytes`, verify content preserved

**Testable**: `files={'/img.png': Path('./local.png')}` and `files={'/data': b'...'}` work.

---

## Phase 5: Execution Presets ✅

**Goal**: Configurable DOS protection limits.

### Python
- [x] Create `ExecutionPreset` enum (STRICT, NORMAL, PERMISSIVE)
- [x] Define limit values for each preset:
  - STRICT: 100 loops, 500 commands
  - NORMAL: 1000 loops, 5000 commands
  - PERMISSIVE: 10000 loops, 50000 commands
- [x] Accept `preset` parameter in constructor (default: NORMAL)
- [x] Pass limits to shim via CLI args

### Shim
- [x] Accept `--limits` JSON arg for execution limits
- [x] Pass limits to just-bash `executionLimits` config

### Python Exceptions
- [x] Add `ExecutionLimitError(BashFSError)` with `limit_type`, `limit_value`
- [x] Parse limit errors from shim output
- [x] Add test: infinite loop with STRICT preset raises `ExecutionLimitError`

**Testable**: Presets correctly limit runaway commands.

---

## Phase 6: Typed Exceptions (FileNotFound, Permission) ✅

**Goal**: Parse common errors into specific exception types.

- [x] Add `FileNotFoundError(CommandError)` with `path` attribute
- [x] Add `PermissionError(CommandError)` with `path` attribute
- [x] Parse stderr patterns to detect error types
- [x] Add test: `cat /nonexistent` raises `FileNotFoundError`
- [x] Add test: verify `FileNotFoundError.path` is set correctly

**Testable**: Specific exceptions raised for common error conditions.

---

## Phase 7: File Helper Methods ✅

**Goal**: Direct filesystem access without bash.

### Shim
- [x] Implement `read-file` command: `--db`, `--path` → file contents
- [x] Implement `write-file` command: `--db`, `--path`, `--content`
- [x] Implement `list-files` command: `--db`, `--path` → JSON array
- [x] Implement `exists` command: `--db`, `--path` → boolean
- [x] Implement `delete-file` command: `--db`, `--path`

### Python
- [x] Implement `read_file(path) -> str`
- [x] Implement `write_file(path, content)`
- [x] Implement `list_files(path) -> list[str]`
- [x] Implement `exists(path) -> bool`
- [x] Implement `delete_file(path)`

### Future Enhancement
- [ ] Add `read_file_bytes(path) -> bytes` for binary file support (currently can seed binary but can't read back as bytes)

### Tests
- [x] Add tests for each method
- [x] Add test: write via helper, read via bash (and vice versa)

**Testable**: All file helpers work and are consistent with bash operations.

---

## Phase 8: Key-Value Store ✅

**Goal**: Separate KV API for agent state.

### Shim
- [x] Implement `kv-get` command: `--db`, `--key` → value or null
- [x] Implement `kv-set` command: `--db`, `--key`, `--value`
- [x] Implement `kv-delete` command: `--db`, `--key`
- [x] Implement `kv-keys` command: `--db` → JSON array

### Python
- [x] Create `KVStore` class with `get`, `set`, `delete`, `keys` methods
- [x] Expose as `sandbox.kv` property
- [x] String values only (document this limitation)

### Tests
- [x] Add test: set/get/delete/keys all work
- [x] Add test: KV state persists across bash() calls

**Testable**: `sandbox.kv.set('key', 'value')` / `sandbox.kv.get('key')` works.

---

## Phase 9: Snapshot & Resume ✅

**Goal**: Export and restore sandbox state.

### Python
- [x] Implement `export_snapshot() -> bytes` - read SQLite file contents
- [x] Accept `snapshot: bytes` in constructor
- [x] Validate mutual exclusivity of `files` and `snapshot` params
- [x] Write snapshot bytes to temp file, pass path to shim
- [x] Checkpoint WAL before export to ensure all data is captured

### Tests
- [x] Add test: create files, export, destroy, resume, verify files exist
- [x] Add test: KV state preserved across snapshot/resume
- [x] Add test: `ValueError` when both `files` and `snapshot` provided

**Testable**: Full round-trip: create → export → resume → verify state.

---

## Phase 10: Lifecycle Hardening ✅

**Goal**: Robust handling of edge cases.

- [x] Track `_destroyed` state, raise `RuntimeError` on use after destroy
- [x] Ensure SQLite file deleted on destroy() (including WAL and SHM files)
- [x] Handle cleanup on Python process crash (atexit handler with WeakSet registry)
- [x] Add context manager support (`__enter__`/`__exit__`) for cleaner usage:
  ```python
  with BashFS() as sandbox:
      result = sandbox.bash('echo "hello"')
  ```
- [x] Add test: operations after destroy() raise RuntimeError
- [x] Add test: destroy() is idempotent (can call twice safely)
- [x] Add tests for context manager (5 tests added)

**Testable**: Lifecycle edge cases handled gracefully.

---

## Phase 11: Async Wrappers ✅

**Goal**: Async support for async frameworks.

- [x] Implement `abash(command)` using `asyncio.to_thread(self.bash, command)`
- [x] Implement `adestroy()` using `asyncio.to_thread(self.destroy)`
- [x] Implement async versions of file helpers (`aread_file`, `awrite_file`, `alist_files`, `aexists`, `adelete_file`, `aexport_snapshot`)
- [x] Implement async versions of KV methods (`aget`, `aset`, `adelete`, `akeys`)
- [x] Add async test: `await sandbox.abash('ls')` works
- [x] Add test: concurrent abash calls on different sandboxes work correctly
- [x] Add test: sequential abash calls on same sandbox work correctly

**Note**: Concurrent calls on the SAME sandbox don't work due to SQLite locking (each subprocess opens the db). Use separate sandboxes for concurrent operations.

**Testable**: Async API works in asyncio event loop.

---

## Phase 12: Documentation & Polish ✅

**Goal**: Production-ready release.

### Completed
- [x] Write README.md with quick start guide
- [x] Add docstrings to all public methods
- [x] Add type hints throughout (verify with pyright - 0 errors)
- [x] Verify all spec requirements implemented

### Optional / Future
- [ ] Create examples/ directory with usage examples
- [ ] Document shim bundling strategy for package distribution
- [ ] Release to PyPI

**Testable**: Package installable via pip, documentation complete.

---

## Future Enhancements

### Simpler Seeding via AgentFS Direct API
- [ ] Explore using AgentFS filesystem API directly for seeding instead of bash scripts
- [ ] Currently seeding uses `bash.exec('cat > file << EOF...')` which is indirect
- [ ] AgentFS `fs` interface may have direct file write methods that are simpler/faster

---

## Testing Strategy

Each phase should have:
1. Unit tests for new functionality
2. Integration test verifying end-to-end behavior
3. Tests run against real shim subprocess (no mocks)

Run tests with: `uv run python -m pytest tests/ -v`

---

## Development Commands

```bash
# Python
uv sync                          # Install Python dependencies
uv run python -m pytest tests/   # Run tests
uv run pyright                   # Type check
uv run ruff check --fix          # Lint
uv run ruff format               # Format

# TypeScript Shim
cd shim
pnpm install                     # Install shim dependencies
pnpm build                       # Build shim
pnpm test                        # Test shim (if applicable)
```
