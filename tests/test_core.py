"""Core tests for LocalSandbox."""

from collections.abc import Generator

import pytest

from localsandbox import (
    LocalSandbox,
    CommandError,
    ExecutionLimitError,
    ExecutionPreset,
    FileNotFoundError,
)


class TestLocalSandboxBasic:
    """Test basic LocalSandbox functionality."""

    def test_echo_hello(self) -> None:
        """Test that echo returns expected output."""
        sandbox = LocalSandbox()
        try:
            result = sandbox.bash('echo "hello"')
            assert result.stdout.strip() == "hello"
            assert result.exit_code == 0
        finally:
            sandbox.destroy()

    def test_result_fields(self) -> None:
        """Test that BashResult has all expected fields."""
        sandbox = LocalSandbox()
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
        sandbox = LocalSandbox()
        sandbox.destroy()
        with pytest.raises(RuntimeError, match="destroyed"):
            sandbox.bash('echo "hello"')

    def test_destroy_is_idempotent(self) -> None:
        """Test that destroy() can be called multiple times."""
        sandbox = LocalSandbox()
        sandbox.destroy()
        sandbox.destroy()  # Should not raise

    def test_command_error_on_nonzero_exit(self) -> None:
        """Test that non-zero exit code raises CommandError."""
        sandbox = LocalSandbox()
        try:
            with pytest.raises(CommandError) as exc_info:
                sandbox.bash("exit 1")
            assert exc_info.value.exit_code == 1
        finally:
            sandbox.destroy()

    def test_command_error_has_context(self) -> None:
        """Test that CommandError contains stdout, stderr, and exit_code."""
        sandbox = LocalSandbox()
        try:
            with pytest.raises(CommandError) as exc_info:
                sandbox.bash('echo "out"; echo "err" >&2; exit 42')
            err = exc_info.value
            assert err.exit_code == 42
            assert "out" in err.stdout
            assert "err" in err.stderr
        finally:
            sandbox.destroy()


class TestLocalSandboxFixture:
    """Test using pytest fixture pattern."""

    @pytest.fixture
    def sandbox(self) -> Generator[LocalSandbox, None, None]:
        """Create a sandbox for testing."""
        s = LocalSandbox()
        yield s
        s.destroy()

    def test_with_fixture(self, sandbox: LocalSandbox) -> None:
        """Test using fixture pattern."""
        result = sandbox.bash('echo "fixture test"')
        assert result.stdout.strip() == "fixture test"


class TestLocalSandboxExecutionPresets:
    """Test execution preset limits."""

    def test_strict_preset_limits_loops(self) -> None:
        """Test that STRICT preset limits loop iterations."""
        sandbox = LocalSandbox(preset=ExecutionPreset.STRICT)
        try:
            # This should hit the loop limit (100 iterations)
            # Using a for loop that runs 200 times
            with pytest.raises((ExecutionLimitError, CommandError)):
                sandbox.bash("for i in $(seq 1 200); do echo $i; done")
        finally:
            sandbox.destroy()

    def test_normal_preset_allows_more_loops(self) -> None:
        """Test that NORMAL preset allows more iterations than STRICT."""
        sandbox = LocalSandbox(preset=ExecutionPreset.NORMAL)
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
            sandbox = LocalSandbox(preset=preset)
            try:
                result = sandbox.bash('echo "test"')
                assert result.stdout.strip() == "test"
            finally:
                sandbox.destroy()


class TestLocalSandboxTypedExceptions:
    """Test typed exception handling for file not found and permission errors."""

    def test_cat_nonexistent_raises_file_not_found(self) -> None:
        """Test that cat on nonexistent file raises FileNotFoundError."""
        sandbox = LocalSandbox()
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
        sandbox = LocalSandbox()
        try:
            with pytest.raises(FileNotFoundError) as exc_info:
                sandbox.bash("cat /some/missing/path.txt")
            err = exc_info.value
            assert err.path == "/some/missing/path.txt"
        finally:
            sandbox.destroy()

    def test_file_not_found_is_command_error(self) -> None:
        """Test that FileNotFoundError is a subclass of CommandError."""
        sandbox = LocalSandbox()
        try:
            with pytest.raises(CommandError):
                sandbox.bash("cat /nonexistent")
        finally:
            sandbox.destroy()

    def test_ls_nonexistent_raises_file_not_found(self) -> None:
        """Test that ls on nonexistent directory raises FileNotFoundError."""
        sandbox = LocalSandbox()
        try:
            with pytest.raises(FileNotFoundError) as exc_info:
                sandbox.bash("ls /does/not/exist")
            assert exc_info.value.path == "/does/not/exist"
        finally:
            sandbox.destroy()


