from examples.console import console
from localsandbox import LocalSandbox

CSV_DATA = """item,qty
apple,2
banana,5
"""

PYTHON_CODE = """\
import csv
from pathlib import Path

total = 0
with open("inventory.csv", newline="") as handle:
    for row in csv.DictReader(handle):
        total += int(row["qty"])

Path("summary.txt").write_text(f"total_qty={total}\\n")
print(total)
"""


def main() -> None:
    console.print("[bold blue]LocalSandbox Python Roundtrip Example[/bold blue]")

    console.print("\n[green]Input CSV Data:[/green]")
    console.print_code(CSV_DATA.strip())

    console.print("\n[green]Python Code to Execute:[/green]")
    console.print_code(PYTHON_CODE.strip())

    with LocalSandbox(files={"inventory.csv": CSV_DATA}) as sandbox:
        console.print("\n[yellow]Executing Python code...[/yellow]")
        print(sandbox.bash("ls -l /").stdout.strip())
        print(sandbox.bash("ls -l /data/").stdout.strip())
        result = sandbox.execute_python(PYTHON_CODE, cwd="/data")
        console.print(f"Python Stdout: [bold cyan]{result.stdout.strip()}[/bold cyan]")

        console.print("\n[yellow]Reading generated summary file...[/yellow]")
        summary = sandbox.read_file("/data/summary.txt")
        console.print_code(summary.strip())

        console.print("\n[green]Execution History (last 5):[/green]")
        for entry in sandbox.history(limit=5):
            params = entry.parameters or {}
            console.print(f" - [bold]{entry.name}[/bold]: {params}")


if __name__ == "__main__":
    main()
