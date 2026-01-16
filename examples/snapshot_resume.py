from localsandbox import LocalSandbox
from examples.console import console


def main() -> None:
    console.print("[bold blue]LocalSandbox Snapshot & Resume Example[/bold blue]")

    snapshot = None
    with LocalSandbox() as sandbox:
        console.print("[green]Session 1: Creating data...[/green]")
        console.print("[yellow]Running: echo 'session one' > /notes.txt[/yellow]")
        sandbox.bash('echo "session one" > /notes.txt')
        sandbox.kv.set("session_id", "alpha")

        console.print("[yellow]Exporting snapshot...[/yellow]")
        snapshot = sandbox.export_snapshot()

    console.print("\n[green]Session 2: Resuming from snapshot...[/green]")
    resumed = LocalSandbox(snapshot=snapshot)
    try:
        content = resumed.read_file("/notes.txt").strip()
        console.print(f"Read file content: [bold cyan]{content}[/bold cyan]")

        session_id = resumed.kv.get("session_id")
        console.print(f"Retrieved session_id: [bold cyan]{session_id}[/bold cyan]")
    finally:
        resumed.destroy()


if __name__ == "__main__":
    main()
