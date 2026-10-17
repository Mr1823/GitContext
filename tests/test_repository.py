from pathlib import Path

from git import Repo
from typer.testing import CliRunner

from gitcontext.cli import app
from gitcontext.ingest.repository import is_git_repository, scan_repository


def test_scan_finds_supported_files_and_skips_ignored_directories(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.PY").write_text("print('ok')")
    (source / "app.ts").write_text("export {}")
    (source / "notes.md").write_text("not source")
    ignored = tmp_path / "node_modules" / "package"
    ignored.mkdir(parents=True)
    (ignored / "index.js").write_text("ignored")

    assert scan_repository(tmp_path) == [
        source / "app.ts", source / "main.PY", source / "notes.md",
    ]


def test_scan_rejects_non_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "file.py"
    file_path.write_text("pass")
    try:
        scan_repository(file_path)
    except NotADirectoryError:
        pass
    else:
        raise AssertionError("Expected NotADirectoryError")


def test_is_git_repository_checks_the_given_root(tmp_path: Path) -> None:
    assert not is_git_repository(tmp_path)
    Repo.init(tmp_path)
    assert is_git_repository(tmp_path)


def test_scan_cli_keeps_hello_and_reports_scan_results(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('ok')")
    runner = CliRunner()

    hello_result = runner.invoke(app, ["hello"])
    scan_result = runner.invoke(app, ["scan", str(tmp_path)])

    assert hello_result.exit_code == 0
    assert "GitContext is running successfully!" in hello_result.output
    assert scan_result.exit_code == 0
    assert "Source files found" in scan_result.output
    assert "main.py" in scan_result.output
