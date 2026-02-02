"""Tests for Python code execution via Pyodide."""

import base64
import io
import os
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from localsandbox import LocalSandbox


class TestPythonExecution:
    """Tests for Python code execution via Pyodide."""

    def test_python_print(self) -> None:
        """Test basic Python print statement."""
        with LocalSandbox() as sandbox:
            result = sandbox.execute_python('print("hello from python")')
            assert result.stdout.strip() == "hello from python"
            assert result.exit_code == 0

    def test_python_reads_sandbox_file(self) -> None:
        """Test that Python can read files from the sandbox."""
        with LocalSandbox(files={"/data/data.txt": "sandbox content"}) as sandbox:
            result = sandbox.execute_python("""
with open('/data/data.txt') as f:
    print(f.read().strip())
""")
            assert result.stdout.strip() == "sandbox content"
            assert result.exit_code == 0

    def test_python_writes_sandbox_file(self) -> None:
        """Test that Python can write files to the sandbox."""
        with LocalSandbox() as sandbox:
            result = sandbox.execute_python("""
with open('/data/output.txt', 'w') as f:
    f.write('written by python')
""")
            assert result.exit_code == 0

            content = sandbox.read_file("/data/output.txt")
            assert content.strip() == "written by python"

    def test_python_error_returns_nonzero_exit(self) -> None:
        """Test that Python errors return non-zero exit code."""
        with LocalSandbox() as sandbox:
            result = sandbox.execute_python("raise ValueError('test error')")
            assert result.exit_code != 0
            assert result.error is not None
            # Full traceback is in stderr
            assert "ValueError" in result.stderr

    def test_python_stderr_captured(self) -> None:
        """Test that Python stderr is captured."""
        with LocalSandbox() as sandbox:
            result = sandbox.execute_python("""
import sys
print('to stderr', file=sys.stderr)
""")
            assert "to stderr" in result.stderr

    def test_python_with_cwd(self) -> None:
        """Test Python execution with custom cwd."""
        with LocalSandbox(files={"/data/project/data.txt": "project data"}) as sandbox:
            result = sandbox.execute_python(
                """
import os
print(os.getcwd())
""",
                cwd="/data/project",
            )
            assert "/data/project" in result.stdout or "project" in result.stdout

    @pytest.mark.parametrize("path", ["file.txt", "./file.txt", "/data/file.txt"])
    def test_python_modifies_existing_file(self, path: str) -> None:
        """Test that Python can modify existing sandbox files."""
        with LocalSandbox(files={path: "original"}) as sandbox:
            result = sandbox.execute_python(f"""
with open({path!r}, 'a') as f:
    f.write(' + modified')
""")
            assert result.exit_code == 0

            content = sandbox.read_file("/data/file.txt")
            assert "original + modified" in content

    def test_python_creates_directory_and_file(self) -> None:
        """Test that Python can create directories and files."""
        with LocalSandbox() as sandbox:
            result = sandbox.execute_python("""
import os
os.makedirs('/data/newdir', exist_ok=True)
with open('/data/newdir/file.txt', 'w') as f:
    f.write('nested content')
""")
            assert result.exit_code == 0
            assert sandbox.exists("/data/newdir/file.txt")
            content = sandbox.read_file("/data/newdir/file.txt")
            assert content.strip() == "nested content"

    def test_python_result_fields(self) -> None:
        """Test that PythonResult has all expected fields."""
        with LocalSandbox() as sandbox:
            result = sandbox.execute_python('print("test")')
            assert isinstance(result.stdout, str)
            assert isinstance(result.stderr, str)
            assert isinstance(result.exit_code, int)

    @pytest.mark.asyncio
    async def test_aexecute_python(self) -> None:
        """Test async Python execution."""
        sandbox = LocalSandbox()
        try:
            result = await sandbox.aexecute_python('print("async python")')
            assert result.stdout.strip() == "async python"
            assert result.exit_code == 0
        finally:
            sandbox.destroy()

    def test_python_records_history(self) -> None:
        """Test that Python execution is recorded in history."""
        with LocalSandbox() as sandbox:
            sandbox.execute_python('print("recorded")')
            history = sandbox.history()
            assert len(history) == 1
            assert history[0].name == "python"

    def test_python_filesystem_blocked(self) -> None:
        """Test that Python cannot read arbitrary host files via JS."""
        with tempfile.TemporaryDirectory(prefix="localsandbox-host-") as temp_dir:
            target = os.path.join(temp_dir, "secret.txt")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("host secret")

            with LocalSandbox() as sandbox:
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
        with LocalSandbox() as sandbox:
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

    def test_python_image_crop(self) -> None:
        """Test that Python can crop an image and write the result."""
        receipt_path = Path(__file__).parent / "data" / "receipt.jpg"
        assert receipt_path.exists(), f"Test data not found: {receipt_path}"

        with LocalSandbox(files={"/data/receipt.jpg": receipt_path}) as sandbox:
            # Crop a 200x200 region from the top-left corner
            # PIL is preloaded via preload_packages
            result = sandbox.execute_python(
                """
from PIL import Image
import base64

# Load the image
img = Image.open('/data/receipt.jpg')

# Crop a 200x200 region from the top-left (x1, y1, x2, y2)
cropped = img.crop((0, 0, 200, 200))

# Save the cropped image
cropped.save('/data/cropped.jpg', 'JPEG')

# Read the saved file and output as base64 for verification
with open('/data/cropped.jpg', 'rb') as f:
    data = f.read()
print(f"SIZE:{len(data)}")
print(f"DATA:{base64.b64encode(data).decode('ascii')}")
""",
                preload_packages=["pillow"],
            )
            assert result.exit_code == 0, f"Python execution failed: {result.stderr}"

            # Parse the output
            lines = result.stdout.strip().split("\n")
            size_line = next((line for line in lines if line.startswith("SIZE:")), None)
            data_line = next((line for line in lines if line.startswith("DATA:")), None)

            assert size_line is not None, "SIZE output not found"
            assert data_line is not None, "DATA output not found"

            # Verify the file has reasonable size (should be a few KB)
            size = int(size_line.split(":")[1])
            assert size > 1000, f"Cropped image too small: {size} bytes"

            # Decode and verify it's a valid JPEG
            image_data = base64.b64decode(data_line.split(":")[1])
            assert len(image_data) == size

            # Check JPEG magic bytes (FFD8FF)
            assert image_data[:2] == b"\xff\xd8", "Not a valid JPEG file"

            # Verify dimensions using PIL on the host

            cropped_img = Image.open(io.BytesIO(image_data))
            assert cropped_img.size == (200, 200), (
                f"Unexpected size: {cropped_img.size}"
            )

    def test_python_pdf_create_and_read(self) -> None:
        """Test that Python can create and read PDFs using PyMuPDF."""
        with LocalSandbox() as sandbox:
            # Create a PDF with text, save it, then read it back
            result = sandbox.execute_python(
                """
import fitz  # PyMuPDF

# Create a new PDF document
doc = fitz.open()

# Add a page
page = doc.new_page()

# Add text to the page
text = "Hello from LocalSandbox!"
page.insert_text((50, 50), text, fontsize=12)

# Save the PDF
doc.save('/data/test.pdf')
doc.close()

# Read the PDF back and extract text
doc2 = fitz.open('/data/test.pdf')
page_count = doc2.page_count
page2 = doc2[0]
extracted_text = page2.get_text()
doc2.close()

print(f"PAGES:{page_count}")
print(f"TEXT:{extracted_text.strip()}")
""",
                preload_packages=["pymupdf"],
            )
            assert result.exit_code == 0, f"Python execution failed: {result.stderr}"

            lines = result.stdout.strip().split("\n")
            text_line = next((line for line in lines if line.startswith("TEXT:")), None)
            assert text_line is not None, "TEXT output not found"

            extracted = text_line.split(":", 1)[1]
            assert "Hello from LocalSandbox" in extracted
