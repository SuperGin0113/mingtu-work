from __future__ import annotations


class UpstreamServiceError(RuntimeError):
    """Raised when an external embedding/rerank service request fails."""
