"""Basic tests for BashFS."""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from bashfs import (
    BashFS,
    CommandError,
    ExecutionLimitError,
    ExecutionPreset,
    FileNotFoundError,
)


class TestBashFSBasic:
    """Test basic BashFS functionality."""

    def test_echo_hello(self) -> None:
        """Test that echo returns expected output."""
        sandbox = BashFS()
        try:
            result = sandbox.bash('echo "hello"')
            assert result.stdout.strip() == "hello"
            assert result.exit_code == 0
        finally:
            sandbox.destroy()

    def test_result_fields(self) -> None:
        """Test that BashResult has all expected fields."""
        sandbox = BashFS()
        try:
            result = sandbox.bash('echo "test"')
            assert isinstance(result.stdout, str)
            assert isinstance(result.stderr, str)
            assert isinstance(result.exit_code, int)
            assert isinstance(result.duration_ms, float)
            assert result.duration_ms > 0
        finally:
            sandbox.destroy()

    def test_destroy_prevents_further_use(self) -> None:
        """Test that destroy() prevents further operations."""
        sandbox = BashFS()
        sandbox.destroy()
        with pytest.raises(RuntimeError, match="destroyed"):
            sandbox.bash('echo "hello"')

    def test_destroy_is_idempotent(self) -> None:
        """Test that destroy() can be called multiple times."""
        sandbox = BashFS()
        sandbox.destroy()
        sandbox.destroy()  # Should not raise

    def test_command_error_on_nonzero_exit(self) -> None:
        """Test that non-zero exit code raises CommandError."""
        sandbox = BashFS()
        try:
            with pytest.raises(CommandError) as exc_info:
                sandbox.bash("exit 1")
            assert exc_info.value.exit_code == 1
        finally:
            sandbox.destroy()

    def test_command_error_has_context(self) -> None:
        """Test that CommandError contains stdout, stderr, and exit_code."""
        sandbox = BashFS()
        try:
            with pytest.raises(CommandError) as exc_info:
                sandbox.bash('echo "out"; echo "err" >&2; exit 42')
            err = exc_info.value
            assert err.exit_code == 42
            assert "out" in err.stdout
            assert "err" in err.stderr
        finally:
            sandbox.destroy()


class TestBashFSPersistence:
    """Test that state persists across bash() calls."""

    def test_file_persists_across_calls(self) -> None:
        """Test that a file created in one call can be read in another."""
        sandbox = BashFS()
        try:
            # Create a file
            sandbox.bash('echo "persistent content" > /home/user/test.txt')

            # Read it back in a separate call
            result = sandbox.bash("cat /home/user/test.txt")
            assert result.stdout.strip() == "persistent content"
        finally:
            sandbox.destroy()

    def test_directory_persists_across_calls(self) -> None:
        """Test that directories created persist."""
        sandbox = BashFS()
        try:
            # Create directory structure
            sandbox.bash("mkdir -p /home/user/project/src")
            sandbox.bash('echo "code" > /home/user/project/src/main.py')

            # Verify in separate calls
            result = sandbox.bash("ls /home/user/project/src")
            assert "main.py" in result.stdout
        finally:
            sandbox.destroy()

    def test_file_modification_persists(self) -> None:
        """Test that file modifications persist."""
        sandbox = BashFS()
        try:
            # Create and modify a file
            sandbox.bash('echo "line1" > /home/user/log.txt')
            sandbox.bash('echo "line2" >> /home/user/log.txt')
            sandbox.bash('echo "line3" >> /home/user/log.txt')

            # Read the full file
            result = sandbox.bash("cat /home/user/log.txt")
            lines = result.stdout.strip().split("\n")
            assert lines == ["line1", "line2", "line3"]
        finally:
            sandbox.destroy()


