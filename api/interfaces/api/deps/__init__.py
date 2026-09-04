"""FastAPI auth dependencies for staff-facing routes (story 8.3 / ISSUE-06).

Re-exports the role-gated Firebase dependencies and the principal type so
routers depend on exactly one import path:
``from api.interfaces.api.deps import require_admin``.
"""

from api.interfaces.api.deps.admin import (
    AuthenticatedPrincipal,
    optional_sales_or_admin,
    require_admin,
    require_authenticated,
    require_sales,
    require_sales_or_admin,
    require_training_sales,
    require_viewer,
)

__all__ = [
    "AuthenticatedPrincipal",
    "optional_sales_or_admin",
    "require_admin",
    "require_authenticated",
    "require_sales",
    "require_sales_or_admin",
    "require_training_sales",
    "require_viewer",
]
