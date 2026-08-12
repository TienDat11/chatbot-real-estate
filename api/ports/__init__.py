"""Domain ports for external services (Ports & Adapters).

The pipeline depends on these Protocols, never on concrete SDKs, so switching a
provider (aibox / DashScope / local) only adds a new adapter.
"""

from api.ports.geo import GeoPlace, GeoPort, GeoResult
from api.ports.llm import LLMChatPort
from api.ports.rag import RagChunk, RagPort, RagResult
from api.ports.rerank import RerankPort
from api.ports.sql import SqlPort, SqlResult

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
