"""Hybrid lexical and optional semantic retrieval."""

import re
from time import perf_counter
from typing import Any

from rank_bm25 import BM25Okapi

from gitcontext.embeddings import encode_texts, rerank_texts
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
    reranker_model: str | None = None,
    candidate_limit: int = 40,
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

    semantic_scores: dict[int, float] = {}
    if embedding_model and any(chunk["embedding"] for chunk in chunks):
        query_vector = encode_texts([query], embedding_model)[0]
        semantic_scores = store.semantic_search(query_vector, max(limit * 8, candidate_limit))

    combined = []
    for index, chunk in enumerate(chunks):
        symbol_matches = len(set(tokenize(query)) & set(tokenize(chunk["symbol"])))
        semantic_score = semantic_scores.get(chunk["id"], 0.0)
        score = 0.55 * lexical_scores[index] + 0.45 * semantic_score if semantic_scores else lexical_scores[index]
        score += 0.35 * symbol_matches
        if score > 0:
            combined.append({**chunk, "score": score})
    ranked = sorted(combined, key=lambda item: (-item["score"], item["path"], item["start_line"]))
    candidates = ranked[:max(limit, candidate_limit)]
    if reranker_model and candidates:
        scores = rerank_texts(
            query,
            [f"{item['symbol']}\n{item['path']}\n{item['content']}" for item in candidates],
            reranker_model,
        )
        candidates = [
            {**item, "retrieval_score": item["score"], "score": scores[index]}
            for index, item in enumerate(candidates)
        ]
        candidates.sort(key=lambda item: (-item["score"], item["path"], item["start_line"]))
    return candidates[:limit]


def retrieve_with_metrics(
    query: str,
    store: VectorStore,
    limit: int = 6,
    embedding_model: str | None = None,
    reranker_model: str | None = None,
) -> tuple[list[dict[str, Any]], float]:
    """Run retrieval and return results plus elapsed milliseconds."""
    started = perf_counter()
    results = retrieve(query, store, limit, embedding_model, reranker_model)
    return results, (perf_counter() - started) * 1000
