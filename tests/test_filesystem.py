"""Filesystem tests for BashFS."""

import tempfile
from pathlib import Path

import pytest

from bashfs import (
    BashFS,
    CommandError,
    FileNotFoundError,
)


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
