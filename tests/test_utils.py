"""Unit tests for `api.utils` pure helpers.

No DB/network: only stdlib plus `api.utils` are used.
"""

from datetime import datetime, timezone

from api.utils import safe_float, sha256_hex, slugify, truncate_str, utc_now_iso


def test_sha256_hex_known_vectors():
    # NIST / RFC 4634 test vectors.
    assert sha256_hex("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_hex("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_sha256_hex_none_treated_as_empty():
    assert sha256_hex(None) == sha256_hex("")
    assert sha256_hex(None) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sha256_hex_is_deterministic():
    assert sha256_hex("legal text đất đai") == sha256_hex("legal text đất đai")


def test_utc_now_iso_is_utc():
    parsed = datetime.fromisoformat(utc_now_iso())
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


def test_safe_float_defaults_on_invalid():
    assert safe_float(None) == 0.0
    assert safe_float("abc") == 0.0
    assert safe_float("") == 0.0
    assert safe_float([]) == 0.0  # unsupported type -> default
    assert safe_float(None, default=1.5) == 1.5


def test_safe_float_parses_valid_input():
    assert safe_float("3.14") == 3.14
    assert safe_float("42") == 42.0
    assert safe_float(7) == 7.0
    assert safe_float(" 2.5 ") == 2.5


def test_truncate_str_leaves_short_text_untouched():
    assert truncate_str("", 5) == ""
    assert truncate_str("short", 10) == "short"
    assert truncate_str("abcdefghij", 10) == "abcdefghij"  # exact fit


def test_truncate_str_appends_ellipsis_when_cut():
    assert truncate_str("abcdefghij", 5) == "abcd…"
    assert truncate_str("abcdef", 5) == "abcd…"
    assert len(truncate_str("a" * 50, 10)) == 10


def test_slugify():
    assert slugify("Căn hộ 2PN") == "can-ho-2pn"
    assert slugify("  Hello  World  ") == "hello-world"
    assert slugify("ABC") == "abc"
    assert slugify("a--b--c") == "a-b-c"
    assert slugify("") == ""
    assert slugify(None) == ""


def test_settings_prod_fails_fast_on_default_secret():
    from pydantic import ValidationError

    from api.config import Settings

    try:
        Settings(app_env="production")
    except ValidationError:
        pass
    else:
        raise AssertionError("production with default secret must fail fast")
