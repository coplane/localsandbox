from localsandbox import LocalSandbox

from examples.console import console


def main() -> None:
    console.print("[bold blue]LocalSandbox Basic Usage Example[/bold blue]")

    with LocalSandbox() as sandbox:
        console.print("[green]Sandbox created[/green]")
        result = sandbox.bash('echo "Hello, LocalSandbox!"')

        console.print(
            f"[green]Command:[/green] echo 'Hello, LocalSandbox!' [green]Output:[/green] {result.stdout.strip()}"
        )

        file_path = "/notes/todo.txt"
        content = "buy milk\nship release\n"

        console.print(f"[yellow]Writing to '{file_path}':[/yellow]")
        console.print_code(content.strip())

        sandbox.write_file(file_path, content)

        read_content = sandbox.read_file(file_path)
        console.print(f"[yellow]Reading from '{file_path}':[/yellow]")
        console.print_code(read_content.strip())

        files = sandbox.list_files("/notes")
        console.print(f"Files in '/notes': [bold cyan]{files}[/bold cyan]")

        console.print(f"[yellow]Deleting '{file_path}'...[/yellow]")
        sandbox.delete_file(file_path)


if __name__ == "__main__":
    main()
