"""Small vector helpers for embeddings (no numpy needed at these sizes)."""

import math
from collections.abc import Sequence


def l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        return [0.0 for _ in vector]
    return [x / norm for x in vector]


def fit_dimensions(vector: Sequence[float], dim: int) -> list[float]:
    """Keep the first `dim` values and re-normalise. Valid for Matryoshka embedding models
    (Qwen3-Embedding), whose leading dimensions form a smaller embedding on their own."""
    if len(vector) < dim:
        raise ValueError(f"embedding has {len(vector)} dimensions; need at least {dim}")
    return l2_normalize(vector[:dim])


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError("vectors differ in length")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
