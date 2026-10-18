"""Repository indexing workflow shared by the CLI and web interface."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError

from gitcontext.chunking import chunk_block, chunk_file
from gitcontext.config import Settings
from gitcontext.embeddings import encode_texts
from gitcontext.ingest.github import (
    github_commit_documents,
    github_documents,
    github_documents_for_slug,
    repository_slug,
)
from gitcontext.ingest.history import commit_documents, file_provenance
from gitcontext.ingest.parser import ParsedBlock
from gitcontext.ingest.remote import (
    RemoteRepository,
    ensure_remote_snapshot,
    get_remote_repository,
)
from gitcontext.ingest.repository import scan_repository
from gitcontext.vector_store import VectorStore


@dataclass(frozen=True)
class IndexSummary:
    repository: Path | str
    source_files: int
    skipped_files: int
    chunks: int
    semantic_embeddings: bool
    database_path: Path
    changed_files: int = 0
    removed_files: int = 0
    commits: int = 0
    pull_requests: int = 0
    incidents: int = 0
    warnings: tuple[str, ...] = ()


def _embed(chunks: list[ParsedBlock], model: str | None) -> list[list[float]] | None:
    return encode_texts([chunk.content for chunk in chunks], model) if model else None


def _head_sha(repository: Path) -> str:
    try:
        return Repo(repository, search_parent_directories=False).head.commit.hexsha
    except (InvalidGitRepositoryError, GitCommandError, ValueError):
        return ""


def repository_is_stale(repository: Path) -> bool:
    """Compare indexed file hashes and HEAD to current repository state."""
    root = Path(repository).resolve()
    settings = Settings.for_repository(root)
    store = VectorStore(settings.database_path)
    if store.value("embedding_model") != (settings.embedding_model or ""):
        return True
    indexed = store.file_hashes()
    files = scan_repository(root)
    current_paths = {file.relative_to(root).as_posix() for file in files}
    if current_paths != set(indexed):
        return True
    if store.value("head_sha") != _head_sha(root):
        return True
    for file in files:
        relative_path = file.relative_to(root).as_posix()
        try:
            digest = sha256(file.read_bytes()).hexdigest()
        except OSError:
            return True
        if indexed.get(relative_path) != digest:
            return True
    return store.count() == 0 and bool(files)


def index_repository(
    repository: Path,
    refresh_external: bool = True,
    *,
    remote_url: str | None = None,
    remote_head_sha: str | None = None,
) -> IndexSummary:
    """Incrementally update source files and refresh Git/GitHub context."""
    root = Path(repository).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")
    settings = Settings.for_repository(root)
    store = VectorStore(settings.database_path)
    files = scan_repository(root)
    prior_hashes = store.file_hashes()
    active_model = settings.embedding_model or ""
    model_changed = store.value("embedding_model") != active_model
    skipped = 0
    changed = 0
    existing_paths: set[str] = set()

    for file_path in files:
        relative_path = file_path.relative_to(root).as_posix()
        existing_paths.add(relative_path)
        try:
            content = file_path.read_bytes()
        except OSError:
            skipped += 1
            continue
        digest = sha256(content).hexdigest()
        if prior_hashes.get(relative_path) == digest and not model_changed:
            continue
        chunks = chunk_file(file_path, root)
        if not chunks:
            skipped += 1
        provenance = file_provenance(root, relative_path)
        if remote_head_sha:
            provenance["commit_sha"] = remote_head_sha
            provenance["source_url"] = (
                f"{remote_url}/blob/{remote_head_sha}/"
                f"{quote(relative_path, safe='/')}"
            ) if remote_url else None
        if not provenance["last_modified"]:
            provenance["last_modified"] = datetime.fromtimestamp(
                file_path.stat().st_mtime, tz=UTC
            ).isoformat()
        chunks = [replace(chunk, **provenance) for chunk in chunks]
        store.replace_file(relative_path, digest, chunks, _embed(chunks, settings.embedding_model))
        changed += 1

    removed = store.remove_missing_files(existing_paths)
    store.set_value("embedding_model", active_model)

    current_head = _head_sha(root)
    history_limit_changed = store.value("history_limit") != str(settings.history_limit)
    refresh_history = (
        refresh_external
        or current_head != store.value("head_sha")
        or history_limit_changed
        or store.count("commit") == 0
    )
    commits = []
    if refresh_history:
        commits = [
            passage
            for document in commit_documents(root, settings.history_limit)
            for passage in chunk_block(document)
        ]
        store.replace_source_type("commit", commits, _embed(commits, settings.embedding_model))
        store.set_value("history_limit", str(settings.history_limit))
    warnings: list[str] = []
    if refresh_external and settings.github_enabled and repository_slug(root):
        try:
            pull_documents, incident_documents = github_documents(root, settings.github_token)
            pulls = [passage for document in pull_documents for passage in chunk_block(document)]
            incidents = [
                passage for document in incident_documents for passage in chunk_block(document)
            ]
            store.replace_source_type(
                "pull_request", pulls, _embed(pulls, settings.embedding_model)
            )
            store.replace_source_type(
                "incident", incidents, _embed(incidents, settings.embedding_model)
            )
        except RuntimeError as error:
            warnings.append(str(error))

    store.sync_qdrant()
    store.set_value("head_sha", remote_head_sha or current_head)
    indexed_records = store.all()
    indexed_commits = {
        row["commit_sha"] for row in indexed_records
        if row["source_type"] == "commit" and row["commit_sha"]
    }
    indexed_pulls = {
        row["pr_number"] for row in indexed_records
        if row["source_type"] == "pull_request" and row["pr_number"]
    }
    indexed_incidents = {
        row["path"] for row in indexed_records if row["source_type"] == "incident"
    }
    return IndexSummary(
        root, len(files), skipped, store.count(), bool(settings.embedding_model),
        settings.database_path, changed, removed, len(indexed_commits),
        len(indexed_pulls), len(indexed_incidents), tuple(warnings),
    )


def prepare_remote_repository(
    value: str,
    token: str | None = None,
    *,
    public_only: bool = False,
) -> tuple[RemoteRepository, list[Path]]:
    """Resolve a GitHub URL and make its current supported files available to scan."""
    remote = get_remote_repository(value, token, public_only=public_only)
    root = ensure_remote_snapshot(remote, token)
    return remote, scan_repository(root)


def remote_repository_is_stale(
    value: str,
    token: str | None = None,
    remote: RemoteRepository | None = None,
    *,
    public_only: bool = False,
) -> tuple[bool, RemoteRepository]:
    """Check whether the persisted remote index matches GitHub's latest commit."""
    remote = remote or get_remote_repository(value, token, public_only=public_only)
    settings = Settings.for_repository(remote.cache_path)
    store = VectorStore(settings.database_path)
    stale = (
        store.count() == 0
        or store.value("remote_head_sha") != remote.head_sha
        or store.value("embedding_model") != (settings.embedding_model or "")
    )
    return stale, remote


