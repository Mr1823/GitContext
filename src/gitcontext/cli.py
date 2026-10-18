import json
from hashlib import sha256
from pathlib import Path

import typer
from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError
from rich.console import Console
from rich.table import Table

from gitcontext.answer import answer_question
from gitcontext.config import Settings
from gitcontext.diagram import generate_diagram, source_extension
from gitcontext.evaluation import (
    citation_accuracy,
    evaluate_retrieval,
    extract_citations,
    token_f1,
)
from gitcontext.indexer import (
    index_remote_repository,
    index_repository,
    prepare_remote_repository,
    remote_repository_is_stale,
    repository_is_stale,
)
from gitcontext.ingest.remote import is_github_url
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
    ..., help="Local repository folder or GitHub repository URL."
)
ASK_PATH_OPTION = typer.Option(
    str(Path.cwd()), "--path", "-p", help="Local repository folder or GitHub repository URL."
)
TOP_K_OPTION = typer.Option(6, "--top-k", min=1, max=20, help="Maximum evidence chunks.")
DIAGRAM_OUTPUT_OPTION = typer.Option(
    Path("."), "--out", file_okay=False, dir_okay=True,
    help="Directory for diagram.json and diagram.mmd.",
)
INCLUDE_TESTS_OPTION = typer.Option(
    False, "--include-tests", help="Include test source files in the diagram."
)
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
    path: str = REPOSITORY_PATH_ARGUMENT,
) -> None:
    """Discover supported files in a local repository or online GitHub repository."""
    if is_github_url(path):
        try:
            settings = Settings.for_repository(Path.cwd())
            remote, source_files = prepare_remote_repository(path, settings.github_token)
            repository_path = remote.url
            is_git = True
            root = remote.cache_path
        except (OSError, RuntimeError, ValueError) as error:
            console.print(f"[bold red]Scan failed:[/bold red] {error}")
            raise typer.Exit(code=1) from error
    else:
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            console.print(f"[bold red]Repository folder does not exist:[/bold red] {root}")
            raise typer.Exit(code=1)
        repository_path = str(root)
        is_git = is_git_repository(root)
        source_files = scan_repository(root)

    table = Table(title="GitContext Repository Scan")
    table.add_column("Property", style="cyan")
    table.add_column("Result", style="green")
    table.add_row("Repository", repository_path)
    table.add_row("Git repository", "Yes" if is_git else "No")
    table.add_row("Source files found", str(len(source_files)))
    console.print(table)

    if source_files:
        console.print("\n[bold]Discovered files[/bold]")
        for file_path in source_files:
            console.print(f"  {file_path.relative_to(root)}")
    else:
        console.print("[yellow]No supported source files found.[/yellow]")


