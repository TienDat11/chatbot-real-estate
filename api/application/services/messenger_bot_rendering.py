"""Render common Markdown into plain text for the Messenger text payload.

Messenger does not render Markdown, so the RAG answer is normalized to plain
text before sending: tables become pipe-free rows with cells separated by an
em dash, emphasis and heading markers are stripped while the text is kept,
links become ``text (url)'', inline code becomes plain text, bullets become
``• '', ordered lists and plain paragraphs are left as-is, and runs of 3 or
more blank lines are collapsed to 2.

Pure, deterministic and I/O-free: no network, no file, no new dependency. All
matching runs operate on Unicode code points, so Vietnamese diacritics survive
byte-for-byte.
"""

from __future__ import annotations

import re

# Inline patterns: code spans first so markers inside them are not interpreted
# as emphasis. Each substitution keeps only the captured content.
_RE_CODE = re.compile(r"`([^`]+)`")
_RE_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_RE_ITALIC = re.compile(r"(\*|_)(.+?)\1")
_RE_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# A line whose stripped content (minus surrounding pipes) is only dashes,
# colons and spaces is a markdown separator row; it is dropped.
_TABLE_SEP_LINE = re.compile(r"^[\s\-:]+$")


def _is_table_sep(line: str) -> bool:
    """True for a markdown separator row such as ``|---|---|`` or ``|:---|``.

    Only the outer pipes are removed before judging, so the pipes BETWEEN the
    dash cells stay and a multi-column separator like ``| --- | --- |`` is
    still recognized. Judging the stripped core alone would leave those inner
    pipes in place and let the row render as ``--- — ---``.
    """
    core = line.strip().strip("|").strip()
    return bool(core) and all(
        _TABLE_SEP_LINE.match(cell) is not None for cell in core.split("|")
    )


def _cells(line: str) -> list[str]:
    """Split a table line on pipes, stripping outer markers and cell whitespace."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _render_inline(text: str) -> str:
    """Apply inline-only replacements: code spans, bold, italic, links."""
    text = _RE_CODE.sub(r"\1", text)
    text = _RE_BOLD.sub(r"\2", text)
    text = _RE_ITALIC.sub(r"\2", text)
    text = _RE_LINK.sub(r"\1 (\2)", text)
    return text


def to_messenger_plain(text: str) -> str:
    """Render common Markdown into plain Messenger text.

    Raises TypeError if ``text`` is not a str. Non-string or empty input is
    returned unchanged.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    if not text:
        return text

    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped.startswith("|"):
            block: list[str] = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.append(" — ".join(_render_inline(c) for c in _cells(block[0])))
            for row in block[1:]:
                if _is_table_sep(row):
                    continue
                out.append(" — ".join(_render_inline(c) for c in _cells(row)))
            continue
        if _TABLE_SEP_LINE.match(stripped):
            i += 1
            continue
        rendered = stripped
        rendered = _RE_CODE.sub(r"\1", rendered)
        rendered = _RE_BOLD.sub(r"\2", rendered)
        rendered = _RE_ITALIC.sub(r"\2", rendered)
        rendered = _RE_LINK.sub(r"\1 (\2)", rendered)
        if rendered.startswith("#"):
            rendered = re.sub(r"#+\s+", "", rendered, count=1)
        rendered = re.sub(r"^[\-\*+]\s+", "\u2022 ", rendered, count=1)
        out.append(rendered)
        i += 1

    # Collapse runs of 3+ blank lines to 2.
    result: list[str] = []
    run: list[str] = []
    for ln in out:
        if not ln.strip():
            run.append("")
        else:
            if len(run) >= 3:
                result.extend(["", ""])
            else:
                result.extend(run)
            run = []
            result.append(ln)
    if len(run) >= 3:
        result.extend(["", ""])
    else:
        result.extend(run)
    return "\n".join(result)


if __name__ == "__main__":
    # Self-check: tables, emphasis, headings, bullets, links, blanks, vietnamese.
    assert "|" not in to_messenger_plain("| a | b |\n|---|---|\n| 1 | 2 |")
    # The separator row must vanish entirely; leaving "--- — ---" is the bug
    # this self-check exists for.
    assert to_messenger_plain("| a | b |\n| --- | --- |\n| 1 | 2 |") == "a — b\n1 — 2"
    assert to_messenger_plain("| a | b |\n|:--|--:|\n| 1 | 2 |") == "a — b\n1 — 2"
    assert to_messenger_plain("**bold** and *italic*") == "bold and italic"
    assert to_messenger_plain("plain `code` text") == "plain code text"
    assert to_messenger_plain("# Heading\n\n\n\nline") == "Heading\n\n\nline"
    assert to_messenger_plain("- one\n- two") == "\u2022 one\n\u2022 two"
    assert to_messenger_plain("[link](http://x)") == "link (http://x)"
    viet = "Xin chào bảng giá 3PN: 3 tỷ, liên hệ ngay!"
    assert to_messenger_plain(viet) == viet
    assert to_messenger_plain("") == ""
    assert to_messenger_plain("1. first\n2. second") == "1. first\n2. second"
    print("self-check ok")