def index_remote_repository(
    value: str,
    *,
    remote: RemoteRepository | None = None,
    public_only: bool = False,
) -> IndexSummary:
    """Fetch and index a GitHub repository without a user-side Git clone."""
    token = Settings.for_repository(Path.cwd()).github_token
    remote = remote or get_remote_repository(value, token, public_only=public_only)
    ensure_remote_snapshot(remote, token)
    summary = index_repository(
        remote.cache_path,
        refresh_external=False,
        remote_url=remote.url,
        remote_head_sha=remote.head_sha,
    )
    settings = Settings.for_repository(remote.cache_path)
    store = VectorStore(settings.database_path)
    warnings = list(summary.warnings)

    if settings.github_enabled:
        try:
            commit_limit = min(settings.history_limit, 30)
            commits = [
                passage
                for document in github_commit_documents(remote.slug, token, commit_limit)
                for passage in chunk_block(document)
            ]
            store.replace_source_type(
                "commit", commits, _embed(commits, settings.embedding_model)
            )
            pull_documents, incident_documents = github_documents_for_slug(remote.slug, token)
            pulls = [
                passage for document in pull_documents for passage in chunk_block(document)
            ]
            incidents = [
                passage for document in incident_documents for passage in chunk_block(document)
            ]
            store.replace_source_type(
                "pull_request", pulls, _embed(pulls, settings.embedding_model)
            )
            store.replace_source_type(
                "incident", incidents, _embed(incidents, settings.embedding_model)
            )
        except RuntimeError as error:
            warnings.append(str(error))

    store.set_value("remote_head_sha", remote.head_sha)
    store.set_value("remote_url", remote.url)
    store.set_value("head_sha", remote.head_sha)
    store.sync_qdrant()
    indexed_records = store.all()
    indexed_commits = {
        row["commit_sha"] for row in indexed_records
        if row["source_type"] == "commit" and row["commit_sha"]
    }
    indexed_pulls = {
        row["pr_number"] for row in indexed_records
        if row["source_type"] == "pull_request" and row["pr_number"]
    }
    indexed_incidents = {
        row["path"] for row in indexed_records if row["source_type"] == "incident"
    }
    return replace(
        summary,
        repository=remote.url,
        chunks=store.count(),
        commits=len(indexed_commits),
        pull_requests=len(indexed_pulls),
        incidents=len(indexed_incidents),
        warnings=tuple(warnings),
    )
