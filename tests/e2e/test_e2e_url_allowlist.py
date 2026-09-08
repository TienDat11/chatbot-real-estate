"""Unit tests for the E2E base-url SSRF guard (validate_e2e_base_url).

The guard lives next to its only consumer (test_wave1_e2e.py); tests/e2e has
no __init__.py, so pytest prepends its own directory to sys.path and the
sibling module is importable by bare basename. Pure unit layer: no app, no
browser, no network.
"""

from __future__ import annotations

import pytest
from test_wave1_e2e import validate_e2e_base_url


@pytest.mark.parametrize(
    "raw",
    [
        "http://localhost:3000",
        "http://localhost",
        "https://localhost:443",
        "http://127.0.0.1:8000",
        "http://[::1]:3000",
        "http://LOCALHOST:3000",  # hostname parsing is case-insensitive
    ],
)
def test_accepts_localhost_allowlist(raw):
    assert validate_e2e_base_url(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "",  # empty input
        "not-a-url",  # neither scheme nor host
        "//localhost:3000",  # scheme-less
        "ftp://localhost",  # non-http(s) scheme
        "file:///etc/passwd",
        "http://example.com",  # external host
        "https://localhost.evil.com",  # allowlist suffix spoof
        "http://127.0.0.1.nip.io",  # DNS-alias style bypass
        "http://localhost@evil.com",  # userinfo trick: real host is evil.com
        "http://[::1",  # malformed IPv6 literal
    ],
)
def test_rejects_everything_outside_allowlist(raw):
    assert validate_e2e_base_url(raw) is None