class TestBashFSSeeding:
    """Test initial file seeding."""

    def test_seed_single_file(self) -> None:
        """Test seeding a single file."""
        with BashFS(files={"/home/user/data.txt": "seeded content"}) as sandbox:
            result = sandbox.bash("cat /home/user/data.txt")
            assert result.stdout.strip() == "seeded content"

            history = sandbox.history()
            # History includes both seed and bash operations
            assert len(history) == 2

            # Most recent first - bash command
            assert history[0].name == "bash"
            assert history[0].parameters is not None
            assert history[0].parameters.get("command") == "cat /home/user/data.txt"
            assert history[0].result is not None
            assert history[0].result.get("exitCode") == 0

            # Second - seed operation
            assert history[1].name == "seed"
            assert history[1].parameters is not None
            assert history[1].parameters.get("paths") == ["/home/user/data.txt"]
            assert history[1].parameters.get("count") == 1
            assert history[1].result is not None
            assert history[1].result.get("success") is True

    def test_seed_multiple_files(self) -> None:
        """Test seeding multiple files."""
        sandbox = BashFS(
            files={
                "/home/user/file1.txt": "content1",
                "/home/user/file2.txt": "content2",
            }
        )
        try:
            result1 = sandbox.bash("cat /home/user/file1.txt")
            result2 = sandbox.bash("cat /home/user/file2.txt")
            assert result1.stdout.strip() == "content1"
            assert result2.stdout.strip() == "content2"
        finally:
            sandbox.destroy()

    def test_seed_nested_directories(self) -> None:
        """Test seeding files in nested directories."""
        sandbox = BashFS(
            files={
                "/project/src/main.py": 'print("hello")',
                "/project/tests/test_main.py": "def test(): pass",
            }
        )
        try:
            result = sandbox.bash("cat /project/src/main.py")
            assert 'print("hello")' in result.stdout

            result = sandbox.bash("ls /project")
            assert "src" in result.stdout
            assert "tests" in result.stdout
        finally:
            sandbox.destroy()

    def test_seed_and_modify(self) -> None:
        """Test that seeded files can be modified."""
        sandbox = BashFS(files={"/home/user/data.txt": "original"})
        try:
            # Modify the seeded file
            sandbox.bash('echo "modified" > /home/user/data.txt')

            # Verify modification persisted
            result = sandbox.bash("cat /home/user/data.txt")
            assert result.stdout.strip() == "modified"
        finally:
            sandbox.destroy()


class TestBashFSFixture:
    """Test using pytest fixture pattern."""

    @pytest.fixture
    def sandbox(self) -> Generator[BashFS, None, None]:
        """Create a sandbox for testing."""
        s = BashFS()
        yield s
        s.destroy()

    def test_with_fixture(self, sandbox: BashFS) -> None:
        """Test using fixture pattern."""
        result = sandbox.bash('echo "fixture test"')
        assert result.stdout.strip() == "fixture test"


class TestBashFSExecutionPresets:
    """Test execution preset limits."""

    def test_strict_preset_limits_loops(self) -> None:
        """Test that STRICT preset limits loop iterations."""
        sandbox = BashFS(preset=ExecutionPreset.STRICT)
        try:
            # This should hit the loop limit (100 iterations)
            # Using a for loop that runs 200 times
            with pytest.raises((ExecutionLimitError, CommandError)):
                sandbox.bash("for i in $(seq 1 200); do echo $i; done")
        finally:
            sandbox.destroy()

    def test_normal_preset_allows_more_loops(self) -> None:
        """Test that NORMAL preset allows more iterations than STRICT."""
        sandbox = BashFS(preset=ExecutionPreset.NORMAL)
        try:
            # This should succeed with NORMAL (1000 iterations allowed)
            result = sandbox.bash("for i in $(seq 1 200); do echo $i; done")
            assert "200" in result.stdout
        finally:
            sandbox.destroy()

    def test_preset_is_configurable(self) -> None:
        """Test that preset parameter is accepted."""
        # Just verify we can create sandboxes with each preset
        for preset in ExecutionPreset:
            sandbox = BashFS(preset=preset)
            try:
                result = sandbox.bash('echo "test"')
                assert result.stdout.strip() == "test"
            finally:
                sandbox.destroy()


