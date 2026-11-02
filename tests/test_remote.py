import io
import tarfile
from pathlib import Path

import pytest

from gitcontext import indexer
from gitcontext.config import Settings
from gitcontext.ingest.errors import GitHubAPIError
from gitcontext.ingest.remote import (
    RemoteRepository,
    _cache_root,
    ensure_remote_snapshot,
    get_remote_repository,
    parse_github_url,
)
from gitcontext.vector_store import VectorStore


def test_remote_cache_uses_data_dir_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GITCONTEXT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("GITCONTEXT_REMOTE_CACHE", raising=False)
    assert _cache_root() == (tmp_path / "data" / "remotes").resolve()


def test_parse_github_repository_urls() -> None:
    assert parse_github_url("https://github.com/Owner/repo") == "Owner/repo"
    assert parse_github_url("https://github.com/Owner/repo.git/") == "Owner/repo"
    assert parse_github_url("git@github.com:Owner/repo.git") == "Owner/repo"
    assert parse_github_url("https://gitlab.com/Owner/repo") is None
    assert parse_github_url("https://github.com/Owner/repo/tree/main") is None


def test_public_web_resolution_rejects_private_repositories(monkeypatch) -> None:
    monkeypatch.setattr(
        "gitcontext.ingest.remote._github_json",
        lambda url, token: {"private": True, "default_branch": "main"},
    )

    with pytest.raises(ValueError, match="public GitHub repositories only"):
        get_remote_repository("https://github.com/owner/repo", public_only=True)


def test_public_web_resolution_reports_private_or_missing_repositories(monkeypatch) -> None:
    def missing_repository(url, token):
        raise GitHubAPIError("GitHub repository was not found or is private.", 404)

    monkeypatch.setattr(
        "gitcontext.ingest.remote._github_json",
        missing_repository,
    )

    with pytest.raises(ValueError, match="Check the URL and repository visibility"):
        get_remote_repository("https://github.com/owner/repo", public_only=True)


def test_remote_snapshot_fetches_supported_files_and_reuses_same_commit(
    tmp_path: Path, monkeypatch
) -> None:
    archive_bytes = io.BytesIO()
    files = {
        "src/main.py": b"def main():\n    return True\n",
        "README.md": b"# Online repository\n",
        "node_modules/pkg/index.js": b"ignored()\n",
        "../outside.py": b"unsafe()\n",
    }
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(f"owner-repo-sha/{name}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    payload = archive_bytes.getvalue()
    downloads = []

    def fake_urlopen(request, timeout):
        downloads.append(request.full_url)
        return io.BytesIO(payload)

    monkeypatch.setattr("gitcontext.ingest.remote.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "gitcontext.ingest.remote._github_json",
        lambda url, token: {
            "tree": [{"type": "blob", "path": "src/main.py"}],
            "truncated": False,
        },
    )
    remote = RemoteRepository(
        "owner/repo", "https://github.com/owner/repo", "main", "abc123", tmp_path / "cache"
    )

    root = ensure_remote_snapshot(remote)
    from gitcontext.ingest.repository import scan_repository

    assert [path.relative_to(root).as_posix() for path in scan_repository(root)] == [
        "README.md", "src/main.py"
    ]
    ensure_remote_snapshot(remote)
    assert len(downloads) == 1


def test_remote_repository_size_limit_is_checked_before_commit_lookup(monkeypatch) -> None:
    monkeypatch.setenv("GITCONTEXT_MAX_REPO_MB", "1")
    calls = []

    def github_json(url, token):
        calls.append(url)
        return {"private": False, "default_branch": "main", "size": 1025}

    monkeypatch.setattr("gitcontext.ingest.remote._github_json", github_json)
    with pytest.raises(ValueError, match="configured limit is 1 MB"):
        get_remote_repository("https://github.com/owner/repo", public_only=True)
    assert len(calls) == 1


def test_remote_blob_count_limit_is_checked_before_archive_fetch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITCONTEXT_MAX_FILES", "2")
    monkeypatch.setattr(
        "gitcontext.ingest.remote._github_json",
        lambda url, token: {
            "tree": [{"type": "blob", "path": f"{n}.py"} for n in range(3)],
            "truncated": False,
        },
    )
    monkeypatch.setattr(
        "gitcontext.ingest.remote.urlopen",
        lambda *args, **kwargs: pytest.fail("archive must not be fetched above the blob cap"),
    )
    remote = RemoteRepository(
        "owner/repo", "https://github.com/owner/repo", "main", "abc123", tmp_path / "cache"
    )
    with pytest.raises(ValueError, match="contains 3 files.*configured limit is 2"):
        ensure_remote_snapshot(remote)


def test_remote_index_uses_managed_snapshot_and_github_source_citations(
    tmp_path: Path, monkeypatch
) -> None:
    cache = tmp_path / "cache" / "owner" / "repo"
    cache.mkdir(parents=True)
    (cache / "auth.py").write_text(
        "def authenticate(token):\n    return token is not None\n", encoding="utf-8"
    )
    remote = RemoteRepository(
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
        "abc123def456",
        cache,
    )
    monkeypatch.setenv("GITCONTEXT_DATABASE", str(tmp_path / "remote-index.sqlite"))
    monkeypatch.setenv("GITCONTEXT_INGEST_GITHUB", "false")
    monkeypatch.setattr(
        indexer, "ensure_remote_snapshot", lambda repository, token=None: repository.cache_path
    )
    sync_calls = []
    monkeypatch.setattr(VectorStore, "sync_qdrant", lambda self: sync_calls.append(self.database_path))

    summary = indexer.index_remote_repository(remote.url, remote=remote)
    store = VectorStore(Settings.for_repository(cache).database_path)
    code = next(row for row in store.all() if row["path"] == "auth.py")

    assert summary.repository == remote.url
    assert summary.source_files == 1
    assert code["commit_sha"] == remote.head_sha
    assert code["source_url"] == (
        f"{remote.url}/blob/{remote.head_sha}/auth.py"
    )
    assert len(sync_calls) == 1
    stale, resolved = indexer.remote_repository_is_stale(remote.url, remote=remote)
    assert not stale
    assert resolved == remote
