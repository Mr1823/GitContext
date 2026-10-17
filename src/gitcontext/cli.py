from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from gitcontext.answer import answer_question
from gitcontext.config import Settings
from gitcontext.indexer import index_repository
from gitcontext.ingest.repository import is_git_repository, scan_repository
from gitcontext.retrieval import retrieve
from gitcontext.vector_store import VectorStore

app = typer.Typer(
    name="gitcontext",
    help="AI-powered codebase intelligence and developer copilot.",
    no_args_is_help=True,
)
console = Console()
REPOSITORY_PATH_ARGUMENT = typer.Argument(
    ...,
    exists=True,
    file_okay=False,
    dir_okay=True,
    readable=True,
    resolve_path=True,
    help="Path to the repository to scan.",
)
ASK_PATH_OPTION = typer.Option(
    Path.cwd(), "--path", "-p", exists=True, file_okay=False,
    dir_okay=True, resolve_path=True, help="Repository to search.",
)
TOP_K_OPTION = typer.Option(6, "--top-k", min=1, max=20, help="Maximum evidence chunks.")
ASK_QUESTION_ARGUMENT = typer.Argument(..., help="Question to ask about the repository.")


@app.callback()
def main() -> None:
    """GitContext: Codebase Intelligence & Developer Copilot."""


@app.command()
def hello() -> None:
    """Check that GitContext is running."""
    console.print("[bold green]GitContext is running successfully![/bold green]")


@app.command()
def scan(
    path: Path = REPOSITORY_PATH_ARGUMENT,
) -> None:
    """Discover supported source files in a repository."""
    is_git = is_git_repository(path)
    source_files = scan_repository(path)

    table = Table(title="GitContext Repository Scan")
    table.add_column("Property", style="cyan")
    table.add_column("Result", style="green")
    table.add_row("Repository path", str(path))
    table.add_row("Git repository", "Yes" if is_git else "No")
    table.add_row("Source files found", str(len(source_files)))
    console.print(table)

    if source_files:
        console.print("\n[bold]Discovered files[/bold]")
        for file_path in source_files:
            console.print(f"  {file_path.relative_to(path)}")
    else:
        console.print("[yellow]No supported source files found.[/yellow]")


@app.command()
def index(
    path: Path = REPOSITORY_PATH_ARGUMENT,
) -> None:
    """Parse source files and build a local searchable index."""
    try:
        summary = index_repository(path)
    except (OSError, RuntimeError, ValueError) as error:
        console.print(f"[bold red]Indexing failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error

    table = Table(title="GitContext Index")
    table.add_column("Property", style="cyan")
    table.add_column("Result", style="green")
    table.add_row("Repository", str(summary.repository))
    table.add_row("Source files", str(summary.source_files))
    table.add_row("Files skipped", str(summary.skipped_files))
    table.add_row("Code chunks indexed", str(summary.chunks))
    table.add_row("Semantic embeddings", "Enabled" if summary.semantic_embeddings else "Off")
    table.add_row("Local index", str(summary.database_path))
    console.print(table)


@app.command()
def ask(
    question: str = ASK_QUESTION_ARGUMENT,
    path: Path = ASK_PATH_OPTION,
    top_k: int = TOP_K_OPTION,
) -> None:
    """Search the local index and answer with repository evidence."""
    settings = Settings.for_repository(path)
    try:
        store = VectorStore(settings.database_path)
        if store.count() == 0:
            console.print("[yellow]The index is empty. Run `gitcontext index` first.[/yellow]")
            raise typer.Exit(code=1)
        results = retrieve(question, store, top_k, settings.embedding_model)
        response = answer_question(question, results, settings)
    except (OSError, RuntimeError, ValueError) as error:
        console.print(f"[bold red]Question failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error
    console.print(response)


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="Address to bind the local web UI."),
    port: int = typer.Option(8765, min=1, max=65535, help="Port for the web UI."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the UI in a browser."),
) -> None:
    """Start the local GitContext web interface."""
    import threading
    import webbrowser

    from gitcontext.web import create_server

    server = create_server(host, port)
    url = f"http://{host}:{server.server_address[1]}"
    console.print(f"GitContext web UI: [bold cyan]{url}[/bold cyan]")
    console.print("Press Ctrl+C to stop the server.")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\nStopping GitContext web UI.")
    finally:
        server.server_close()


if __name__ == "__main__":
    app()
