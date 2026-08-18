from examples.console import console
from localsandbox import LocalSandbox


def main() -> None:
    console.print("[bold blue]LocalSandbox KV Store & History Example[/bold blue]")

    with LocalSandbox() as sandbox:
        console.print("[green]Setting KV entries...[/green]")
        sandbox.kv.set("user:42:name", "Ada")
        sandbox.kv.set("user:42:role", "engineer")

        keys = sandbox.kv.keys("user:42:")
        console.print(f"Keys matching 'user:42:': [bold cyan]{keys}[/bold cyan]")

        console.print("\n[green]Running some commands...[/green]")
        sandbox.bash('echo "first"')
        sandbox.execute_python('print("second")')

        console.print("\n[green]Execution History (last 5):[/green]")
        for entry in sandbox.history(limit=5):
            params = entry.parameters or {}
            console.print(f" - [bold]{entry.name}[/bold]: {params}")


if __name__ == "__main__":
    main()
