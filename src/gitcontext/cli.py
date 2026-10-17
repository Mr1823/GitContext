import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from gitcontext.answer import answer_question
from gitcontext.config import Settings
from gitcontext.evaluation import (
    citation_accuracy,
    evaluate_retrieval,
    extract_citations,
    token_f1,
)
from gitcontext.indexer import index_repository, repository_is_stale
from gitcontext.ingest.repository import is_git_repository, scan_repository
from gitcontext.retrieval import retrieve_with_metrics
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
EVALUATION_FIXTURE_ARGUMENT = typer.Argument(
    ..., exists=True, dir_okay=False, readable=True, help="JSON evaluation fixture."
)
EVALUATION_PATH_OPTION = typer.Option(
    Path.cwd(), "--path", "-p", exists=True, file_okay=False,
    dir_okay=True, resolve_path=True, help="Repository to evaluate.",
)
EVALUATION_K_OPTION = typer.Option(5, min=1, max=20, help="Ranking cutoff for retrieval metrics.")


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
    table.add_row("Total chunks indexed", str(summary.chunks))
    table.add_row("Semantic embeddings", "Enabled" if summary.semantic_embeddings else "Off")
    table.add_row("Changed files indexed", str(summary.changed_files))
    table.add_row("Removed files", str(summary.removed_files))
    table.add_row("Git commits indexed", str(summary.commits))
    table.add_row("Pull requests indexed", str(summary.pull_requests))
    table.add_row("Incidents indexed", str(summary.incidents))
    table.add_row("Local index", str(summary.database_path))
    console.print(table)
    for warning in summary.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


@app.command()
def ask(
    question: str = ASK_QUESTION_ARGUMENT,
    path: Path = ASK_PATH_OPTION,
    top_k: int = TOP_K_OPTION,
) -> None:
    """Search the local index and answer with repository evidence."""
    settings = Settings.for_repository(path)
    try:
        if repository_is_stale(path):
            summary = index_repository(path, refresh_external=False)
            console.print(
                f"[dim]Updated stale index: {summary.changed_files} changed files, "
                f"{summary.removed_files} removed files.[/dim]"
            )
        store = VectorStore(settings.database_path)
        if store.count() == 0:
            console.print("[yellow]The index is empty. Run `gitcontext index` first.[/yellow]")
            raise typer.Exit(code=1)
        results, elapsed_ms = retrieve_with_metrics(
            question, store, top_k, settings.embedding_model, settings.reranker_model
        )
        response = answer_question(question, results, settings)
    except (OSError, RuntimeError, ValueError) as error:
        console.print(f"[bold red]Question failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error
    console.print(response)
    console.print(f"[dim]Retrieval: {elapsed_ms:.1f} ms[/dim]")


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="Address to bind the local web UI."),
    port: int = typer.Option(8765, min=1, max=65535, help="Port for the web UI."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the UI in a browser."),
) -> None:
    """Start the local GitContext web interface."""
    import threading
    import webbrowser

    import uvicorn

    from gitcontext.web import app as web_app

    url = f"http://{host}:{port}"
    console.print(f"GitContext web UI: [bold cyan]{url}[/bold cyan]")
    console.print("Press Ctrl+C to stop the server.")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(web_app, host=host, port=port, log_level="info")


@app.command()
def evaluate(
    fixture: Path = EVALUATION_FIXTURE_ARGUMENT,
    path: Path = EVALUATION_PATH_OPTION,
    k: int = EVALUATION_K_OPTION,
) -> None:
    """Evaluate retrieval against a JSON list of questions and relevant file paths."""
    try:
        cases = json.loads(fixture.read_text(encoding="utf-8"))
        if not isinstance(cases, list) or any(
            not isinstance(case, dict)
            or not isinstance(case.get("question"), str)
            or not isinstance(case.get("relevant_paths", []), list)
            for case in cases
        ):
            raise ValueError("Fixture must contain question and relevant_paths objects.")
        settings = Settings.for_repository(path)
        store = VectorStore(settings.database_path)
        result_sets = []
        metrics = evaluate_retrieval(
            cases,
            lambda question: _evaluate_results(
                question, store, k, settings.embedding_model, settings.reranker_model,
                result_sets,
            ),
            k,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        console.print(f"[bold red]Evaluation failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error
    table = Table(title="GitContext Retrieval Evaluation")
    table.add_column("Metric", style="cyan")
    table.add_column("Result", style="green")
    table.add_row(f"Recall@{k}", f"{metrics['recall_at_k']:.3f}")
    table.add_row(f"Precision@{k}", f"{metrics['precision_at_k']:.3f}")
    table.add_row("Mean reciprocal rank", f"{metrics['mrr']:.3f}")
    table.add_row("Average retrieval latency", f"{metrics['avg_latency_ms']:.1f} ms")
    table.add_row("Queries", str(metrics["queries"]))
    if result_sets:
        citation_scores = []
        answer_scores = []
        for case, results in zip(cases, result_sets):
            response = answer_question(case["question"], results, settings)
            citation_scores.append(citation_accuracy(extract_citations(response), results))
            if isinstance(case.get("reference_answer"), str):
                answer_scores.append(token_f1(response, case["reference_answer"]))
        if citation_scores:
            table.add_row("Citation accuracy", f"{sum(citation_scores) / len(citation_scores):.3f}")
        if answer_scores:
            table.add_row("Answer token F1", f"{sum(answer_scores) / len(answer_scores):.3f}")
    console.print(table)


def _evaluate_results(question, store, limit, embedding_model, reranker_model, result_sets):
    results, _ = retrieve_with_metrics(
        question, store, limit, embedding_model, reranker_model
    )
    result_sets.append(results)
    return results


if __name__ == "__main__":
    app()
