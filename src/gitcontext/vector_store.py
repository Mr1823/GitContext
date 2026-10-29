"""Persistent chunk metadata, freshness manifest, and local Qdrant vectors."""

import json
import sqlite3
from pathlib import Path
from typing import Any

from gitcontext.records import ParsedBlock


class VectorStore:
    """Use SQLite as the metadata source of truth and Qdrant for dense search."""

    COLLECTION = "gitcontext_chunks"

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    embedding TEXT,
                    source_type TEXT NOT NULL DEFAULT 'code',
                    commit_sha TEXT,
                    author TEXT,
                    last_modified TEXT,
                    pr_number INTEGER,
                    source_url TEXT,
                    content_hash TEXT
                )
            """)
            self._ensure_columns(connection)
            connection.execute("CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path)")
            connection.execute("CREATE INDEX IF NOT EXISTS chunks_type ON chunks(source_type)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection) -> None:
        existing = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}
        migrations = {
            "source_type": "TEXT NOT NULL DEFAULT 'code'",
            "commit_sha": "TEXT",
            "author": "TEXT",
            "last_modified": "TEXT",
            "pr_number": "INTEGER",
            "source_url": "TEXT",
            "content_hash": "TEXT",
        }
        for name, declaration in migrations.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE chunks ADD COLUMN {name} {declaration}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def value(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_value(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def file_hashes(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT path,content_hash FROM files").fetchall()
        return {row["path"]: row["content_hash"] for row in rows}

    def replace_file(
        self,
        path: str,
        content_hash: str,
        chunks: list[ParsedBlock],
        embeddings: list[list[float]] | None = None,
    ) -> None:
        """Atomically replace one changed source file and update its manifest."""
        if embeddings is not None and len(embeddings) != len(chunks):
            raise ValueError("Embedding count must match chunk count")
        with self._connect() as connection:
            connection.execute("DELETE FROM chunks WHERE path=?", (path,))
            connection.execute("DELETE FROM files WHERE path=?", (path,))
            connection.execute(
                "INSERT INTO files(path,content_hash) VALUES(?,?)", (path, content_hash)
            )
            self._insert_chunks(connection, chunks, embeddings, content_hash)

    def remove_missing_files(self, existing_paths: set[str]) -> int:
        """Delete stale file records and their chunks; return deleted file count."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT path FROM chunks WHERE source_type IN ('code','documentation')"
            ).fetchall()
            removed = [row["path"] for row in rows if row["path"] not in existing_paths]
            for path in removed:
                connection.execute("DELETE FROM chunks WHERE path=?", (path,))
                connection.execute("DELETE FROM files WHERE path=?", (path,))
        return len(removed)

    def replace_source_type(
        self,
        source_type: str,
        chunks: list[ParsedBlock],
        embeddings: list[list[float]] | None = None,
    ) -> None:
        """Replace volatile history, PR, or incident chunks."""
        if embeddings is not None and len(embeddings) != len(chunks):
            raise ValueError("Embedding count must match chunk count")
        with self._connect() as connection:
            connection.execute("DELETE FROM chunks WHERE source_type=?", (source_type,))
            self._insert_chunks(connection, chunks, embeddings)

    @staticmethod
    def _insert_chunks(
        connection: sqlite3.Connection,
        chunks: list[ParsedBlock],
        embeddings: list[list[float]] | None,
        content_hash: str | None = None,
    ) -> None:
        for index, chunk in enumerate(chunks):
            connection.execute(
                """INSERT INTO chunks(
                    path,start_line,end_line,symbol,content,embedding,source_type,
                    commit_sha,author,last_modified,pr_number,source_url,content_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    chunk.path, chunk.start_line, chunk.end_line, chunk.symbol,
                    chunk.content,
                    json.dumps(embeddings[index]) if embeddings is not None else None,
                    chunk.source_type, chunk.commit_sha, chunk.author,
                    chunk.last_modified, chunk.pr_number, chunk.source_url,
                    content_hash,
                ),
            )

    def replace_all(
        self,
        chunks: list[ParsedBlock],
        embeddings: list[list[float]] | None = None,
    ) -> None:
        """Replace the index atomically; retained for simple imports/tests."""
        if embeddings is not None and len(embeddings) != len(chunks):
            raise ValueError("Embedding count must match chunk count")
        with self._connect() as connection:
            connection.execute("DELETE FROM chunks")
            connection.execute("DELETE FROM files")
            self._insert_chunks(connection, chunks, embeddings)

    def all(self) -> list[dict[str, Any]]:
        """Return all indexed chunks in stable source and line order."""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM chunks ORDER BY path,start_line,id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["embedding"] = json.loads(item["embedding"]) if item["embedding"] else None
            result.append(item)
        return result

    def count(self, source_type: str | None = None) -> int:
        with self._connect() as connection:
            if source_type is None:
                query = "SELECT COUNT(*) FROM chunks"
                parameters = ()
            else:
                query = "SELECT COUNT(*) FROM chunks WHERE source_type=?"
                parameters = (source_type,)
            return int(connection.execute(query, parameters).fetchone()[0])

    def sync_qdrant(self) -> None:
        """Rebuild the local Qdrant collection from stored dense vectors."""
        rows = [row for row in self.all() if row["embedding"]]
        client = self._qdrant_client()
        try:
            if client.collection_exists(self.COLLECTION):
                client.delete_collection(self.COLLECTION)
            if not rows:
                return
            from qdrant_client.models import Distance, PointStruct, VectorParams

            client.create_collection(
                collection_name=self.COLLECTION,
                vectors_config=VectorParams(size=len(rows[0]["embedding"]), distance=Distance.COSINE),
            )
            client.upsert(
                collection_name=self.COLLECTION,
                points=[
                    PointStruct(
                        id=row["id"], vector=row["embedding"],
                        payload={key: row[key] for key in (
                            "path", "start_line", "end_line", "symbol", "content",
                            "source_type", "commit_sha", "author", "last_modified",
                            "pr_number", "source_url",
                        )},
                    ) for row in rows
                ],
            )
        finally:
            client.close()

    def semantic_search(self, vector: list[float], limit: int) -> dict[int, float]:
        """Return Qdrant point IDs and cosine scores for a query vector."""
        client = self._qdrant_client()
        try:
            if not client.collection_exists(self.COLLECTION):
                return {}
            points = client.query_points(
                collection_name=self.COLLECTION, query=vector, limit=limit
            ).points
            return {int(point.id): float(point.score) for point in points}
        finally:
            client.close()

    def _qdrant_client(self):
        try:
            from qdrant_client import QdrantClient
        except ImportError as error:
            raise RuntimeError("Install qdrant-client to use vector search.") from error
        return QdrantClient(path=str(self.database_path.parent / "qdrant"))
