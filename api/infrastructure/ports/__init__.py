"""Domain ports for external services (Ports & Adapters).

The pipeline depends on these Protocols, never on concrete SDKs, so switching a
provider (aibox / DashScope / local) only adds a new adapter.
"""

from .geo import GeoPlace, GeoPort, GeoResult
from .llm import LLMChatPort
from .rag import RagChunk, RagPort, RagResult
from .rerank import RerankPort
from .sql import SqlPort, SqlResult

__all__ = [
    "GeoPlace",
    "GeoPort",
    "GeoResult",
    "LLMChatPort",
    "RagChunk",
    "RagPort",
    "RagResult",
    "RerankPort",
    "SqlPort",
    "SqlResult",
]
