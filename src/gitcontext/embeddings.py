"""Optional local semantic embeddings powered by sentence-transformers."""

from functools import lru_cache


@lru_cache(maxsize=2)
def _model(name: str):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as error:
        raise RuntimeError(
            "Install the project dependencies to use semantic embeddings."
        ) from error
    return SentenceTransformer(name)


def encode_texts(texts: list[str], model_name: str) -> list[list[float]]:
    """Encode text locally; model files are fetched on first use if needed."""
    if not texts:
        return []
    vectors = _model(model_name).encode(texts, normalize_embeddings=True)
    return vectors.tolist()
