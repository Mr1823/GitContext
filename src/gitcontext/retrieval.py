"""Hybrid lexical and optional semantic retrieval."""

import re
from typing import Any

from rank_bm25 import BM25Okapi

from gitcontext.embeddings import encode_texts
from gitcontext.vector_store import VectorStore

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
STOP_WORDS = {
    "a", "an", "and", "are", "do", "does", "for", "how", "i", "in",
    "is", "it", "of", "on", "or", "the", "to", "what", "where",
    "which", "who", "why", "with", "defined",
}


def tokenize(text: str) -> list[str]:
    return [
        part.lower()
        for token in TOKEN_RE.findall(text)
        for part in token.split("_")
        if part and part.lower() not in STOP_WORDS
    ]


def retrieve(
    query: str,
    store: VectorStore,
    limit: int = 6,
    embedding_model: str | None = None,
) -> list[dict[str, Any]]:
    """Rank indexed code by BM25 and, when available, semantic similarity."""
    chunks = store.all()
    if not chunks or limit <= 0:
        return []
    tokenized = [
        tokenize(chunk["content"] + " " + chunk["path"])
        + tokenize(chunk["symbol"]) * 3
        for chunk in chunks
    ]
    if any(tokenized):
        lexical_scores = BM25Okapi(tokenized).get_scores(tokenize(query)).tolist()
    else:
        lexical_scores = [0.0] * len(chunks)
    lexical_scores = [max(float(score), 0.0) for score in lexical_scores]
    if not any(lexical_scores):
        query_tokens = set(tokenize(query))
        lexical_scores = [
            float(len(query_tokens & set(tokens))) for tokens in tokenized
        ]
    lexical_max = max(lexical_scores, default=0.0)
    lexical_scores = [score / lexical_max if lexical_max else 0.0 for score in lexical_scores]

    semantic_scores = [0.0] * len(chunks)
    if embedding_model and any(chunk["embedding"] for chunk in chunks):
        query_vector = encode_texts([query], embedding_model)[0]
        for index, chunk in enumerate(chunks):
            vector = chunk["embedding"]
            if vector and len(vector) == len(query_vector):
                semantic_scores[index] = max(
                    0.0, sum(a * b for a, b in zip(vector, query_vector))
                )

    combined = []
    for index, chunk in enumerate(chunks):
        symbol_matches = len(set(tokenize(query)) & set(tokenize(chunk["symbol"])))
        score = (
            0.55 * lexical_scores[index] + 0.45 * semantic_scores[index]
            if any(semantic_scores) else lexical_scores[index]
        )
        score += 0.35 * symbol_matches
        if score > 0:
            combined.append({**chunk, "score": score})
    return sorted(combined, key=lambda item: (-item["score"], item["path"], item["start_line"]))[:limit]
