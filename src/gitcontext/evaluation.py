"""Small retrieval metrics for local evaluation fixtures."""


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
