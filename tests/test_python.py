"""Tests for Python code execution via Pyodide."""

import asyncio
import base64
import io
import json
import os
import tempfile
import time
from pathlib import Path

import pytest
from PIL import Image

from localsandbox import (
    LocalSandbox,
    PythonToolset,
    ToolDefinition,
    function_to_tool_definition,
)
from localsandbox.core import JsonValue
from localsandbox.exceptions import TimeoutError


def _write_fake_server(script_path: Path, source: str) -> Path:
    """Write a minimal Deno server script for timeout tests."""
    script_path.write_text(source, encoding="utf-8")
    return script_path


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

    def test_python_cwd_is_reset_between_calls(self) -> None:
        """Each execution should start in the requested cwd."""
        with LocalSandbox(files={"/data/sub/file.txt": "x"}) as sandbox:
            first = sandbox.execute_python(
                """
import os
os.chdir("/data/sub")
print(os.getcwd())
"""
            )
            assert first.exit_code == 0, first.stderr
            assert first.stdout.strip() == "/data/sub"

            second = sandbox.execute_python("""
import os
print(os.getcwd())
""")
            assert second.exit_code == 0, second.stderr
            assert second.stdout.strip() == "/data"

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

    def test_python_delete_persists_to_sandbox(self) -> None:
        """Files deleted in Python should be removed from the sandbox."""
        with LocalSandbox(files={"/data/delete-me.txt": "bye"}) as sandbox:
            result = sandbox.execute_python("""
import os
os.remove("/data/delete-me.txt")
""")
            assert result.exit_code == 0, result.stderr
            assert not sandbox.exists("/data/delete-me.txt")

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

    def test_python_preload_packages_after_warmup(self) -> None:
        """Package preloading should work after an earlier non-preloaded call."""
        with LocalSandbox() as sandbox:
            warmup = sandbox.execute_python('print("warmup")')
            assert warmup.exit_code == 0, warmup.stderr

            result = sandbox.execute_python(
                """
from PIL import Image
print(Image.__name__)
""",
                preload_packages=["pillow"],
            )
            assert result.exit_code == 0, result.stderr
            assert result.stdout.strip() == "PIL.Image"

    @pytest.mark.asyncio
    async def test_concurrent_requests_share_server_safely(self) -> None:
        """Concurrent requests on one sandbox should not corrupt responses."""
        sandbox = LocalSandbox(files={"/data/data.txt": "hello"})
        try:
            read_task = asyncio.create_task(sandbox.aread_file("/data/data.txt"))
            exec_task = asyncio.create_task(
                sandbox.aexecute_python(
                    """
import time
time.sleep(0.1)
print("done")
"""
                )
            )
            content, result = await asyncio.gather(read_task, exec_task)
            assert content == "hello"
            assert result.exit_code == 0, result.stderr
            assert result.stdout.strip() == "done"
        finally:
            sandbox.destroy()


