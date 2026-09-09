"""Verify canonical CDN origins and project namespaces through media_http.

Every outbound probe passes the §3.7 fail-closed SSRF policy
(``validate_media_fetch_url`` steps 1-4 inside :func:`perform_media_head`)
with the project allowlist from ``media_config.allowed_media_origins``.
Exits 0 only when all four project probes return HTTP 200 with a media
content-type. This check is public-only: it never reads or prints R2
credentials, and configured hosts are never written to stdout unredacted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.application.services.media_config import allowed_media_origins  # noqa: E402
from api.application.services.media_http import perform_media_head  # noqa: E402

# Some CDN edges reject non-browser tool UAs before content policy applies.
PROBE_UA = "ragre-cdn-verify/1.0"


@dataclass(frozen=True)
class Probe:
    project: str
    kind: str
    path: str


PROBES = (
    Probe("camellia", "image", "images/camellia/matbang/matbang-02.png"),
    Probe("camellia", "price-board", "images/camellia/banggia/gia-1.png"),
    Probe("soleil", "image", "images/soleil/matbang/"
          "2025.10.13-toa-d-mat-bang-ch01-studio.png"),
    Probe("camellia", "video", "media/video/brand-film-web.mp4"),
)


def _redact_origin(url: str) -> str:
    return "h:" + hashlib.sha256(url.encode()).hexdigest()[:10]


async def _probe_all(transport: httpx.AsyncBaseTransport | None) -> list[dict]:
    results: list[dict] = []
    for probe in PROBES:
        allowed = sorted(allowed_media_origins(probe.project))
        item: dict = {"project": probe.project, "kind": probe.kind,
                      "path": probe.path}
        if not allowed:
            item["head"] = {"status": "NO_ALLOWLIST"}
            results.append(item)
            continue
        url = f"{allowed[0]}/{probe.path}"
        item["origin"] = _redact_origin(allowed[0])
        try:
            verdict = await perform_media_head(
                url,
                allowed,
                user_agent=PROBE_UA,
                transport=transport,
                timeout_seconds=15.0,
            )
        except Exception as exc:  # noqa: BLE001 - operator-facing report
            item["head"] = {"status": "UNAVAILABLE", "error": type(exc).__name__}
            results.append(item)
            continue
        if verdict is None:
            item["head"] = {"status": "REJECTED_OR_UNHEALTHY"}
        else:
            item["head"] = {
                "status": verdict.status_code,
                "content_type": verdict.content_type,
            }
        results.append(item)
    return results


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-unavailable", action="store_true")
    args = parser.parse_args()

    failures: list[str] = []
    for project in ("camellia", "soleil"):
        if not allowed_media_origins(project):
            failures.append(f"{project}: no public origin configured")

    results = asyncio.run(_probe_all(None))

    for item in results:
        head = item["head"]
        status = head.get("status")
        healthy = status == 200 and str(head.get("content_type", "")).startswith(
            ("image/", "video/")
        )
        if not healthy:
            failures.append(f"{item['project']}/{item['path']}: {json.dumps(head)}")

    status_word = (
        "BLOCKED" if failures and not args.allow_unavailable
        else ("DEGRADED" if failures else "PASS")
    )
    print(json.dumps({"status": status_word, "probes": results,
                      "failures": failures}, sort_keys=True))
    return 1 if failures and not args.allow_unavailable else 0


if __name__ == "__main__":
    raise SystemExit(run())