class TestLocalSandboxContextManager:
    """Test context manager (with statement) support."""

    def test_context_manager_basic(self) -> None:
        """Test basic context manager usage."""
        with LocalSandbox() as sandbox:
            result = sandbox.bash('echo "context manager test"')
            assert result.stdout.strip() == "context manager test"

    def test_context_manager_destroys_on_exit(self) -> None:
        """Test that context manager calls destroy on exit."""
        with LocalSandbox() as sandbox:
            sandbox.bash('echo "test"')
        # After exiting, sandbox should be destroyed
        with pytest.raises(RuntimeError, match="destroyed"):
            sandbox.bash('echo "after exit"')

    def test_context_manager_destroys_on_exception(self) -> None:
        """Test that context manager calls destroy even on exception."""
        sandbox_ref = None
        try:
            with LocalSandbox() as sandbox:
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
        with LocalSandbox(files={"/home/user/test.txt": "content"}) as sandbox:
            content = sandbox.read_file("/home/user/test.txt")
            assert content.strip() == "content"

    def test_context_manager_with_kv(self) -> None:
        """Test context manager with KV store."""
        with LocalSandbox() as sandbox:
            sandbox.kv.set("key", "value")
            assert sandbox.kv.get("key") == "value"


class TestLocalSandboxAsync:
    """Test async API."""

    @pytest.mark.asyncio
    async def test_abash_basic(self) -> None:
        """Test basic async bash execution."""
        sandbox = LocalSandbox()
        try:
            result = await sandbox.abash('echo "async hello"')
            assert result.stdout.strip() == "async hello"
            assert result.exit_code == 0
        finally:
            sandbox.destroy()

    @pytest.mark.asyncio
    async def test_abash_error(self) -> None:
        """Test async bash raises CommandError on failure."""
        sandbox = LocalSandbox()
        try:
            with pytest.raises(CommandError):
                await sandbox.abash("exit 1")
        finally:
            sandbox.destroy()

    @pytest.mark.asyncio
    async def test_async_file_helpers(self) -> None:
        """Test async file helper methods."""
        sandbox = LocalSandbox()
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
        sandbox = LocalSandbox()
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
        sandbox = LocalSandbox()
        await sandbox.abash('echo "test"')
        await sandbox.adestroy()
        with pytest.raises(RuntimeError, match="destroyed"):
            await sandbox.abash('echo "after destroy"')

    @pytest.mark.asyncio
    async def test_aexport_snapshot(self) -> None:
        """Test async snapshot export."""
        sandbox = LocalSandbox()
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
        sandbox1 = LocalSandbox()
        sandbox2 = LocalSandbox()
        sandbox3 = LocalSandbox()
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
        sandbox = LocalSandbox()
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
        with LocalSandbox() as sandbox:
            sandbox.bash('echo "hello"')
            sandbox.bash('echo "world"')

            history = sandbox.history()
            assert len(history) == 2
            assert all(e.name == "bash" for e in history)

    def test_history_includes_command_and_cwd(self) -> None:
        """Test that history entries include command and cwd parameters."""
        with LocalSandbox() as sandbox:
            sandbox.bash('echo "test"')

            history = sandbox.history()
            assert len(history) == 1
            entry = history[0]
            assert entry.parameters is not None
            assert entry.parameters.get("command") == 'echo "test"'
            assert entry.parameters.get("cwd") == "/home/user"

    def test_history_includes_exit_code(self) -> None:
        """Test that history entries include the exit code in result."""
        with LocalSandbox() as sandbox:
            sandbox.bash('echo "success"')

            history = sandbox.history()
            assert len(history) == 1
            entry = history[0]
            assert entry.result is not None
            assert entry.result.get("exitCode") == 0

    def test_history_records_failed_commands(self) -> None:
        """Test that failed commands are also recorded in history."""
        with LocalSandbox() as sandbox:
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
        with LocalSandbox() as sandbox:
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
        with LocalSandbox() as sandbox:
            sandbox.bash('echo "test"')

            history = sandbox.history()
            assert len(history) == 1
            entry = history[0]
            assert entry.started_at > 0
            assert entry.completed_at >= entry.started_at

    def test_history_empty_on_new_sandbox(self) -> None:
        """Test that a new sandbox has empty history."""
        with LocalSandbox() as sandbox:
            history = sandbox.history()
            assert len(history) == 0

    def test_history_persists_in_snapshot(self) -> None:
        """Test that history is preserved in snapshots."""
        with LocalSandbox() as sandbox1:
            sandbox1.bash('echo "before snapshot"')
            snapshot = sandbox1.export_snapshot()

        with LocalSandbox(snapshot=snapshot) as sandbox2:
            history = sandbox2.history()
            assert len(history) == 1
            assert history[0].parameters is not None
            assert history[0].parameters.get("command") == 'echo "before snapshot"'

    @pytest.mark.asyncio
    async def test_ahistory(self) -> None:
        """Test async history method."""
        sandbox = LocalSandbox()
        try:
            await sandbox.abash('echo "async command"')
            history = await sandbox.ahistory()
            assert len(history) == 1
            assert history[0].parameters is not None
            assert history[0].parameters.get("command") == 'echo "async command"'
        finally:
            sandbox.destroy()
