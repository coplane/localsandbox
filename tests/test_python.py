"""Tests for Python code execution via Pyodide."""

import os
import tempfile

import pytest

from bashfs import BashFS


class TestPythonExecution:
    """Tests for Python code execution via Pyodide."""

    def test_python_print(self) -> None:
        """Test basic Python print statement."""
        with BashFS() as sandbox:
            result = sandbox.execute_python('print("hello from python")')
            assert result.stdout.strip() == "hello from python"
            assert result.exit_code == 0

    def test_python_reads_sandbox_file(self) -> None:
        """Test that Python can read files from the sandbox."""
        with BashFS(files={"/data.txt": "sandbox content"}) as sandbox:
            result = sandbox.execute_python("""
with open('/agent/data.txt') as f:
    print(f.read().strip())
""")
            assert result.stdout.strip() == "sandbox content"
            assert result.exit_code == 0

    def test_python_writes_sandbox_file(self) -> None:
        """Test that Python can write files to the sandbox."""
        with BashFS() as sandbox:
            result = sandbox.execute_python("""
with open('/agent/output.txt', 'w') as f:
    f.write('written by python')
""")
            assert result.exit_code == 0

            content = sandbox.read_file("/output.txt")
            assert content.strip() == "written by python"

    def test_python_error_returns_nonzero_exit(self) -> None:
        """Test that Python errors return non-zero exit code."""
        with BashFS() as sandbox:
            result = sandbox.execute_python("raise ValueError('test error')")
            assert result.exit_code != 0
            assert result.error is not None
            # Full traceback is in stderr
            assert "ValueError" in result.stderr

    def test_python_stderr_captured(self) -> None:
        """Test that Python stderr is captured."""
        with BashFS() as sandbox:
            result = sandbox.execute_python("""
import sys
print('to stderr', file=sys.stderr)
""")
            assert "to stderr" in result.stderr

    def test_python_with_cwd(self) -> None:
        """Test Python execution with custom cwd."""
        with BashFS(files={"/project/data.txt": "project data"}) as sandbox:
            result = sandbox.execute_python(
                """
import os
print(os.getcwd())
""",
                cwd="/project",
            )
            assert "/project" in result.stdout or "project" in result.stdout

    def test_python_modifies_existing_file(self) -> None:
        """Test that Python can modify existing sandbox files."""
        with BashFS(files={"/file.txt": "original"}) as sandbox:
            result = sandbox.execute_python("""
with open('/agent/file.txt', 'a') as f:
    f.write(' + modified')
""")
            assert result.exit_code == 0

            content = sandbox.read_file("/file.txt")
            assert "original + modified" in content

    def test_python_creates_directory_and_file(self) -> None:
        """Test that Python can create directories and files."""
        with BashFS() as sandbox:
            result = sandbox.execute_python("""
import os
os.makedirs('/agent/newdir', exist_ok=True)
with open('/agent/newdir/file.txt', 'w') as f:
    f.write('nested content')
""")
            assert result.exit_code == 0
            assert sandbox.exists("/newdir/file.txt")
            content = sandbox.read_file("/newdir/file.txt")
            assert content.strip() == "nested content"

    def test_python_result_fields(self) -> None:
        """Test that PythonResult has all expected fields."""
        with BashFS() as sandbox:
            result = sandbox.execute_python('print("test")')
            assert isinstance(result.stdout, str)
            assert isinstance(result.stderr, str)
            assert isinstance(result.exit_code, int)

    @pytest.mark.asyncio
    async def test_aexecute_python(self) -> None:
        """Test async Python execution."""
        sandbox = BashFS()
        try:
            result = await sandbox.aexecute_python('print("async python")')
            assert result.stdout.strip() == "async python"
            assert result.exit_code == 0
        finally:
            sandbox.destroy()

    def test_python_records_history(self) -> None:
        """Test that Python execution is recorded in history."""
        with BashFS() as sandbox:
            sandbox.execute_python('print("recorded")')
            history = sandbox.history()
            assert len(history) == 1
            assert history[0].name == "python"

    def test_python_filesystem_blocked(self) -> None:
        """Test that Python cannot read arbitrary host files via JS."""
        with tempfile.TemporaryDirectory(prefix="bashfs-host-") as temp_dir:
            target = os.path.join(temp_dir, "secret.txt")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("host secret")

            with BashFS() as sandbox:
                result = sandbox.execute_python(f"""
import js
try:
    js.Deno.readTextFileSync({target!r})
    print("ALLOWED")
except Exception as e:
    print(f"BLOCKED:{{e}}")
""")
                assert result.exit_code == 0
                assert "BLOCKED:" in result.stdout
                assert "NotCapable" in result.stdout

    def test_python_network_blocked(self) -> None:
        """Test that Python code cannot access the network."""
        with BashFS() as sandbox:
            result = sandbox.execute_python("""
import pyodide.http
import asyncio

async def test_net():
    try:
        resp = await pyodide.http.pyfetch("https://example.com")
        return f"ALLOWED:{resp.status}"
    except Exception as e:
        return f"BLOCKED:{type(e).__name__}"

print(asyncio.get_event_loop().run_until_complete(test_net()))
""")
            assert "BLOCKED" in result.stdout
            assert "ALLOWED" not in result.stdout
