"""Repository discovery helpers for GitContext."""

import os
from pathlib import Path

from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

SUPPORTED_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java"}
IGNORED_DIRECTORIES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", "dist", "build",
}


def is_git_repository(path: Path) -> bool:
    """Return whether *path* is the root of a Git repository."""
    try:
        Repo(path, search_parent_directories=False)
        return True
    except (InvalidGitRepositoryError, NoSuchPathError, GitCommandError, OSError):
        return False


def scan_repository(path: Path) -> list[Path]:
    """Find supported source files while skipping generated directories."""
    root = Path(path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    source_files: list[Path] = []
    for current_dir, directories, filenames in os.walk(root):
        directories[:] = sorted(
            directory for directory in directories
            if directory not in IGNORED_DIRECTORIES
        )
        for filename in filenames:
            file_path = Path(current_dir) / filename
            if file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                source_files.append(file_path)

    return sorted(source_files)
