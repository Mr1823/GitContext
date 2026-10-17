from pathlib import Path

from git import Repo
from typer.testing import CliRunner

from gitcontext.answer import answer_question
from gitcontext.chunking import chunk_file
from gitcontext.cli import app
from gitcontext.config import Settings
from gitcontext.evaluation import precision_at_k, recall_at_k, reciprocal_rank, token_f1
from gitcontext.indexer import index_repository, repository_is_stale
from gitcontext.ingest.history import commit_documents, file_provenance
from gitcontext.ingest.parser import parse_file
from gitcontext.retrieval import retrieve
from gitcontext.vector_store import VectorStore


def test_python_parser_extracts_named_declarations(tmp_path: Path) -> None:
    source = tmp_path / "auth.py"
    source.write_text("class Auth:\n    def login(self):\n        return True\n")

    blocks = parse_file(source, tmp_path)

    assert [block.symbol for block in blocks] == ["Auth", "login"]
    assert blocks[1].start_line == 2
    assert "return True" in blocks[1].content


def test_javascript_parser_extracts_classes_and_functions(tmp_path: Path) -> None:
    source = tmp_path / "auth.js"
    source.write_text(
        "export class Login { submit() { return true; } }\n"
        "export function authenticateUser() { return true; }\n"
    )

    blocks = parse_file(source, tmp_path)

    assert {block.symbol for block in blocks} >= {"Login", "submit", "authenticateUser"}


def test_typescript_and_java_declarations_are_structural(tmp_path: Path) -> None:
    typescript = tmp_path / "service.ts"
    typescript.write_text("export class Service { run(): void {} }\n")
    java = tmp_path / "Service.java"
    java.write_text("class Service { void run() {} }\n")

    ts_symbols = {block.symbol for block in parse_file(typescript, tmp_path)}
    java_symbols = {block.symbol for block in parse_file(java, tmp_path)}

    assert {"Service", "run"} <= ts_symbols
    assert {"Service", "run"} <= java_symbols


def test_markdown_parser_chunks_by_heading(tmp_path: Path) -> None:
    source = tmp_path / "README.md"
    source.write_text("# Setup\nInstall it.\n\n## Run\nStart the app.\n")

    blocks = parse_file(source, tmp_path)

    assert [block.symbol for block in blocks] == ["Setup", "Run"]
    assert blocks[0].source_type == "documentation"


def test_git_history_and_file_provenance_are_indexable(tmp_path: Path) -> None:
    repository = Repo.init(tmp_path)
    writer = repository.config_writer()
    writer.set_value("user", "name", "GitContext Tester")
    writer.set_value("user", "email", "tester@example.com")
    writer.release()
    source = tmp_path / "app.py"
    source.write_text("def run():\n    return True\n")
    repository.index.add(["app.py"])
    commit = repository.index.commit("Add app entry point")

    provenance = file_provenance(tmp_path, "app.py")
    history = commit_documents(tmp_path, limit=5)

    assert provenance["commit_sha"] == commit.hexsha
    assert provenance["author"] == "GitContext Tester"
    assert history
    assert history[0].source_type == "commit"
    assert "app.py" in history[0].content


def test_indexing_only_rebuilds_changed_and_removed_files(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "main.py"
    source.write_text("def run_app():\n    return True\n")
    docs = repository / "README.md"
    docs.write_text("# Project\nDescription\n")
    monkeypatch.setenv("GITCONTEXT_DATABASE", str(tmp_path / "index.sqlite"))

    first = index_repository(repository)
    assert not repository_is_stale(repository)
    second = index_repository(repository)
    source.write_text("def run_app():\n    return False\n")
    docs.unlink()
    assert repository_is_stale(repository)
    third = index_repository(repository)

    assert first.changed_files == 2
    assert second.changed_files == 0
    assert third.changed_files == 1
    assert third.removed_files == 1
    assert not repository_is_stale(repository)


def test_large_block_is_split_with_line_overlap(tmp_path: Path) -> None:
    source = tmp_path / "long.js"
    source.write_text("\n".join(f"line_{i}" for i in range(120)))

    chunks = chunk_file(source, tmp_path, max_chars=80, overlap_lines=2)

    assert len(chunks) > 1
    assert chunks[0].start_line == 1
    assert chunks[1].start_line <= chunks[0].end_line


def test_store_replaces_and_retrieves_relevant_chunks(tmp_path: Path) -> None:
    file_path = tmp_path / "auth.py"
    file_path.write_text("def authenticate_user(token):\n    return token\n")
    chunks = chunk_file(file_path, tmp_path)
    store = VectorStore(tmp_path / "index.sqlite")
    store.replace_all(chunks)

    results = retrieve("authenticate user token", store)

    assert results
    assert results[0]["path"] == "auth.py"
    store.replace_all([])
    assert store.count() == 0


def test_qdrant_vector_search_returns_nearest_chunk(tmp_path: Path) -> None:
    store = VectorStore(tmp_path / "index.sqlite")
    source = tmp_path / "auth.py"
    source.write_text("def login():\n    return True\n")
    chunks = chunk_file(source, tmp_path)
    store.replace_all(chunks, [[1.0, 0.0] for _ in chunks])
    store.sync_qdrant()

    matches = store.semantic_search([1.0, 0.0], limit=3)

    assert matches
    assert max(matches.values()) == 1.0


def test_answer_without_api_key_returns_cited_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = Settings.for_repository(tmp_path)
    result = {
        "path": "auth.py", "start_line": 4, "end_line": 6,
        "content": "def login():\n    return True", "symbol": "login", "score": 1.0,
    }

    answer = answer_question("Where is login?", [result], settings)

    assert "auth.py:4-6" in answer
    assert "def login()" in answer


def test_question_cli_workflow_uses_repository_local_index(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "auth.py"
    source.write_text("def authenticate_user(token):\n    return token\n")
    database = tmp_path / ".gitcontext" / "index.sqlite"
    monkeypatch.setenv("GITCONTEXT_DATABASE", str(database))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = VectorStore(database)
    store.replace_all(chunk_file(source, tmp_path))

    result = CliRunner().invoke(app, ["ask", "authenticate user", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "auth.py:1-2" in result.output


def test_evaluation_metrics() -> None:
    retrieved = ["src/a.py", "src/b.py"]
    relevant = {"src/a.py", "src/c.py"}

    assert recall_at_k(retrieved, relevant, 2) == 0.5
    assert precision_at_k(retrieved, relevant, 2) == 0.5
    assert reciprocal_rank(retrieved, relevant) == 1.0
    assert token_f1("return user data", "return user data") == 1.0