@app.command()
def index(
    path: str = REPOSITORY_PATH_ARGUMENT,
) -> None:
    """Parse a local folder or online GitHub repository and build its index."""
    try:
        if is_github_url(path):
            summary = index_remote_repository(path)
        else:
            root = Path(path).expanduser().resolve()
            if not root.is_dir():
                raise NotADirectoryError(f"Repository folder does not exist: {root}")
            summary = index_repository(root)
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
def diagram(
    path: str = REPOSITORY_PATH_ARGUMENT,
    out: Path = DIAGRAM_OUTPUT_OPTION,
    include_tests: bool = INCLUDE_TESTS_OPTION,
) -> None:
    """Generate an architecture diagram from an existing repository index."""
    try:
        token = Settings.for_repository(Path.cwd()).github_token
        if is_github_url(path):
            remote, files = prepare_remote_repository(path, token)
            root = remote.cache_path
            repository_identity = remote.url
            commit_sha = remote.head_sha
        else:
            root = Path(path).expanduser().resolve()
            if not root.is_dir():
                raise NotADirectoryError(f"Repository folder does not exist: {root}")
            files = scan_repository(root)
            repository_identity = str(root)
            try:
                commit_sha = Repo(root, search_parent_directories=False).head.commit.hexsha
            except (InvalidGitRepositoryError, GitCommandError, ValueError):
                commit_sha = None

        settings = Settings.for_repository(root)
        if not settings.database_path.is_file():
            raise RuntimeError("No existing index was found. Run `gitcontext index` first.")
        store = VectorStore(settings.database_path)
        if is_github_url(path):
            if (store.value("remote_url") != repository_identity
                    or store.value("remote_head_sha") != commit_sha):
                raise RuntimeError(
                    "The GitHub index is missing or stale. Run `gitcontext index` first."
                )
        elif is_git_repository(root) and store.value("head_sha") != (commit_sha or ""):
            raise RuntimeError("The repository index is stale. Run `gitcontext index` first.")

        paths = [file.relative_to(root).as_posix() for file in files]
        source_files = [file for file in paths if source_extension(file) in {
            ".py", ".js", ".jsx", ".ts", ".tsx"
        }]
        contents = {}
        hashes = store.file_hashes()
        for relative in source_files:
            file_path = root / relative
            try:
                raw_content = file_path.read_bytes()
                content = raw_content.decode("utf-8")
            except (OSError, UnicodeError) as error:
                raise RuntimeError(f"Could not read indexed source file: {relative}") from error
            contents[relative] = content
            if hashes.get(relative) != sha256(raw_content).hexdigest():
                raise RuntimeError(
                    f"The index does not match {relative}. Run `gitcontext index` first."
                )
        readme = ""
        readme_file = next((file for file in files
                            if file.parent == root and file.stem.lower() == "readme"), None)
        if readme_file:
            try:
                readme = readme_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                readme = ""

        run_diagnostics: list[dict[str, str]] = []
        result = generate_diagram(
            root, repository_identity, commit_sha, paths, contents, readme, store, settings,
            stored_hashes=hashes, include_tests=include_tests,
            run_diagnostics=run_diagnostics,
        )
        out = out.expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        (out / "diagram.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (out / "diagram.mmd").write_text(result["mermaid"], encoding="utf-8")
    except (OSError, RuntimeError, ValueError) as error:
        console.print(f"[bold red]Diagram generation failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error

    table = Table(title="GitContext Architecture Diagram")
    table.add_column("Property", style="cyan")
    table.add_column("Result", style="green")
    table.add_row("Nodes", str(len(result["nodes"])))
    table.add_row("Edges", str(len(result["edges"])))
    table.add_row("LLM calls", str(result["llm_calls_made"]))
    table.add_row(
        "LLM retries", str(sum(item["type"] == "retry" for item in run_diagnostics))
    )
    rejected_labels = [
        item["reason"] for item in run_diagnostics
        if item["type"] == "rejected_response" and item["phase"] == "labels"
        and item["reason"].startswith(("Node label ", "Edge label ", "Label "))
    ]
    table.add_row(
        "Rejected label rules", "; ".join(rejected_labels) if rejected_labels else "None"
    )
    table.add_row("Cache", "Hit" if result["cache_hit"] else "Miss")
    table.add_row("Labels", result["labels_status"])
    table.add_row(
        "Not analysed",
        ", ".join(f'{item["extension"]}: {item["file_count"]}'
                   for item in result["not_analysed"]) or "None",
    )
    table.add_row("Output", str(out))
    console.print(table)


@app.command()
def ask(
    question: str = ASK_QUESTION_ARGUMENT,
    path: str = ASK_PATH_OPTION,
    top_k: int = TOP_K_OPTION,
) -> None:
    """Search a local or online GitHub repository index and answer with evidence."""
    try:
        if is_github_url(path):
            token = Settings.for_repository(Path.cwd()).github_token
            stale, remote = remote_repository_is_stale(path, token)
            if stale:
                summary = index_remote_repository(path, remote=remote)
                console.print(
                    f"[dim]Updated remote index at {remote.head_sha[:8]}: "
                    f"{summary.changed_files} changed files.[/dim]"
                )
            repository = remote.cache_path
        else:
            repository = Path(path).expanduser().resolve()
            if not repository.is_dir():
                raise NotADirectoryError(f"Repository folder does not exist: {repository}")
        settings = Settings.for_repository(repository)
        if not is_github_url(path) and repository_is_stale(repository):
            summary = index_repository(repository, refresh_external=False)
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
