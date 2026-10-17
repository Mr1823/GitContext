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


@lru_cache(maxsize=2)
def _cross_encoder(name: str):
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as error:
        raise RuntimeError(
            "Install the semantic extra to use cross-encoder reranking."
        ) from error
    return CrossEncoder(name)


def rerank_texts(
    query: str, texts: list[str], model_name: str
) -> list[float]:
    """Score query and passage pairs with a local cross-encoder model."""
    if not texts:
        return []
    scores = _cross_encoder(model_name).predict([[query, text] for text in texts])
    return [float(score) for score in scores]
