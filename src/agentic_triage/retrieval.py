"""Minimal, dependency-free vector store for prior-case retrieval (RAG).

The default embedding is a deterministic hashing bag-of-words — good enough to
demonstrate retrieval, run offline, and keep tests reproducible with zero
external services. In production you would swap `embed()` for a real embedding
model and back `VectorStore` with pgvector; the interface is intentionally the
same (add / search), so only this file changes. See docs/architecture.md.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

_DIM = 256
_WORD = re.compile(r"[a-z0-9]+")


def embed(text: str, dim: int = _DIM) -> list[float]:
    """Hashing embedding: map tokens into a fixed-dim vector, L2-normalized."""
    vec = [0.0] * dim
    for tok in _WORD.findall(text.lower()):
        h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "big")
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


@dataclass
class Record:
    id: str
    text: str
    metadata: dict = field(default_factory=dict)
    vector: list[float] = field(default_factory=list)


class VectorStore:
    def __init__(self) -> None:
        self._records: list[Record] = []

    def add(self, id: str, text: str, metadata: dict | None = None) -> None:
        self._records.append(
            Record(id=id, text=text, metadata=metadata or {}, vector=embed(text))
        )

    def search(self, query: str, k: int = 3) -> list[tuple[float, Record]]:
        q = embed(query)
        scored = [(cosine(q, r.vector), r) for r in self._records]
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:k]

    def __len__(self) -> int:
        return len(self._records)
