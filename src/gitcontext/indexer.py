"""Repository indexing workflow shared by the CLI and web interface."""

from dataclasses import dataclass
from pathlib import Path

from gitcontext.chunking import chunk_file
from gitcontext.config import Settings
from gitcontext.embeddings import encode_texts
from gitcontext.ingest.repository import scan_repository
from gitcontext.vector_store import VectorStore


@dataclass(frozen=True)
class IndexSummary:
    repository: Path
    source_files: int
    skipped_files: int
    chunks: int
    semantic_embeddings: bool
    database_path: Path


def index_repository(repository: Path) -> IndexSummary:
    """Build or replace the local index for a repository."""
    root = Path(repository).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")
    settings = Settings.for_repository(root)
    files = scan_repository(root)
    chunks = []
    skipped = 0
    for file_path in files:
        parsed = chunk_file(file_path, root)
        if parsed:
            chunks.extend(parsed)
        else:
            skipped += 1
    embeddings = (
        encode_texts([chunk.content for chunk in chunks], settings.embedding_model)
        if settings.embedding_model
        else None
    )
    VectorStore(settings.database_path).replace_all(chunks, embeddings)
    return IndexSummary(
        root, len(files), skipped, len(chunks), embeddings is not None,
        settings.database_path,
    )