class TestPythonTools:
    """Tests for Python execution with host tools."""

    @staticmethod
    def _echo_toolset() -> PythonToolset:
        return PythonToolset(
            definitions=[
                ToolDefinition(
                    name="echo",
                    description="Echo back a string value.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                        },
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    output_schema={
                        "type": "object",
                        "properties": {
                            "echo": {"type": "string"},
                        },
                        "required": ["echo"],
                        "additionalProperties": False,
                    },
                )
            ],
            handlers={
                "echo": lambda payload: {"echo": str(payload["text"])},
            },
        )

    @staticmethod
    def _search_toolset() -> PythonToolset:
        success_schema = {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
            },
            "required": ["ok"],
            "additionalProperties": False,
        }
        definitions = [
            ToolDefinition(
                name="slack_post_message",
                description="Post a message to Slack channels and threads.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["channel", "text"],
                    "additionalProperties": False,
                },
                output_schema=success_schema,
            ),
            ToolDefinition(
                name="github_search_issues",
                description="Search GitHub issues in a repository.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "query": {"type": "string"},
                    },
                    "required": ["repo", "query"],
                    "additionalProperties": False,
                },
                output_schema=success_schema,
            ),
            ToolDefinition(
                name="web_lookup",
                description="Search the web for public information.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                output_schema=success_schema,
            ),
        ]
        return PythonToolset(
            definitions=definitions,
            handlers={
                definition.name: lambda payload: {"ok": True}
                for definition in definitions
            },
        )

    def test_function_to_tool_definition(self) -> None:
        """Callable inference should derive the full tool definition."""

        def greet(name: str, excited: bool = False) -> dict[str, str]:
            """Greet a user."""
            suffix = "!" if excited else "."
            return {"message": f"Hello, {name}{suffix}"}

        assert function_to_tool_definition(greet) == ToolDefinition(
            name="greet",
            description="Greet a user.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "excited": {"type": "boolean", "default": False},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        )

    def test_function_to_tool_definition_anyof(self) -> None:
        """Inference should preserve richer Pydantic schema constructs."""

        def maybe_echo(text: str | None) -> dict[str, str]:
            """Echo text if present."""
            return {"echo": text or ""}

        assert function_to_tool_definition(maybe_echo) == ToolDefinition(
            name="maybe_echo",
            description="Echo text if present.",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "null"},
                        ]
                    }
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        )

    def test_python_calls_host_tool(self) -> None:
        """Python code can call an SDK-provided host tool."""
        with LocalSandbox() as sandbox:
            result = sandbox.execute_python(
                """
from host_tools import call

response = call("echo", {"text": "hello bridge"})
print(response["echo"])
""",
                toolset=self._echo_toolset(),
            )

            assert result.exit_code == 0, result.stderr
            assert result.stdout.strip() == "hello bridge"

    def test_python_accepts_callable_toolsets(self) -> None:
        """execute_python() can infer a toolset from bound methods/functions."""

        class Helpers:
            def repeat(self, text: str, count: int = 2) -> dict[str, str]:
                """Repeat text a configurable number of times."""
                return {"text": text * count}

        helpers = Helpers()

        with LocalSandbox() as sandbox:
            result = sandbox.execute_python(
                """
import json
from host_tools import call, search

response = call("repeat", {"text": "ha"})
matches = search("repeat", detail="full", limit=1)
print(response["text"])
print(json.dumps(matches[0]))
""",
                toolset=[helpers.repeat],
            )

            assert result.exit_code == 0, result.stderr
            lines = result.stdout.strip().splitlines()
            assert lines[0] == "haha"
            payload = json.loads(lines[1])
            assert payload["name"] == "repeat"
            assert (
                payload["description"] == "Repeat text a configurable number of times."
            )
            assert payload["input_schema"]["required"] == ["text"]
            assert payload["input_schema"]["properties"]["count"]["default"] == 2

    def test_callable_toolsets_use_pydantic_validation(self) -> None:
        """Inferred callable toolsets should validate inputs with Pydantic."""

        def maybe_repeat(count: int | None) -> dict[str, int]:
            """Return the requested count."""
            return {"count": count or 0}

        with LocalSandbox() as sandbox:
            result = sandbox.execute_python(
                """
from host_tools import call

call("maybe_repeat", {"count": "not-an-int"})
""",
                toolset=[maybe_repeat],
            )

            assert result.exit_code != 0
            assert "validation_error" in result.stderr

    def test_python_tool_input_validation_failure(self) -> None:
        """Schema validation failures are returned through the bridge."""
        with LocalSandbox() as sandbox:
            result = sandbox.execute_python(
                """
from host_tools import call

call("echo", {"text": 123})
""",
                toolset=self._echo_toolset(),
            )

            assert result.exit_code != 0
            assert "validation_error" in result.stderr

    def test_python_calls_async_host_tool(self) -> None:
        """Async tool handlers are supported."""

        async def reverse(payload: dict[str, JsonValue]) -> JsonValue:
            await asyncio.sleep(0)
            text = str(payload["text"])
            return {"reversed": text[::-1]}

        toolset = PythonToolset(
            definitions=[
                ToolDefinition(
                    name="reverse",
                    description="Reverse a string.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                        },
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    output_schema={
                        "type": "object",
                        "properties": {
                            "reversed": {"type": "string"},
                        },
                        "required": ["reversed"],
                        "additionalProperties": False,
                    },
                )
            ],
            handlers={"reverse": reverse},
        )

        with LocalSandbox() as sandbox:
            result = sandbox.execute_python(
                """
from host_tools import call

response = call("reverse", {"text": "drawer"})
print(response["reversed"])
""",
                toolset=toolset,
            )

            assert result.exit_code == 0, result.stderr
            assert result.stdout.strip() == "reward"

    def test_python_tool_calls_record_history(self) -> None:
        """Bridge executions record both python and python_tool_call history."""
        with LocalSandbox() as sandbox:
            sandbox.execute_python(
                """
from host_tools import call

response = call("echo", {"text": "history"})
print(response["echo"])
""",
                toolset=self._echo_toolset(),
            )

            history_names = [entry.name for entry in sandbox.history(limit=10)]
            assert "python" in history_names
            assert "python_tool_call" in history_names

    def test_python_can_search_declared_tools(self) -> None:
        """The host_tools module can rank declared tools and return details."""
        with LocalSandbox() as sandbox:
            result = sandbox.execute_python(
                """
import json
from host_tools import search

brief = search("slack")
full = search("slack message", detail="full", limit=1)
print(json.dumps({"brief": brief, "full": full}))
""",
                toolset=self._search_toolset(),
            )

            assert result.exit_code == 0, result.stderr
            payload = json.loads(result.stdout)
            assert [entry["name"] for entry in payload["brief"]] == [
                "slack_post_message"
            ]
            assert "input_schema" not in payload["brief"][0]
            assert payload["full"][0]["name"] == "slack_post_message"
            assert payload["full"][0]["input_schema"]["properties"]["channel"] == {
                "type": "string"
            }

    def test_python_execution_state_persists_between_calls(self) -> None:
        """Compatible executions reuse interpreter state."""
        with LocalSandbox() as sandbox:
            toolset = self._echo_toolset()
            r1 = sandbox.execute_python(
                'MY_GLOBAL = 42\nprint("set")',
                toolset=toolset,
            )
            assert r1.exit_code == 0, r1.stderr
            r2 = sandbox.execute_python(
                "print(MY_GLOBAL)",
                toolset=toolset,
            )
            assert r2.exit_code == 0, r2.stderr
            assert r2.stdout.strip() == "42"

    def test_python_persistent_bridge_output_isolation(self) -> None:
        """stdout from first call doesn't leak into second call's result."""
        with LocalSandbox() as sandbox:
            toolset = self._echo_toolset()
            r1 = sandbox.execute_python('print("first")', toolset=toolset)
            assert "first" in r1.stdout
            r2 = sandbox.execute_python('print("second")', toolset=toolset)
            assert "second" in r2.stdout
            assert "first" not in r2.stdout

    def test_python_persistent_bridge_file_changes(self) -> None:
        """sandbox.write_file() between tool-enabled calls is visible."""
        with LocalSandbox() as sandbox:
            toolset = self._echo_toolset()
            sandbox.execute_python('print("warmup")', toolset=toolset)
            sandbox.write_file("/data/between.txt", "injected")
            result = sandbox.execute_python(
                'print(open("/data/between.txt").read())',
                toolset=toolset,
            )
            assert result.exit_code == 0, result.stderr
            assert result.stdout.strip() == "injected"

    def test_python_host_delete_is_visible_between_calls(self) -> None:
        """Host-side deletions should be visible to the next Python call."""
        with LocalSandbox() as sandbox:
            toolset = self._echo_toolset()
            sandbox.write_file("/data/gone.txt", "bye")
            sandbox.execute_python('print("warmup")', toolset=toolset)
            sandbox.delete_file("/data/gone.txt")
            result = sandbox.execute_python(
                """
from pathlib import Path
print(Path("/data/gone.txt").exists())
""",
                toolset=toolset,
            )
            assert result.exit_code == 0, result.stderr
            assert result.stdout.strip() == "False"

    def test_python_preload_change_restarts_runner(self) -> None:
        """Changing preload packages should restart the runner."""
        with LocalSandbox() as sandbox:
            first = sandbox.execute_python('MARKER = "warm"\nprint("set")')
            assert first.exit_code == 0, first.stderr

            second = sandbox.execute_python(
                """
try:
    print(MARKER)
except NameError:
    print("not found")
from PIL import Image
print(Image.__name__)
""",
                preload_packages=["pillow"],
            )
            assert second.exit_code == 0, second.stderr
            assert second.stdout.strip().splitlines() == ["not found", "PIL.Image"]

    def test_python_different_toolset_restarts_runner(self) -> None:
        """Changing the tool manifest should restart the runner."""
        with LocalSandbox() as sandbox:
            toolset1 = self._echo_toolset()
            sandbox.execute_python('BRIDGE_ID = "first"', toolset=toolset1)
            toolset2 = PythonToolset(
                definitions=[
                    ToolDefinition(
                        name="noop",
                        description="Does nothing.",
                        input_schema={
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    )
                ],
                handlers={"noop": lambda payload: {}},
            )
            result = sandbox.execute_python(
                """
try:
    print(BRIDGE_ID)
except NameError:
    print("not found")
""",
                toolset=toolset2,
            )
            assert result.exit_code == 0, result.stderr
            assert result.stdout.strip() == "not found"

    def test_python_tool_timeout_returns_promptly(self) -> None:
        """Timed-out tools should return control quickly to the caller."""

        def slow_write(payload: dict[str, JsonValue]) -> JsonValue:
            time.sleep(0.2)
            return {"ok": True}

        toolset = PythonToolset(
            definitions=[
                ToolDefinition(
                    name="slow_write",
                    description="Sleeps and writes a marker file.",
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    output_schema={
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                    timeout_ms=50,
                )
            ],
            handlers={"slow_write": slow_write},
        )

        with LocalSandbox() as sandbox:
            warmup = sandbox.execute_python('print("warmup")', toolset=toolset)
            assert warmup.exit_code == 0, warmup.stderr

            started_at = time.monotonic()
            result = sandbox.execute_python(
                """
from host_tools import call

try:
    call("slow_write", {})
except Exception as exc:
    print(exc)
""",
                toolset=toolset,
            )
            elapsed_s = time.monotonic() - started_at

            assert result.exit_code == 0, result.stderr
            assert "timed out" in result.stdout
            assert elapsed_s < 0.15


class TestServerTimeouts:
    """Tests for bounded server handshake and response waits."""

    def test_server_startup_timeout_is_bounded(self, tmp_path: Path) -> None:
        """Startup should fail fast if the server never sends a ready envelope."""
        server_path = _write_fake_server(
            tmp_path / "hang-before-ready.ts",
            """
await new Promise((resolve) => setTimeout(resolve, 5000));
""",
        )

        sandbox = LocalSandbox()
        sandbox._server_path = server_path
        sandbox._server_startup_timeout_ms = 50

        try:
            with pytest.raises(TimeoutError):
                sandbox.read_file("/data/does-not-matter.txt")
        finally:
            sandbox.destroy()

    def test_server_response_timeout_is_bounded(self, tmp_path: Path) -> None:
        """Operations should time out if the server stops responding."""
        server_path = _write_fake_server(
            tmp_path / "hang-after-ready.ts",
            """
console.log(JSON.stringify({ type: "ready" }));
await Deno.stdin.read(new Uint8Array(1024));
await new Promise((resolve) => setTimeout(resolve, 5000));
""",
        )

        sandbox = LocalSandbox()
        sandbox._server_path = server_path
        sandbox._server_startup_timeout_ms = 50
        sandbox._default_request_timeout_ms = 50

        try:
            with pytest.raises(TimeoutError):
                sandbox.read_file("/data/does-not-matter.txt")
        finally:
            sandbox.destroy()
