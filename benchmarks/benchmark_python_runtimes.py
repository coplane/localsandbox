"""Compare Python execution across the Pyodide and Monty backends."""

import argparse
import math
import statistics
import time
from dataclasses import dataclass

from localsandbox import (
    LocalSandbox,
    PythonRuntime,
    PythonToolset,
    functions_to_toolset,
)


@dataclass(frozen=True)
class BenchmarkCase:
    """One equivalent workload for both Python runtimes."""

    name: str
    pyodide_code: str
    monty_code: str
    expected_stdout: str

    def code_for(self, runtime: PythonRuntime) -> str:
        """Return the runtime-specific form of this workload."""
        if runtime is PythonRuntime.MONTY:
            return self.monty_code
        return self.pyodide_code


@dataclass(frozen=True)
class BenchmarkResult:
    """Timing samples for one runtime and workload."""

    case: str
    runtime: PythonRuntime
    durations_ms: tuple[float, ...]

    @property
    def mean_ms(self) -> float:
        return statistics.mean(self.durations_ms)

    @property
    def median_ms(self) -> float:
        return statistics.median(self.durations_ms)

    @property
    def p95_ms(self) -> float:
        ordered = sorted(self.durations_ms)
        return ordered[math.ceil(len(ordered) * 0.95) - 1]


def multiply(value: int, factor: int = 2) -> dict[str, int]:
    """Multiply a value by a factor."""
    return {"value": value * factor}


CASES = [
    BenchmarkCase(
        name="stateless computation",
        pyodide_code="print(sum([i * i for i in range(10_000)]))",
        monty_code="print(sum([i * i for i in range(10_000)]))",
        expected_stdout="333283335000",
    ),
    BenchmarkCase(
        name="file manipulation",
        pyodide_code="""
from pathlib import Path

path = Path('/data/benchmark.txt')
path.write_text('localsandbox' * 512)
print(len(path.read_text()))
""",
        monty_code="""
from pathlib import Path

path = Path('/data/benchmark.txt')
path.write_text('localsandbox' * 512)
print(len(path.read_text()))
""",
        expected_stdout="6144",
    ),
    BenchmarkCase(
        name="host tool call",
        pyodide_code="""
from host_tools import call

print(call('multiply', {'value': 21})['value'])
""",
        monty_code="print(multiply(value=21)['value'])",
        expected_stdout="42",
    ),
]


def run_case(
    sandbox: LocalSandbox,
    case: BenchmarkCase,
    runtime: PythonRuntime,
    toolset: PythonToolset,
) -> None:
    """Execute and validate one workload."""
    result = sandbox.execute_python(case.code_for(runtime), toolset=toolset)

    if result.exit_code != 0:
        raise RuntimeError(
            f"{runtime.value} {case.name!r} failed:\n{result.stderr.strip()}"
        )
    actual_stdout = result.stdout.strip()
    if actual_stdout != case.expected_stdout:
        raise RuntimeError(
            f"{runtime.value} {case.name!r} returned {actual_stdout!r}; "
            f"expected {case.expected_stdout!r}"
        )


def measure_execution(
    sandbox: LocalSandbox,
    case: BenchmarkCase,
    runtime: PythonRuntime,
    toolset: PythonToolset,
) -> float:
    """Measure one execution in an existing sandbox."""
    started_at = time.perf_counter()
    run_case(sandbox, case, runtime, toolset)
    return (time.perf_counter() - started_at) * 1000


def measure_cold_start(
    case: BenchmarkCase,
    runtime: PythonRuntime,
    toolset: PythonToolset,
) -> float:
    """Measure sandbox startup through its first successful execution."""
    started_at = time.perf_counter()
    with LocalSandbox(python_runtime=runtime) as sandbox:
        run_case(sandbox, case, runtime, toolset)
        return (time.perf_counter() - started_at) * 1000


def benchmark_runtime(
    runtime: PythonRuntime,
    runs: int,
    warmups: int,
    toolset: PythonToolset,
) -> list[BenchmarkResult]:
    """Benchmark every workload using one persistent, warmed sandbox."""
    results: list[BenchmarkResult] = []
    with LocalSandbox(python_runtime=runtime) as sandbox:
        for case in CASES:
            for _ in range(warmups):
                run_case(sandbox, case, runtime, toolset)

            durations = tuple(
                measure_execution(sandbox, case, runtime, toolset) for _ in range(runs)
            )
            results.append(
                BenchmarkResult(
                    case=case.name,
                    runtime=runtime,
                    durations_ms=durations,
                )
            )
    return results


def benchmark_cold_starts(
    runtime: PythonRuntime,
    runs: int,
    toolset: PythonToolset,
) -> list[BenchmarkResult]:
    """Benchmark startup through first execution using a fresh sandbox each time."""
    return [
        BenchmarkResult(
            case=case.name,
            runtime=runtime,
            durations_ms=tuple(
                measure_cold_start(case, runtime, toolset) for _ in range(runs)
            ),
        )
        for case in CASES
    ]


def format_results(results: list[BenchmarkResult]) -> str:
    """Format benchmark results as a Markdown table."""
    pyodide_means = {
        result.case: result.mean_ms
        for result in results
        if result.runtime is PythonRuntime.PYODIDE
    }
    case_order = {case.name: index for index, case in enumerate(CASES)}
    runtime_order = {
        PythonRuntime.PYODIDE: 0,
        PythonRuntime.MONTY: 1,
    }
    ordered = sorted(
        results,
        key=lambda result: (case_order[result.case], runtime_order[result.runtime]),
    )

    lines = [
        (
            "| Scenario | Runtime | Runs | Mean (ms) | Median (ms) | p95 (ms) | "
            "Min (ms) | Max (ms) | Speed vs Pyodide |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in ordered:
        speedup = pyodide_means[result.case] / result.mean_ms
        lines.append(
            f"| {result.case} | {result.runtime.value} | "
            f"{len(result.durations_ms)} | {result.mean_ms:.2f} | "
            f"{result.median_ms:.2f} | {result.p95_ms:.2f} | "
            f"{min(result.durations_ms):.2f} | {max(result.durations_ms):.2f} | "
            f"{speedup:.2f}x |"
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    """Parse benchmark command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--runs", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument(
        "--cold-start",
        action="store_true",
        help="also measure construction through first execution in fresh sandboxes",
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.warmups < 0:
        parser.error("--warmups cannot be negative")
    return args


def main() -> None:
    """Run the benchmark and print its results."""
    args = parse_args()
    toolset = functions_to_toolset([multiply])
    results: list[BenchmarkResult] = []
    for runtime in (PythonRuntime.PYODIDE, PythonRuntime.MONTY):
        results.extend(benchmark_runtime(runtime, args.runs, args.warmups, toolset))

    print(
        f"Warmed execute_python() latency; {args.warmups} warmup(s) excluded "
        f"per scenario."
    )
    print(format_results(results))
    print(
        "\nTimes include LocalSandbox filesystem synchronization and tool-bridge "
        "overhead, but exclude sandbox construction."
    )

    if args.cold_start:
        cold_results: list[BenchmarkResult] = []
        for runtime in (PythonRuntime.PYODIDE, PythonRuntime.MONTY):
            cold_results.extend(benchmark_cold_starts(runtime, args.runs, toolset))

        print("\nCold-start latency from sandbox construction through first execution.")
        print(format_results(cold_results))
        print("\nCold-start samples exclude sandbox teardown.")


if __name__ == "__main__":
    main()
