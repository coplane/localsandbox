# Examples

Run from the repo root, for example:

```bash
python examples/basic_usage.py
```

Scripts:
- `basic_usage.py`: Basic bash + file helper usage.
- `python_roundtrip.py`: Run Python via Pyodide and read/write sandbox files.
- `snapshot_resume.py`: Export a snapshot and resume a new sandbox.
- `kv_and_history.py`: Use the KV store and inspect tool history.

Notes:
- `execute_python` runs inside Pyodide and sees the sandbox mounted at `/data`.
- First Python execution may be slower while Pyodide caches.
