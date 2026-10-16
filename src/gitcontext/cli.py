
import typer
from rich.console import Console

app = typer.Typer(
    name="gitcontext",
    help="AI-powered codebase intelligence and developer copilot.",
    no_args_is_help=True,
)

console = Console()


@app.callback()
def main() -> None:
    """GitContext: Codebase Intelligence & Developer Copilot."""
    pass


@app.command()
def hello() -> None:
    """Check that GitContext is running."""
    console.print(
        "[bold green]GitContext is running successfully![/bold green]"
    )


if __name__ == "__main__":
    app()
