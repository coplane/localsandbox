# Benchmarks

Run the Python runtime comparison from the repository root:

```bash
uv run python benchmarks/benchmark_python_runtimes.py --runs 10
```

The benchmark reuses one warmed sandbox per backend and compares end-to-end
`execute_python()` latency for pure computation, persistent file manipulation,
and an injected host-tool call. Warmups and sandbox construction are excluded
from the reported samples.

Pass `--cold-start` to also report timings from constructing a fresh sandbox
through its first execution of each workload:

```bash
uv run python benchmarks/benchmark_python_runtimes.py --runs 10 --cold-start
```

Cold-start samples include sandbox construction and runtime startup, but exclude
sandbox teardown. They can take substantially longer because every sample starts
a new Pyodide or Monty runtime.
