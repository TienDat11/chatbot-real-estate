"""Canonical CDN origin inventory, decision, and guarded backfill (FR-13/FR-14).

Default mode is DRY-RUN and performs zero mutations:

  1. Inventory: dump every ``images.url_cdn`` row plus ``project_config.media``
     registry entries grouped by parsed origin/prefix, and HEAD-probe each
     distinct stored URL through :func:`api.application.services.media_http.
     perform_media_head` (fail-closed SSRF policy, allowlist =
     ``media_config.allowed_media_origins(project_key)``). The full artifact is
     written OUTSIDE git before any rewrite consideration.
  2. Decide: the canonical origin per project comes from
     ``IMAGE_CDN_PROJECT_MAP`` (map always beats ``R2_PUBLIC_URL``); the first
     mapped origin whose probe returned HTTP 200 wins. When no healthy mapped
     origin holds the objects the script reports a REQUIRED_OPERATOR_ACTION
     instead of improvising an origin; copies into the canonical
     ``images/<project>/...`` namespace happen only in ``--execute`` and are
     additive (never a delete).
  3. Backfill (--execute only): rebuild each URL as
     ``<chosen_origin>/<object_key>`` from parsed components ONLY - never a
     string prefix on an absolute URL. Any candidate containing a second
     ``scheme://`` is rejected for manual review. Rows already resolving 200 on
     a mapped healthy origin are never rewritten. Every rewrite candidate must
     have been observed 200 + media content-type BEFORE the UPDATE runs;
     re-running is a no-op (idempotent). A rollback export (JSON + SQL) is
     saved next to the inventory artifact before the transaction opens.

Credentials/env values are never printed to stdout beyond redacted host hashes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.application.services.media_config import allowed_media_origins  # noqa: E402
from api.application.services.media_http import (  # noqa: E402
    MediaHeadProbe,
    perform_media_head,
)

# Some CDN edges (Cloudflare r2.dev) reject unconventional tool UAs outright;
# operators may pass a conventional UA so the policy checks stay meaningful.
DEFAULT_PROBE_UA = "ragre-cdn-migration/1.0"

HEALTHY_STATUS = 200

PROJECTS = ("camellia", "soleil")


def redact_host(value: str) -> str:
    """One-way short hash so logs/artifact summaries never leak configured hosts."""
    return "h:" + hashlib.sha256(value.encode()).hexdigest()[:10]


@dataclass(frozen=True)
class MediaComponents:
    """Parsed pieces of one stored media URL."""

    url: str
    origin: str
    object_key: str


def looks_like_absolute_url(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(
        ("http://", "https://")
    )


def parse_media_components(value: object) -> MediaComponents | None:
    """Split one absolute http(s) media URL into origin + object key.

    Returns None for anything that is not a clean absolute URL: relative keys,
    credential-bearing authorities, missing hosts, or exotic schemes. Built for
    operator-controlled stored values, so the failure contract is simply None.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    object_key = unquote(parsed.path).lstrip("/")
    if not object_key:
        return None
    return MediaComponents(url=text, origin=origin, object_key=object_key)


