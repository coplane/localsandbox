class Console:
    """Simple helper to print colorful output without external dependencies."""

    # ANSI Colors
    RESET = "\033[0m"
    BOLD = "\033[1m"
    BLUE = "\033[34m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"

    def print(self, text: str = "") -> None:
        # Simple tag replacement for basic rich-like syntax
        text = str(text)
        replacements = [
            ("[bold blue]", self.BLUE + self.BOLD),
            ("[/bold blue]", self.RESET),
            ("[blue]", self.BLUE),
            ("[/blue]", self.RESET),
            ("[bold green]", self.GREEN + self.BOLD),
            ("[/bold green]", self.RESET),
            ("[green]", self.GREEN),
            ("[/green]", self.RESET),
            ("[yellow]", self.YELLOW),
            ("[/yellow]", self.RESET),
            ("[bold red]", self.RED + self.BOLD),
            ("[/bold red]", self.RESET),
            ("[red]", self.RED),
            ("[/red]", self.RESET),
            ("[bold cyan]", self.CYAN + self.BOLD),
            ("[/bold cyan]", self.RESET),
            ("[cyan]", self.CYAN),
            ("[/cyan]", self.RESET),
            ("[bold]", self.BOLD),
            ("[/bold]", self.RESET),
        ]

        for tag, code in replacements:
            text = text.replace(tag, code)

        print(text + self.RESET)

    def print_code(self, code: str) -> None:
        """Prints indented code block."""
        lines = code.split("\n")
        for line in lines:
            print(f"  {line}")


console = Console()