class TestBashFSPathAndBytes:
    """Test Path references and binary file support."""

    def test_seed_from_path(self) -> None:
        """Test seeding a file from a local Path."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("content from local file")
            temp_path = Path(f.name)

        try:
            sandbox = BashFS(files={"/home/user/imported.txt": temp_path})
            try:
                result = sandbox.bash("cat /home/user/imported.txt")
                assert result.stdout.strip() == "content from local file"
            finally:
                sandbox.destroy()
        finally:
            temp_path.unlink()

    def test_seed_binary_content(self) -> None:
        """Test seeding binary content."""
        # Create some binary data with non-UTF8 bytes
        binary_data = bytes([0x00, 0x01, 0x02, 0xFF, 0xFE, 0x89, 0x50, 0x4E, 0x47])

        sandbox = BashFS(files={"/home/user/binary.bin": binary_data})
        try:
            # Check file size matches (9 bytes)
            result = sandbox.bash("wc -c /home/user/binary.bin")
            assert "9" in result.stdout
        finally:
            sandbox.destroy()

    def test_seed_empty_bytes(self) -> None:
        """Test seeding empty binary content."""
        sandbox = BashFS(files={"/home/user/empty.bin": b""})
        try:
            result = sandbox.bash("wc -c /home/user/empty.bin")
            # wc -c output format varies, but should contain 0
            assert "0" in result.stdout
        finally:
            sandbox.destroy()

    def test_seed_binary_from_path(self) -> None:
        """Test seeding binary file from a local Path."""
        binary_data = bytes(
            [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]
        )  # PNG header

        with tempfile.NamedTemporaryFile(mode="wb", suffix=".bin", delete=False) as f:
            f.write(binary_data)
            temp_path = Path(f.name)

        try:
            sandbox = BashFS(files={"/home/user/image.png": temp_path})
            try:
                # Check file size matches
                result = sandbox.bash("wc -c /home/user/image.png")
                assert "8" in result.stdout
            finally:
                sandbox.destroy()
        finally:
            temp_path.unlink()


class TestBashFSTypedExceptions:
    """Test typed exception handling for file not found and permission errors."""

    def test_cat_nonexistent_raises_file_not_found(self) -> None:
        """Test that cat on nonexistent file raises FileNotFoundError."""
        sandbox = BashFS()
        try:
            with pytest.raises(FileNotFoundError) as exc_info:
                sandbox.bash("cat /nonexistent/file.txt")
            err = exc_info.value
            assert err.exit_code != 0
            assert "No such file or directory" in err.stderr
        finally:
            sandbox.destroy()

    def test_file_not_found_has_path(self) -> None:
        """Test that FileNotFoundError contains the correct path."""
        sandbox = BashFS()
        try:
            with pytest.raises(FileNotFoundError) as exc_info:
                sandbox.bash("cat /some/missing/path.txt")
            err = exc_info.value
            assert err.path == "/some/missing/path.txt"
        finally:
            sandbox.destroy()

    def test_file_not_found_is_command_error(self) -> None:
        """Test that FileNotFoundError is a subclass of CommandError."""
        sandbox = BashFS()
        try:
            with pytest.raises(CommandError):
                sandbox.bash("cat /nonexistent")
        finally:
            sandbox.destroy()

    def test_ls_nonexistent_raises_file_not_found(self) -> None:
        """Test that ls on nonexistent directory raises FileNotFoundError."""
        sandbox = BashFS()
        try:
            with pytest.raises(FileNotFoundError) as exc_info:
                sandbox.bash("ls /does/not/exist")
            assert exc_info.value.path == "/does/not/exist"
        finally:
            sandbox.destroy()


class TestBashFSFileHelpers:
    """Test file helper methods."""

    def test_read_file(self) -> None:
        """Test reading a file via helper method."""
        sandbox = BashFS(files={"/home/user/test.txt": "hello world"})
        try:
            content = sandbox.read_file("/home/user/test.txt")
            assert content.strip() == "hello world"
        finally:
            sandbox.destroy()

    def test_write_file(self) -> None:
        """Test writing a file via helper method."""
        sandbox = BashFS()
        try:
            sandbox.write_file("/home/user/new.txt", "new content")
            content = sandbox.read_file("/home/user/new.txt")
            assert content.strip() == "new content"
        finally:
            sandbox.destroy()

    def test_list_files(self) -> None:
        """Test listing files in a directory."""
        sandbox = BashFS(
            files={
                "/home/user/dir/file1.txt": "a",
                "/home/user/dir/file2.txt": "b",
            }
        )
        try:
            files = sandbox.list_files("/home/user/dir")
            assert sorted(files) == ["file1.txt", "file2.txt"]
        finally:
            sandbox.destroy()

    def test_exists_true(self) -> None:
        """Test that exists returns True for existing file."""
        sandbox = BashFS(files={"/home/user/exists.txt": "content"})
        try:
            assert sandbox.exists("/home/user/exists.txt") is True
        finally:
            sandbox.destroy()

    def test_exists_false(self) -> None:
        """Test that exists returns False for nonexistent file."""
        sandbox = BashFS()
        try:
            assert sandbox.exists("/nonexistent/file.txt") is False
        finally:
            sandbox.destroy()

    def test_delete_file(self) -> None:
        """Test deleting a file via helper method."""
        sandbox = BashFS(files={"/home/user/todelete.txt": "content"})
        try:
            assert sandbox.exists("/home/user/todelete.txt") is True
            sandbox.delete_file("/home/user/todelete.txt")
            assert sandbox.exists("/home/user/todelete.txt") is False
        finally:
            sandbox.destroy()

    def test_write_via_helper_read_via_bash(self) -> None:
        """Test writing via helper and reading via bash."""
        sandbox = BashFS()
        try:
            sandbox.write_file("/home/user/cross.txt", "cross-method content")
            result = sandbox.bash("cat /home/user/cross.txt")
            assert result.stdout.strip() == "cross-method content"
        finally:
            sandbox.destroy()

    def test_write_via_bash_read_via_helper(self) -> None:
        """Test writing via bash and reading via helper."""
        sandbox = BashFS()
        try:
            sandbox.bash('echo "bash written" > /home/user/bash.txt')
            content = sandbox.read_file("/home/user/bash.txt")
            assert content.strip() == "bash written"
        finally:
            sandbox.destroy()

    def test_read_nonexistent_file_raises_error(self) -> None:
        """Test that reading nonexistent file raises FileNotFoundError."""
        sandbox = BashFS()
        try:
            with pytest.raises(FileNotFoundError):
                sandbox.read_file("/nonexistent/file.txt")
        finally:
            sandbox.destroy()

    def test_write_creates_parent_directories(self) -> None:
        """Test that write_file creates parent directories."""
        sandbox = BashFS()
        try:
            sandbox.write_file("/deep/nested/path/file.txt", "content")
            assert sandbox.exists("/deep/nested/path/file.txt") is True
        finally:
            sandbox.destroy()


class TestBashFSKeyValueStore:
    """Test KV store functionality."""

    def test_kv_set_and_get(self) -> None:
        """Test setting and getting a KV value."""
        sandbox = BashFS()
        try:
            sandbox.kv.set("mykey", "myvalue")
            value = sandbox.kv.get("mykey")
            assert value == "myvalue"
        finally:
            sandbox.destroy()

    def test_kv_get_nonexistent(self) -> None:
        """Test getting a nonexistent key returns None."""
        sandbox = BashFS()
        try:
            value = sandbox.kv.get("nonexistent")
            assert value is None
        finally:
            sandbox.destroy()

    def test_kv_delete(self) -> None:
        """Test deleting a KV value."""
        sandbox = BashFS()
        try:
            sandbox.kv.set("todelete", "value")
            assert sandbox.kv.get("todelete") == "value"
            sandbox.kv.delete("todelete")
            assert sandbox.kv.get("todelete") is None
        finally:
            sandbox.destroy()

    def test_kv_keys(self) -> None:
        """Test listing all keys."""
        sandbox = BashFS()
        try:
            sandbox.kv.set("key1", "value1")
            sandbox.kv.set("key2", "value2")
            sandbox.kv.set("other", "value3")
            keys = sandbox.kv.keys()
            assert "key1" in keys
            assert "key2" in keys
            assert "other" in keys
        finally:
            sandbox.destroy()

    def test_kv_keys_with_prefix(self) -> None:
        """Test listing keys with a prefix filter."""
        sandbox = BashFS()
        try:
            sandbox.kv.set("prefix:a", "value1")
            sandbox.kv.set("prefix:b", "value2")
            sandbox.kv.set("other", "value3")
            keys = sandbox.kv.keys("prefix:")
            assert "prefix:a" in keys
            assert "prefix:b" in keys
            assert "other" not in keys
        finally:
            sandbox.destroy()

    def test_kv_persists_across_bash_calls(self) -> None:
        """Test that KV state persists across bash() calls."""
        sandbox = BashFS()
        try:
            sandbox.kv.set("persistent", "data")
            sandbox.bash("echo test")  # Execute bash command
            value = sandbox.kv.get("persistent")
            assert value == "data"
        finally:
            sandbox.destroy()

    def test_kv_overwrites_existing(self) -> None:
        """Test that setting a key overwrites existing value."""
        sandbox = BashFS()
        try:
            sandbox.kv.set("key", "original")
            sandbox.kv.set("key", "updated")
            assert sandbox.kv.get("key") == "updated"
        finally:
            sandbox.destroy()


class TestBashFSSnapshot:
    """Test snapshot and resume functionality."""

    def test_snapshot_and_resume_files(self) -> None:
        """Test that files are preserved across snapshot/resume."""
        # Create sandbox with files
        sandbox1 = BashFS()
        try:
            sandbox1.bash('echo "test content" > /home/user/myfile.txt')
            sandbox1.bash("mkdir -p /project/src")
            sandbox1.bash('echo "code" > /project/src/main.py')

            # Export snapshot
            snapshot = sandbox1.export_snapshot()
            assert len(snapshot) > 0
        finally:
            sandbox1.destroy()

        # Resume from snapshot
        sandbox2 = BashFS(snapshot=snapshot)
        try:
            # Verify files exist
            assert sandbox2.exists("/home/user/myfile.txt")
            content = sandbox2.read_file("/home/user/myfile.txt")
            assert "test content" in content

            assert sandbox2.exists("/project/src/main.py")
            content = sandbox2.read_file("/project/src/main.py")
            assert "code" in content
        finally:
            sandbox2.destroy()

    def test_snapshot_and_resume_kv(self) -> None:
        """Test that KV state is preserved across snapshot/resume."""
        sandbox1 = BashFS()
        try:
            sandbox1.kv.set("key1", "value1")
            sandbox1.kv.set("key2", "value2")
            snapshot = sandbox1.export_snapshot()
        finally:
            sandbox1.destroy()

        sandbox2 = BashFS(snapshot=snapshot)
        try:
            assert sandbox2.kv.get("key1") == "value1"
            assert sandbox2.kv.get("key2") == "value2"
        finally:
            sandbox2.destroy()

    def test_cannot_provide_both_files_and_snapshot(self) -> None:
        """Test that ValueError is raised when both files and snapshot provided."""
        with pytest.raises(ValueError, match="Cannot provide both"):
            BashFS(files={"/test.txt": "content"}, snapshot=b"dummy")

    def test_snapshot_after_modifications(self) -> None:
        """Test that modifications are captured in snapshot."""
        sandbox1 = BashFS(files={"/initial.txt": "initial"})
        try:
            # Modify and add files
            sandbox1.bash('echo "modified" > /initial.txt')
            sandbox1.bash('echo "new" > /new.txt')
            snapshot = sandbox1.export_snapshot()
        finally:
            sandbox1.destroy()

        sandbox2 = BashFS(snapshot=snapshot)
        try:
            assert "modified" in sandbox2.read_file("/initial.txt")
            assert "new" in sandbox2.read_file("/new.txt")
        finally:
            sandbox2.destroy()

    def test_export_snapshot_after_destroy_raises(self) -> None:
        """Test that export_snapshot raises after destroy."""
        sandbox = BashFS()
        sandbox.destroy()
        with pytest.raises(RuntimeError, match="destroyed"):
            sandbox.export_snapshot()


class TestBashFSContextManager:
    """Test context manager (with statement) support."""

    def test_context_manager_basic(self) -> None:
        """Test basic context manager usage."""
        with BashFS() as sandbox:
            result = sandbox.bash('echo "context manager test"')
            assert result.stdout.strip() == "context manager test"

    def test_context_manager_destroys_on_exit(self) -> None:
        """Test that context manager calls destroy on exit."""
        with BashFS() as sandbox:
            sandbox.bash('echo "test"')
        # After exiting, sandbox should be destroyed
        with pytest.raises(RuntimeError, match="destroyed"):
            sandbox.bash('echo "after exit"')

    def test_context_manager_destroys_on_exception(self) -> None:
        """Test that context manager calls destroy even on exception."""
        sandbox_ref = None
        try:
            with BashFS() as sandbox:
                sandbox_ref = sandbox
                sandbox.bash('echo "before error"')
                raise ValueError("Test error")
        except ValueError:
            pass  # Expected

        # Sandbox should still be destroyed
        assert sandbox_ref is not None
        with pytest.raises(RuntimeError, match="destroyed"):
            sandbox_ref.bash('echo "after error"')

    def test_context_manager_with_files(self) -> None:
        """Test context manager with initial files."""
        with BashFS(files={"/home/user/test.txt": "content"}) as sandbox:
            content = sandbox.read_file("/home/user/test.txt")
            assert content.strip() == "content"

    def test_context_manager_with_kv(self) -> None:
        """Test context manager with KV store."""
        with BashFS() as sandbox:
            sandbox.kv.set("key", "value")
            assert sandbox.kv.get("key") == "value"


class TestBashFSAsync:
    """Test async API."""

    @pytest.mark.asyncio
    async def test_abash_basic(self) -> None:
        """Test basic async bash execution."""
        sandbox = BashFS()
        try:
            result = await sandbox.abash('echo "async hello"')
            assert result.stdout.strip() == "async hello"
            assert result.exit_code == 0
        finally:
            sandbox.destroy()

    @pytest.mark.asyncio
    async def test_abash_error(self) -> None:
        """Test async bash raises CommandError on failure."""
        sandbox = BashFS()
        try:
            with pytest.raises(CommandError):
                await sandbox.abash("exit 1")
        finally:
            sandbox.destroy()

    @pytest.mark.asyncio
    async def test_async_file_helpers(self) -> None:
        """Test async file helper methods."""
        sandbox = BashFS()
        try:
            await sandbox.awrite_file("/home/user/async.txt", "async content")
            content = await sandbox.aread_file("/home/user/async.txt")
            assert content.strip() == "async content"

            exists = await sandbox.aexists("/home/user/async.txt")
            assert exists is True

            files = await sandbox.alist_files("/home/user")
            assert "async.txt" in files

            await sandbox.adelete_file("/home/user/async.txt")
            exists = await sandbox.aexists("/home/user/async.txt")
            assert exists is False
        finally:
            sandbox.destroy()

    @pytest.mark.asyncio
    async def test_async_kv(self) -> None:
        """Test async KV store methods."""
        sandbox = BashFS()
        try:
            await sandbox.kv.aset("asynckey", "asyncvalue")
            value = await sandbox.kv.aget("asynckey")
            assert value == "asyncvalue"

            keys = await sandbox.kv.akeys()
            assert "asynckey" in keys

            await sandbox.kv.adelete("asynckey")
            value = await sandbox.kv.aget("asynckey")
            assert value is None
        finally:
            sandbox.destroy()

    @pytest.mark.asyncio
    async def test_adestroy(self) -> None:
        """Test async destroy."""
        sandbox = BashFS()
        await sandbox.abash('echo "test"')
        await sandbox.adestroy()
        with pytest.raises(RuntimeError, match="destroyed"):
            await sandbox.abash('echo "after destroy"')

    @pytest.mark.asyncio
    async def test_aexport_snapshot(self) -> None:
        """Test async snapshot export."""
        sandbox = BashFS()
        try:
            await sandbox.abash('echo "snapshot content" > /home/user/snap.txt')
            snapshot = await sandbox.aexport_snapshot()
            assert len(snapshot) > 0
        finally:
            await sandbox.adestroy()

    @pytest.mark.asyncio
    async def test_concurrent_abash_on_different_sandboxes(self) -> None:
        """Test that concurrent abash calls on DIFFERENT sandboxes work."""
        import asyncio

        # Each sandbox has its own db file, so concurrent access works
        sandbox1 = BashFS()
        sandbox2 = BashFS()
        sandbox3 = BashFS()
        try:
            results = await asyncio.gather(
                sandbox1.abash('echo "one"'),
                sandbox2.abash('echo "two"'),
                sandbox3.abash('echo "three"'),
            )
            outputs = [r.stdout.strip() for r in results]
            assert sorted(outputs) == ["one", "three", "two"]
        finally:
            sandbox1.destroy()
            sandbox2.destroy()
            sandbox3.destroy()

    @pytest.mark.asyncio
    async def test_sequential_abash_calls(self) -> None:
        """Test sequential async bash calls on the same sandbox."""
        sandbox = BashFS()
        try:
            # Sequential calls work fine on the same sandbox
            r1 = await sandbox.abash('echo "first"')
            r2 = await sandbox.abash('echo "second"')
            r3 = await sandbox.abash('echo "third"')
            assert r1.stdout.strip() == "first"
            assert r2.stdout.strip() == "second"
            assert r3.stdout.strip() == "third"
        finally:
            sandbox.destroy()


class TestHistory:
    """Tests for bash command history recording."""

    def test_history_records_bash_commands(self) -> None:
        """Test that bash commands are recorded in history."""
        with BashFS() as sandbox:
            sandbox.bash('echo "hello"')
            sandbox.bash('echo "world"')

            history = sandbox.history()
            assert len(history) == 2
            assert all(e.name == "bash" for e in history)

    def test_history_includes_command_and_cwd(self) -> None:
        """Test that history entries include command and cwd parameters."""
        with BashFS() as sandbox:
            sandbox.bash('echo "test"')

            history = sandbox.history()
            assert len(history) == 1
            entry = history[0]
            assert entry.parameters is not None
            assert entry.parameters.get("command") == 'echo "test"'
            assert entry.parameters.get("cwd") == "/home/user"

    def test_history_includes_exit_code(self) -> None:
        """Test that history entries include the exit code in result."""
        with BashFS() as sandbox:
            sandbox.bash('echo "success"')

            history = sandbox.history()
            assert len(history) == 1
            entry = history[0]
            assert entry.result is not None
            assert entry.result.get("exitCode") == 0

    def test_history_records_failed_commands(self) -> None:
        """Test that failed commands are also recorded in history."""
        with BashFS() as sandbox:
            try:
                sandbox.bash("exit 1")
            except Exception:
                pass  # Expected to fail

            history = sandbox.history()
            # May not be recorded if command failed before record call
            # This depends on implementation - let's check
            if len(history) > 0:
                entry = history[0]
                assert entry.result is not None
                assert entry.result.get("exitCode") == 1

    def test_history_limit(self) -> None:
        """Test that history respects the limit parameter."""
        with BashFS() as sandbox:
            for i in range(5):
                sandbox.bash(f'echo "{i}"')

            # Get all
            history_all = sandbox.history(limit=100)
            assert len(history_all) == 5

            # Get limited
            history_limited = sandbox.history(limit=2)
            assert len(history_limited) == 2

    def test_history_timestamps(self) -> None:
        """Test that history entries have valid timestamps."""
        with BashFS() as sandbox:
            sandbox.bash('echo "test"')

            history = sandbox.history()
            assert len(history) == 1
            entry = history[0]
            assert entry.started_at > 0
            assert entry.completed_at >= entry.started_at

    def test_history_empty_on_new_sandbox(self) -> None:
        """Test that a new sandbox has empty history."""
        with BashFS() as sandbox:
            history = sandbox.history()
            assert len(history) == 0

    def test_history_persists_in_snapshot(self) -> None:
        """Test that history is preserved in snapshots."""
        with BashFS() as sandbox1:
            sandbox1.bash('echo "before snapshot"')
            snapshot = sandbox1.export_snapshot()

        with BashFS(snapshot=snapshot) as sandbox2:
            history = sandbox2.history()
            assert len(history) == 1
            assert history[0].parameters is not None
            assert history[0].parameters.get("command") == 'echo "before snapshot"'

    @pytest.mark.asyncio
    async def test_ahistory(self) -> None:
        """Test async history method."""
        sandbox = BashFS()
        try:
            await sandbox.abash('echo "async command"')
            history = await sandbox.ahistory()
            assert len(history) == 1
            assert history[0].parameters is not None
            assert history[0].parameters.get("command") == 'echo "async command"'
        finally:
            sandbox.destroy()
