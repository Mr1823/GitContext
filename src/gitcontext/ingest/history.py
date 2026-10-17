"""Extract repository file provenance and commit history from Git."""

from pathlib import Path

from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError

from gitcontext.ingest.parser import ParsedBlock


def file_provenance(repository: Path, relative_path: str) -> dict[str, str | None]:
    """Return the last commit, author, and timestamp affecting a file."""
    try:
        repo = Repo(repository, search_parent_directories=False)
        raw = repo.git.log("-1", "--format=%H%x1f%an%x1f%aI", "--", relative_path).strip()
    except (InvalidGitRepositoryError, GitCommandError, OSError):
        return {"commit_sha": None, "author": None, "last_modified": None}
    if not raw:
        return {"commit_sha": None, "author": None, "last_modified": None}
    parts = raw.split("\x1f", maxsplit=2)
    return {
        "commit_sha": parts[0] if parts else None,
        "author": parts[1] if len(parts) > 1 else None,
        "last_modified": parts[2] if len(parts) > 2 else None,
    }


def commit_documents(repository: Path, limit: int = 200) -> list[ParsedBlock]:
    """Turn recent Git commits into searchable metadata chunks."""
    if limit <= 0:
        return []
    try:
        repo = Repo(repository, search_parent_directories=False)
        commits = repo.iter_commits("--all", max_count=limit)
    except (InvalidGitRepositoryError, GitCommandError, OSError):
        return []
    documents = []
    for commit in commits:
        authored = commit.authored_datetime.isoformat()
        changed_paths = sorted(commit.stats.files)
        message = commit.message.strip()
        content = (
            f"Commit {commit.hexsha}\nAuthor: {commit.author.name}\n"
            f"Date: {authored}\nMessage:\n{message}\n"
            f"Changed files:\n" + "\n".join(changed_paths[:100])
        )
        documents.append(ParsedBlock(
            path=f"history/commit/{commit.hexsha[:12]}",
            start_line=1,
            end_line=max(1, len(content.splitlines())),
            symbol=commit.summary[:160],
            content=content,
            source_type="commit",
            commit_sha=commit.hexsha,
            author=commit.author.name,
            last_modified=authored,
        ))
    return documents
