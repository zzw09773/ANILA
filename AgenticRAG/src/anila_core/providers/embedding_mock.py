"""Mock EmbeddingProvider for testing without external dependencies.

Returns deterministic pseudo-random vectors of the correct dimension so
tests that check vector shape / indexing logic work without network calls.
"""

from __future__ import annotations

import hashlib
import struct


class MockEmbeddingProvider:
    """Deterministic mock embedding provider for unit tests.

    For a given text, always returns the same vector (hash-seeded),
    making tests reproducible without hitting any external API.

    Args:
        dimension: Embedding dimension to emulate (default: 4096).
    """

    def __init__(self, dimension: int = 4096) -> None:
        self._dimension = dimension

    async def embed(
        self,
        texts: list[str],
        input_type: str = "passage",
    ) -> list[list[float]]:
        """Return deterministic fake embeddings for each text."""
        return [self._text_to_vector(t) for t in texts]

    @property
    def dimension(self) -> int:
        return self._dimension

    def _text_to_vector(self, text: str) -> list[float]:
        """Produce a deterministic unit-ish vector from text via SHA-256."""
        digest = hashlib.sha256(text.encode()).digest()
        # Repeat digest to fill dimension floats
        repeats = (self._dimension * 4) // len(digest) + 1
        raw = (digest * repeats)[: self._dimension * 4]
        values = [
            struct.unpack_from("f", raw, i * 4)[0]
            for i in range(self._dimension)
        ]
        # Normalise to avoid extreme values
        magnitude = sum(v * v for v in values) ** 0.5 or 1.0
        return [v / magnitude for v in values]
