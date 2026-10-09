"""Operator pre-flight: prove the Messenger runtime is live on a deployed revision.

Sends one syntactically valid webhook event with a real X-Hub-Signature-256 to a
running revision, then reports the HTTP result. A 200 with ``accepted`` true means
the webhook route loaded, the signature verified and the ingress gate ran; the
operator then confirms the persisted row with the SQL in the checklist (PHAN C).

Never prints a secret. Reads MESSENGER_APP_SECRET and MESSENGER_VERIFY_TOKEN from
the environment, falling back to the repo ``.env``.

Usage:
  python scripts/probe_messenger_webhook.py --url https://<revision-url> [--psid 999000999]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# A Page id that is deliberately not a real Page: the probe must never be able to
# reach Meta, and the ingress gate rejects unknown PSIDs before any dispatch.
PROBE_PSID = "999000999"


def _load_env() -> None:
    """Populate the process environment from the repo ``.env`` without overriding it."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _handshake(base: str, verify_token: str, challenge: str) -> tuple[int, str]:
    url = (
        f"{base}/api/integrations/messenger-bot/webhook"
        f"?hub.mode=subscribe&hub.verify_token={verify_token}&hub.challenge={challenge}"
    )
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.status, response.read().decode("utf-8", "replace")


def _signed_post(base: str, app_secret: str, page_id: str, psid: str) -> tuple[int, str]:
    body = json.dumps(
        {
            "object": "page",
            "entry": [
                {
                    "id": page_id,
                    "time": 1,
                    "messaging": [
                        {
                            "sender": {"id": psid},
                            "recipient": {"id": page_id},
                            "timestamp": 1,
                            "message": {"mid": f"probe-{psid}", "text": "probe"},
                        }
                    ],
                }
            ],
        }
    ).encode()
    request = urllib.request.Request(
        f"{base}/api/integrations/messenger-bot/webhook",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(app_secret, body),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="Messenger webhook pre-flight probe")
    parser.add_argument("--url", required=True, help="Base URL of the revision to probe")
    parser.add_argument("--psid", default=PROBE_PSID, help="Synthetic PSID to send")
    parser.add_argument("--challenge", default="CHALLENGE_PROBE", help="Handshake challenge echo")
    args = parser.parse_args()

    _load_env()
    app_secret = os.environ.get("MESSENGER_APP_SECRET")
    verify_token = os.environ.get("MESSENGER_VERIFY_TOKEN")
    page_id = os.environ.get("MESSENGER_PAGE_ID")
    missing = [
        name
        for name, value in (
            ("MESSENGER_APP_SECRET", app_secret),
            ("MESSENGER_VERIFY_TOKEN", verify_token),
            ("MESSENGER_PAGE_ID", page_id),
        )
        if not value
    ]
    if missing:
        print(f"REFUSING: missing {', '.join(missing)}", file=sys.stderr)
        return 2

    base = args.url.rstrip("/")
    status, echoed = _handshake(base, verify_token, args.challenge)
    handshake_ok = status == 200 and echoed == args.challenge
    print(f"handshake: HTTP {status} echo={echoed!r} -> {'OK' if handshake_ok else 'FAIL'}")

    status, payload = _signed_post(base, app_secret, page_id, args.psid)
    print(f"signed POST: HTTP {status} body={payload[:200]}")
    if status != 200:
        print("FAIL: expected HTTP 200", file=sys.stderr)
        return 1
    print("Now confirm the persisted row (see MESSENGER_PREDEMO_CHECKLIST.md PHAN C):")
    print("  SELECT id, psid, event_kind, status FROM messenger_bot_events ORDER BY id DESC LIMIT 3;")
    return 0 if handshake_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
