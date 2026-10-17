from pathlib import Path

from typer.testing import CliRunner

from gitcontext.answer import answer_question
from gitcontext.chunking import chunk_file
from gitcontext.cli import app
from gitcontext.config import Settings
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