def build_canonical_url(chosen_origin: str, object_key: str) -> str | None:
    """Join parsed components into ``origin/key``; None when any guard trips.

    Guards (fail-closed): the origin must be a bare http(s) authority, the key
    must be relative and traversal-free, and the joined result may contain
    exactly ONE ``scheme://`` occurrence - a second one means a URL was fed in
    place of a key (double-prefix class bug) and construction is refused.
    """
    if not isinstance(chosen_origin, str) or not isinstance(object_key, str):
        return None
    origin = chosen_origin.strip().rstrip("/")
    key = object_key.strip().lstrip("/")
    parsed = urlsplit(origin) if origin else None
    if (
        not origin
        or parsed is None
        or parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return None
    if not key or looks_like_absolute_url(key):
        return None
    parts = key.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    candidate = f"{origin}/{key}"
    if candidate.count("://") != 1 or not looks_like_absolute_url(candidate):
        return None
    if candidate.startswith(("http://http://", "https://https://", "http://https://",
                             "https://http://")):
        return None
    return candidate


@dataclass
class RowPlan:
    """Decision for one images row."""

    image_id: str
    project_key: str
    status_value: str
    old_url: str
    old_origin: str
    object_key: str
    decision: str  # keep_healthy | rewrite | manual_review
    reason: str
    new_url: str | None = None
    verified_status: int | None = None


def plan_row(
    components: MediaComponents,
    project_key: str,
    status_value: str,
    *,
    image_id: str,
    chosen_origin: str | None,
    url_health: dict[str, int | None],
) -> RowPlan:
    """Classify one row against probe evidence and the chosen origin."""
    base = dict(
        image_id=image_id,
        project_key=project_key,
        status_value=status_value,
        old_url=components.url,
        old_origin=components.origin,
        object_key=components.object_key,
    )
    if chosen_origin is None:
        return RowPlan(decision="manual_review",
                       reason="no healthy mapped origin for project", **base)
    healthy_old = url_health.get(components.url) == HEALTHY_STATUS
    if healthy_old and components.origin == chosen_origin:
        return RowPlan(decision="keep_healthy",
                       reason="already resolving 200 on mapped healthy origin", **base)
    candidate = build_canonical_url(chosen_origin, components.object_key)
    if candidate is None:
        return RowPlan(decision="manual_review",
                       reason="canonical url construction refused by guard", **base)
    if candidate == components.url:
        # Same final URL but the stored copy was never seen healthy: leave it
        # alone (idempotence) and let verification decide separately.
        return RowPlan(decision="manual_review",
                       reason="stored url equals candidate but not verified 200", **base)
    verified = url_health.get(candidate) == HEALTHY_STATUS
    if not verified:
        return RowPlan(decision="manual_review",
                       reason="candidate not observed 200 yet", **base)
    return RowPlan(decision="rewrite",
                   reason=f"origin {redact_host(components.origin)} is not healthy",
                   new_url=candidate, verified_status=HEALTHY_STATUS, **base)


def plan_rows(
    rows: list[dict[str, Any]],
    chosen_origins: dict[str, str | None],
    url_health: dict[str, int | None],
) -> list[RowPlan]:
    """Build decisions for every inventory row (pure, no I/O)."""
    plans: list[RowPlan] = []
    for row in rows:
        components = parse_media_components(row["url_cdn"])
        project_key = str(row.get("project_key") or "")
        if components is None:
            plans.append(RowPlan(
                image_id=str(row.get("image_id")), project_key=project_key,
                status_value=str(row.get("status") or ""), old_url=str(row["url_cdn"]),
                old_origin="", object_key="", decision="manual_review",
                reason="url is not parseable into origin + object key"))
            continue
        plans.append(plan_row(
            components, project_key, str(row.get("status") or ""),
            image_id=str(row.get("image_id")),
            chosen_origin=chosen_origins.get(project_key), url_health=url_health))
    return plans


async def probe_media_url(
    url: str,
    project_key: str,
    *,
    user_agent: str = DEFAULT_PROBE_UA,
    transport: httpx.AsyncBaseTransport | None = None,
    resolve: Any = None,
    timeout_seconds: float = 10.0,
) -> MediaHeadProbe | None:
    """HEAD-probe one URL under the media_http SSRF policy for a project.

    Fail-closed: no allowlist for the project, DNS failure, redirect hop, bad
    content-type, or any policy violation yields None. Wired callers:
    this script's inventory/verification phases and scripts/verify_cdn_media.py.
    """
    allowed = sorted(allowed_media_origins(project_key))
    if not allowed:
        return None
    kwargs: dict[str, Any] = {}
    if resolve is not None:
        kwargs["resolve"] = resolve
    return await perform_media_head(
        url,
        allowed,
        transport=transport,
        timeout_seconds=timeout_seconds,
        user_agent=user_agent,
        **kwargs,
    )


async def probe_urls(
    urls: list[str],
    project_by_url: dict[str, str],
    *,
    user_agent: str = DEFAULT_PROBE_UA,
    transport: httpx.AsyncBaseTransport | None = None,
    resolve: Any = None,
    max_probes: int = 400,
) -> dict[str, int | None]:
    """Probe distinct URLs, returning status codes (None = policy/network fail)."""
    health: dict[str, int | None] = {}
    if len(urls) > max_probes:
        urls = urls[:max_probes]
    semaphore = asyncio.Semaphore(8)

    async def one(url: str) -> None:
        async with semaphore:
            probe = await probe_media_url(
                url, project_by_url[url], user_agent=user_agent,
                transport=transport, resolve=resolve)
            health[url] = None if probe is None else probe.status_code

    await asyncio.gather(*(one(url) for url in urls))
    return health


def fetch_inventory() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read images rows and registry media entries (SELECT-only)."""
    import psycopg2  # noqa: PLC0415

    from api.infrastructure.config.config import settings  # noqa: PLC0415

    with psycopg2.connect(settings.pg_dsn_sync, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT image_id, project_key, kind, status, url_cdn FROM images "
                "ORDER BY project_key, image_id"
            )
            images = [
                {
                    "image_id": r[0], "project_key": r[1], "kind": r[2],
                    "status": r[3], "url_cdn": r[4],
                }
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT project_key, media FROM project_config "
                "WHERE media IS NOT NULL ORDER BY project_key"
            )
            registry = [
                {"project_key": r[0],
                 "entries": [e for e in (r[1] or []) if isinstance(e, dict)]}
                for r in cur.fetchall()
            ]
    return images, registry


def group_inventory(
    images: list[dict[str, Any]], registry: list[dict[str, Any]]
) -> dict[str, Any]:
    """Group rows by parsed origin and object-key prefix (report shape)."""
    by_origin: dict[str, dict[str, Any]] = {}
    for row in images:
        components = parse_media_components(row["url_cdn"])
        origin_key = components.origin if components else "UNPARSEABLE"
        bucket = by_origin.setdefault(origin_key, {"rows": 0, "projects": set(),
                                                   "sample_keys": []})
        bucket["rows"] += 1
        bucket["projects"].add(str(row.get("project_key")))
        if len(bucket["sample_keys"]) < 3 and components:
            bucket["sample_keys"].append(components.object_key)
    for bucket in by_origin.values():
        bucket["projects"] = sorted(bucket["projects"])
    registry_groups: dict[str, list[str]] = {}
    for entry in registry:
        prefixes: dict[str, int] = {}
        for item in entry["entries"]:
            key = str(item.get("object_key", "")).lstrip("/")
            prefix = key.rsplit("/", 1)[0] + "/" if "/" in key else key
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
        registry_groups[str(entry["project_key"])] = [
            f"{prefix}({count})" for prefix, count in sorted(prefixes.items())
        ]
    return {"by_origin": by_origin, "registry_prefixes": registry_groups}


def choose_origins(
    project_keys: tuple[str, ...], url_health: dict[str, int | None]
) -> dict[str, str | None]:
    """Pick the mapped origin per project that holds verified-200 objects."""
    healthy_origins = {
        components.origin
        for url, status in url_health.items()
        if status == HEALTHY_STATUS
        and (components := parse_media_components(url)) is not None
    }
    chosen: dict[str, str | None] = {}
    for project_key in project_keys:
        origins = sorted(allowed_media_origins(project_key))
        # Sorted order keeps the decision deterministic when several mapped
        # origins hold the same objects; first healthy one wins.
        chosen[project_key] = next(
            (origin for origin in origins if origin in healthy_origins), None
        )
    return chosen


async def run_plan(
    *, user_agent: str, transport: httpx.AsyncBaseTransport | None = None,
    resolve: Any = None, max_probes: int = 400, project_keys: tuple[str, ...] = PROJECTS,
    fetcher: Any = None,
) -> dict[str, Any]:
    """Execute the read-only inventory + decision pipeline; returns the artifact."""
    fetch = fetcher or fetch_inventory
    images, registry = fetch()
    groups = group_inventory(images, registry)

    project_by_url: dict[str, str] = {}
    for row in images:
        components = parse_media_components(row["url_cdn"])
        if components and row.get("project_key"):
            project_by_url.setdefault(components.url, str(row["project_key"]))
    # Evidence comes ONLY from real stored object URLs: an origin counts as
    # healthy when it actually serves a stored key with HTTP 200 + media type.
    probe_targets = list(project_by_url)
    url_health = await probe_urls(
        probe_targets, project_by_url, user_agent=user_agent, transport=transport,
        resolve=resolve, max_probes=max_probes)
    chosen = choose_origins(project_keys, url_health)
    # Phase 2: the rewrite guard demands 200 evidence for every candidate URL
    # on the chosen origin, but a dead-origin migration has NO stored row on
    # that origin yet, so the candidates were never in the phase-1 probe set.
    # Derive each row's candidate and probe it before planning.
    for row in images:
        components = parse_media_components(row["url_cdn"])
        project_key = str(row.get("project_key") or "")
        if components is None or not project_key:
            continue
        origin = chosen.get(project_key)
        if origin is None:
            continue
        candidate = build_canonical_url(origin, components.object_key)
        if candidate is not None and candidate not in url_health:
            project_by_url.setdefault(candidate, project_key)
            url_health[candidate] = None  # placeholder; probed below as a unit
    pending = [url for url, status in url_health.items() if status is None]
    if pending:
        url_health.update(await probe_urls(
            pending, project_by_url, user_agent=user_agent, transport=transport,
            resolve=resolve, max_probes=max_probes))
    plans = plan_rows(images, chosen, url_health)
    actions = []
    for project_key in project_keys:
        if chosen[project_key] is None:
            actions.append({
                "required_operator_action": (
                    f"{project_key}: IMAGE_CDN_PROJECT_MAP lists no origin that "
                    "resolved 200. Add a healthy canonical origin to the project "
                    "entry (objects may need copying into images/%s/...) then "
                    "re-run --dry." % project_key)})
    masked_summary = [
        {
            "decision": plan.decision,
            "image_id": plan.image_id,
            "project_key": plan.project_key,
            "old_origin": redact_host(plan.old_origin),
            "reason": plan.reason,
        }
        for plan in plans
        if plan.decision != "keep_healthy"
    ]
    return {
        "generated_at_epoch": time.time(),
        "mode": "dry-run-plan",
        "images_total": len(images),
        "origin_groups": groups,
        "chosen_origin_redacted": {
            k: (None if v is None else redact_host(v)) for k, v in chosen.items()
        },
        "url_health_sample": {
            redact_host(url): status
            for url, status in sorted(url_health.items())[:20]
        },
        "non_keep_plans": masked_summary,
        "counts": {
            name: sum(1 for p in plans if p.decision == name)
            for name in ("keep_healthy", "rewrite", "manual_review")
        },
        "actions": actions,
        "_plans": plans,
        "_chosen": chosen,
    }


def rollback_export(plans: list[RowPlan], out_dir: Path) -> list[Path]:
    """Save JSON + SQL undo exports OUTSIDE git; returns the file paths."""
    rewrites = [p for p in plans if p.decision == "rewrite"]
    payload = [
        {"image_id": p.image_id, "from": p.old_url, "to": p.new_url}
        for p in rewrites
    ]
    json_path = out_dir / "rollback_export.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    sql_lines = ["BEGIN;"]
    for item in payload:
        sql_lines.append(
            f"UPDATE images SET url_cdn = '{item['from']}' "
            f"WHERE image_id = '{item['image_id']}';"
        )
    sql_lines.append("COMMIT;")
    sql_path = out_dir / "rollback.sql"
    sql_path.write_text("\n".join(sql_lines) + "\n", encoding="utf-8")
    return [json_path, sql_path]


def apply_rewrites(plans: list[RowPlan], conn: Any) -> int:
    """Guarded transactional backfill; returns updated row count.

    Each UPDATE matches BOTH image_id and the old URL, so concurrent edits or a
    double application simply touch nothing (idempotent).
    """
    rewrites = [p for p in plans if p.decision == "rewrite"]
    if not rewrites:
        return 0
    updated = 0
    with conn.cursor() as cur:
        for plan in rewrites:
            cur.execute(
                "UPDATE images SET url_cdn = %s, updated_at = now() "
                "WHERE image_id = %s AND url_cdn = %s",
                (plan.new_url, plan.image_id, plan.old_url),
            )
            updated += cur.rowcount
    conn.commit()
    return updated


def regenerate_manifest() -> str:
    """Sync ``ingest/image_captions_manifest.json:r2_public_base`` from settings.

    The manifest must never be hand-edited: its public base is regenerated from
    ``settings.image_cdn_base(project_key)`` (map wins over R2_PUBLIC_URL) so
    the next images_ingest run derives url_cdn values from the canonical
    origin. Returns one of updated|unchanged|refused-no-canonical-origin.
    """
    from api.infrastructure.config.config import settings  # noqa: PLC0415

    manifest_path = _REPO_ROOT / "ingest" / "image_captions_manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = settings.image_cdn_base(str(data.get("project_key") or ""))
    if not base:
        return "refused-no-canonical-origin"
    if str(data.get("r2_public_base", "")).rstrip("/") == base.rstrip("/"):
        return "unchanged"
    data["r2_public_base"] = base
    manifest_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return "updated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry", action="store_true", help="default: inventory + plan only")
    parser.add_argument(
        "--execute", action="store_true",
        help="apply guarded rewrites AFTER reviewing the dry-run artifact")
    parser.add_argument("--artifact-dir", type=Path, default=None,
                        help="where the plan/rollback artifacts go (outside git)")
    parser.add_argument("--probe-user-agent", default=DEFAULT_PROBE_UA)
    parser.add_argument(
        "--regen-manifest", action="store_true",
        help="with --execute: regenerate ingest manifest r2_public_base "
             "from settings instead of hand-editing")
    args = parser.parse_args(argv)
    if args.execute and args.dry:
        print("PASS either --dry or --execute, not both")
        return 2
    execute = args.execute
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = args.artifact_dir or (
        Path(tempfile.gettempdir()) / "cdn_migration" / stamp
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        artifact = asyncio.run(run_plan(user_agent=args.probe_user_agent))
    except Exception as exc:  # noqa: BLE001 - operator-facing CLI failure
        print(json.dumps({"status": "ERROR", "phase": "inventory",
                          "error_type": type(exc).__name__}))
        return 3
    plans: list[RowPlan] = artifact.pop("_plans")
    artifact.pop("_chosen")
    artifact["mode"] = "execute-plan" if execute else "dry-run-plan"

    if execute:
        blocking = [
            a["required_operator_action"] for a in artifact["actions"]
        ]
        if blocking:
            for action in blocking:
                print(json.dumps({"BLOCKED": action}))
            return 4
        if artifact["counts"]["manual_review"]:
            for plan in plans:
                if plan.decision == "manual_review":
                    print(json.dumps({
                        "MANUAL_REVIEW": {"image_id": plan.image_id,
                                          "reason": plan.reason}}))
            return 5
        saved = rollback_export(plans, out_dir)
        artifact["rollback_exports"] = [str(p.name) for p in saved]
        import psycopg2  # noqa: PLC0415

        from api.infrastructure.config.config import settings  # noqa: PLC0415

        with psycopg2.connect(settings.pg_dsn_sync, connect_timeout=5) as conn:
            artifact["updated_rows"] = apply_rewrites(plans, conn)
        if args.regen_manifest:
            artifact["manifest_r2_public_base"] = regenerate_manifest()
    else:
        artifact["updated_rows"] = 0

    plan_path = out_dir / ("apply_plan.json" if execute else "inventory_plan.json")
    plan_path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": "PLANNED" if not execute else "APPLIED",
        "artifact": str(plan_path),
        "counts": artifact["counts"],
        "updated_rows": artifact["updated_rows"],
        "actions_required": len(artifact["actions"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
