"""Zalo composition using the same conversation pipeline as the web adapter.

No startup registration or live processing is enabled.
ZB-03 must supply shared atomic technical admission before uncached live turns.
"""

from api.application.services.chat_history_service import ChatHistoryService
from api.application.services.global_assistant_service import GlobalAssistantService
from api.application.services.zalo_bot_processor import ZaloBotProcessor
from api.infrastructure.adapters.postgres_chat_history import repository
from api.infrastructure.adapters.postgres_zalo_events import PostgresZaloEventStore
from api.infrastructure.adapters.zalo_bot_sender import HttpxZaloBotSender
from api.infrastructure.config.config import Settings


def build_zalo_processor(*, settings: Settings) -> ZaloBotProcessor:
    """Compose the dormant processor using the ruled shared pipeline."""
    from api.application.pipelines.conv_workflow import RagQueryPipelineConv  # noqa: PLC0415

    core = GlobalAssistantService(
        history=ChatHistoryService(repository),
        pipeline_factory=RagQueryPipelineConv,
    )
    return ZaloBotProcessor(
        store=PostgresZaloEventStore(),
        core=core,
        sender=HttpxZaloBotSender(token=settings.zalo_bot_token),
        identity_secret=settings.zalo_bot_identity_secret.get_secret_value(),
    )
