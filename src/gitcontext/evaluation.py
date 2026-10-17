"""Retrieval and answer quality metrics for JSON evaluation fixtures."""

import re
from collections.abc import Callable
from statistics import mean
from time import perf_counter
from typing import Any


def recall_at_k(retrieved_paths: list[str], relevant_paths: set[str], k: int = 5) -> float:
    """Return the fraction of relevant files present in the first *k* results."""
    if not relevant_paths:
        return 0.0
    return len(set(retrieved_paths[:k]) & relevant_paths) / len(relevant_paths)


def reciprocal_rank(retrieved_paths: list[str], relevant_paths: set[str]) -> float:
    """Return reciprocal rank of the first relevant result."""
    for rank, path in enumerate(retrieved_paths, start=1):
        if path in relevant_paths:
            return 1.0 / rank
    return 0.0


def precision_at_k(retrieved_paths: list[str], relevant_paths: set[str], k: int = 5) -> float:
    """Return the relevant fraction of the first *k* results."""
    if k <= 0:
        return 0.0
    return len(set(retrieved_paths[:k]) & relevant_paths) / k


def token_f1(prediction: str, reference: str) -> float:
    """Token-overlap F1 for quick answer checks against a reference response."""
    predicted = re.findall(r"[a-z0-9_]+", prediction.lower())
    expected = re.findall(r"[a-z0-9_]+", reference.lower())
    if not predicted or not expected:
        return float(predicted == expected)
    overlap = sum(min(predicted.count(word), expected.count(word)) for word in set(expected))
    precision = overlap / len(predicted)
    recall = overlap / len(expected)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def extract_citations(answer: str) -> list[tuple[str, int, int]]:
    """Extract path and line ranges from standard GitContext citations."""
    return [
        (match.group(1), int(match.group(2)), int(match.group(3)))
        for match in re.finditer(r"([\w./-]+):(\d+)-(\d+)", answer)
    ]


def citation_accuracy(
    citations: list[tuple[str, int, int]], evidence: list[dict[str, Any]]
) -> float:
    """Fraction of cited path and line ranges supported by retrieved chunks."""
    if not citations:
        return 0.0
    supported = sum(
        any(
            citation_path == item["path"]
            and citation_start <= item["end_line"]
            and citation_end >= item["start_line"]
            for item in evidence
        )
        for citation_path, citation_start, citation_end in citations
    )
    return supported / len(citations)


def evaluate_retrieval(
    cases: list[dict[str, Any]],
    retrieve_fn: Callable[[str], list[dict[str, Any]]],
    k: int = 5,
) -> dict[str, float | int]:
    """Aggregate Recall@K, Precision@K, MRR, and average retrieval latency."""
    recalls = []
    precisions = []
    reciprocal_ranks = []
    latencies = []
    for case in cases:
        started = perf_counter()
        results = retrieve_fn(case["question"])
        latencies.append((perf_counter() - started) * 1000)
        paths = [result["path"] for result in results]
        relevant = set(case.get("relevant_paths", []))
        recalls.append(recall_at_k(paths, relevant, k))
        precisions.append(precision_at_k(paths, relevant, k))
        reciprocal_ranks.append(reciprocal_rank(paths, relevant))
    return {
        "queries": len(cases),
        "recall_at_k": mean(recalls) if recalls else 0.0,
        "precision_at_k": mean(precisions) if precisions else 0.0,
        "mrr": mean(reciprocal_ranks) if reciprocal_ranks else 0.0,
        "avg_latency_ms": mean(latencies) if latencies else 0.0,
    }
