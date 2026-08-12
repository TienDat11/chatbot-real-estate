"""Legacy settings entry point — thin re-export of api.config.

Kept so existing `from ingest.config import settings` and
`from ingest.config import Settings as IngestSettings` imports keep working.
"""

from api.config import Settings, get_settings, settings

IngestSettings = Settings

__all__ = ["IngestSettings", "Settings", "get_settings", "settings"]
