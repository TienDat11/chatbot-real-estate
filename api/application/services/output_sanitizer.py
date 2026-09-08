"""Output sanitizer — keeps internal chunk identifiers out of visible answers (spec §8).

Two complementary entry points, both pure with respect to caller state:

- ``sanitize_answer_text``: one-shot post-processing for a complete answer.
  Applied inside ``RagRgreConvWorkflow`` right before the result payload is
  assembled, so the authoritative ``done`` answer is always clean even if a
  marker shape slipped past the streaming guards.
- ``StreamingSanitizer``: stateful hold-back buffer for SSE token deltas.
  LLM markers can be split across token boundaries, so a per-token filter is
  structurally unable to catch them; the buffer withholds an unterminated
  ``[`` tail (until the bracket resolves into a citation, a markdown link, or
  a removable internal id) plus a rolling 36-char window that guards bare
  UUID shapes, and ``flush()`` releases whatever remains at end of stream
  after a termination pass drops fragments that can no longer resolve:
  unterminated ``[`` groups, dangling ``full_doc_id``/``doc_id``/``source_node``
  stems, growing ``v_unit_*`` view-name stems, cut-off ``fe-`` ids, partial
  UUID-shaped runs, and artifact-only ``(fe-..., v_unit_...)`` paren groups that
  never closed. The same termination pass runs on the authoritative
  ``done`` answer so a cut-off marker never reaches the client through either channel.

Literal HTML ``<br>`` tags the model emits inside markdown cells and bullet lines
are never blocked but never shipped either: both entry points swap them for a real
newline, or for `` • `` when the tag sits in a table row (a cell cannot span lines).
The streaming buffer holds a ``<br`` stem atomically until the tag completes, so a
delta boundary can never release half a tag.

Numeric citations ``[1]``, markdown links, and structured ``sources[]`` /
``facts[]`` payloads are never touched: the sanitizer only ever sees answer
text, and bracketed groups are removed only when their content is not purely
numeric and they are not immediately followed by ``(`` (markdown link form).
"""

from __future__ import annotations

import re

# Length of a canonical UUID string; the streaming buffer withholds this many
# trailing characters so a UUID split across deltas is never released half-formed.
UUID_TOKEN_LENGTH = 36

# Bracketed group whose content is NOT purely numeric -> internal id marker
# (e.g. "[id-của-chunk]", "[chunk_abc123]", "[node_9f2]", "[doc_camellia_cs_bh_2024]").
# "[1]"/"[2]" citations fail the negative lookahead and are preserved; a bracket
# immediately followed by "(" is markdown-link label text and is preserved too.
_BRACKETED_INTERNAL_ID = re.compile(r"\[(?![0-9]+\])([^\[\]]+)\](?!\()\s*")

# Bare UUID-shaped token anywhere in prose (spec example: a1b2c3d4-e5f6-7890-abcd-ef1234567890).
_BARE_UUID_TOKEN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b\s*"
)

# Partial UUID-shaped token: a dashed hex run whose first group is already a full
# 8-char UUID group but the run is NOT the complete 8-4-4-4-12 shape — an internal
# id cut off mid-transmission (e.g. "a1b2c3d4-e5f6" or "a1b2c3d4-e5f6-7890-abcd-ef12").
# The 8-char first group AND at least one dash group are required so short numeric
# ranges ("5-7", "100-200") and dates ("2024-08-24") survive untouched.
_PARTIAL_UUID = re.compile(
    r"(?<![0-9a-fA-F-])[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{1,4}){1,3}"
    r"(?:-[0-9a-fA-F]{0,12})?(?![0-9a-fA-F-])\s*"
)

# Internal key=value / key: value leaks ("full_doc_id=camellia_x", "doc_id: camellia_x",
# "source_node=..."). The whole fragment is dropped so surrounding prose stays readable.
_KEY_VALUE_LEAK = re.compile(
    r"\b(?:full_doc_id|doc_id|source_node)\b\s*[=:]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,.;:!?)]+)\s*"
)

# Stem words that can grow into a key=value leak; used by the streaming buffer to
# find atomic withhold segments.
_KEY_LEAK_STEM = re.compile(r"(?:full_doc_id|doc_id|source_node)")

# Termination-only: a key-leak stem left dangling at end of stream (no value ever
# arrived) would surface as "doc_id:" / "full_doc_id=" if released as-is. The
# separator is optional so a bare trailing stem is caught too; a prose word that
# merely ends with the stem is protected by ``$``.
_KEY_STEM_TERMINATED = re.compile(r"\b(?:full_doc_id|doc_id|source_node)\b\s*[=:]?\s*$")

# Internal fe ids leaked as BARE prose (not bracketed citations): the reported
# answer wrote "(fe-001 đến fe-004, v_unit_estimates)" as literal text. The range
# form ("fe-001 đến fe-004" / "fe-001 - fe-004" / "fe-001 to fe-004") is removed
# first and as ONE unit so its connector never dangles behind an orphaned id;
# then standalone tokens. Trailing whitespace is consumed like every pattern here.
_FE_RANGE_TOKEN = re.compile(r"\bfe-\d{1,4}\s*(?:đến|to|[-–—])\s*fe-\d{1,4}\b\s*", re.IGNORECASE)
_FE_ID_TOKEN = re.compile(r"\bfe-\d{1,4}\b\s*", re.IGNORECASE)

# Internal artifacts leaked inside parentheses ("(fe-001 đến fe-004,
# v_unit_estimates)", "(fe-001, v_unit_estimates)"): when the WHOLE group is
# artifact-only (fe ids, internal view names, range connectors, punctuation,
# whitespace) it is removed INCLUDING the parens so connectors and commas never
# dangle behind individually stripped tokens. Any prose word inside the group
# disqualifies the match and the group is preserved untouched.
_PAREN_ARTIFACT_GROUP = re.compile(
    r"\((?:\s*\bfe-\d{1,4}\b\s*|\s*\bv_unit_[a-z_]+\b\s*|\s*(?:đến|to)\s*"
    r"|[,;\-–—()]|\s)+\)\s*",
    re.IGNORECASE,
)

# Parens left EMPTY by the passes above ("(, )", "()"): cosmetic shells with no
# readable content are dropped so the answer keeps flowing naturally.
_EMPTY_PAREN_RESIDUE = re.compile(r"\(\s*[;,]?\s*\)\s*")

# Internal estimate-view names ("v_unit_estimates") leaked bare into prose.
_V_UNIT_VIEW_TOKEN = re.compile(r"\bv_unit_[a-z_]+\b\s*", re.IGNORECASE)

# Streaming helpers: identifier characters a growing view-name stem can still
# traverse before prose closes it (mirrors the key-leak stem mechanism).
_V_UNIT_STEM = re.compile(r"\bv_unit", re.IGNORECASE)

# Termination-only: a dangling view-name fragment ("v_unit", "V_unit_est") at end
# of stream can no longer resolve and must be dropped instead of released.
_V_UNIT_STEM_TERMINATED = re.compile(r"\bv_unit(?:_[a-z_]*)?\b\s*$", re.IGNORECASE)

# Termination-only sibling for fe ids cut off mid-token at stream death ("fe-00");
# complete ids are already removed by the blocked passes above.
_FE_ID_TERMINATED = re.compile(r"\bfe-[0-9]{0,4}\s*$", re.IGNORECASE)

# Literal HTML <br> tags the model sometimes emits inside markdown table cells and
# bullet lines ("• 3,91 tỷ<br>• 4,31 tỷ"). React's renderer has no rehype-raw, so it
# would show the tag as raw text: the backend must never ship it. The tag is swapped
# for a real newline outside a table row and for " • " inside one, because a markdown
# cell cannot span lines.
_BR_TAG = re.compile(r"<br[ \t]*/?[ \t]*>", re.IGNORECASE)

# Streaming helper: any prefix of _BR_TAG that can still grow into a complete tag.
# Held atomically so a delta boundary can never release a half tag ("<br" + ">").
_BR_STEM = re.compile(r"<(?:b(?:r[ \t]*(?:/[ \t]*)?)?)?", re.IGNORECASE)

# Termination-only: a br stem dangling at end of stream can never close, so it is
# dropped instead of surfacing as literal HTML. A bare "<" is excluded on purpose —
# it can be legitimate prose ("kích thước < 50"), while "<b" has no other reading.
_BR_STEM_TERMINATED = re.compile(r"<b(?:r[ \t]*(?:/[ \t]*)?)?$", re.IGNORECASE)

# Table-cell bullet runs: a model commonly writes "• A<br>• B" inside one cell, so
# the " • " a <br> becomes can abut an existing bullet and double it. The FE mirror
# collapses any run of two or more middle-dot bullets to a single " • " separator
# (packages/ui/src/inline-format.ts); these patterns let the backend do the same at
# each table-row replacement, scoped to the tag's immediate vicinity so prose
# newlines and br-free text stay byte-identical.
_TRAILING_BULLET_RUN = re.compile(r"(?:[ \t]*•)+[ \t]*\Z")
_LEADING_BULLET_RUN = re.compile(r"[ \t]*(?:•[ \t]*)+")

# Streaming helper: the maximal whitespace/bullet/<br> cluster abutting a tag on
# either side. Held atomic so the local bullet-run merge always sees its
# neighbouring bullets (and the whitespace the merge consumes) in the SAME released
# batch, which is what keeps the per-batch " • " collapse byte-identical to the
# one-shot pass over the answer. The trailing "[ \t]*" mirrors what the absorber
# eats after each bullet, so a delta cut can never strand that spacing.
_BR_CLUSTER_TOKEN = re.compile(r"[ \t]*(?:•|<br[ \t]*/?[ \t]*>)[ \t]*", re.IGNORECASE)
_BR_CLUSTER_BEFORE = re.compile(
    r"(?:[ \t]*(?:•|<br[ \t]*/?[ \t]*>))+[ \t]*\Z", re.IGNORECASE
)

# Streaming helpers: a maximal hex/dash run whose shape can still become a UUID
# is withheld atomically until it completes, is disproven by a closing
# non-hex character, or is judged wholesale by flush().
_HEXDASH_RUN = re.compile(r"[0-9a-fA-F][0-9a-fA-F-]*")
_HEX_DASH_CHARS = frozenset("0123456789abcdefABCDEF-")
_UUID_SHAPE_PREFIX = re.compile(
    r"[0-9a-fA-F]{1,8}(?:-[0-9a-fA-F]{1,4}){0,3}(?:-[0-9a-fA-F]{0,12})?"
)

# Streaming helpers: an fe id stem ("fe", "fe-00") grows digit-by-digit into a
# blocked token. Once a non-hexdash character closes the hexdash run the UUID
# guard drops it, so fe ids need their OWN atomic spans: withhold from the stem
# start until the token resolves (complete id, prose disproof, or flush).
_FE_ID_STEM = re.compile(r"\bfe(?:-[0-9]{0,4})?", re.IGNORECASE)
# A connector directly after a complete id means a range may still be arriving;
# only an fe-stem-shaped tail keeps that range alive ("đến và" is plain prose).
_RANGE_CONNECTOR_RE = re.compile(r"\s*(?:đến|to|[-–—])\s*", re.IGNORECASE)
_RANGE_TAIL_GROWING = re.compile(r"f(?:e(?:-[0-9a-fA-F]{0,4})?)?$", re.IGNORECASE)
# Streaming helper: characters/stems that keep an opened "(" eligible as an
# artifact-only group; used by flush()-side judgment on COMPLETE text only.
_ARTIFACT_PAREN_ATOM = re.compile(
    r"(?:\bfe-[0-9]{0,4}\b\s*|\bv_unit_[a-z_]{0,20}\b\s*|đến|to|[,;\-–—()\s])*",
    re.IGNORECASE,
)

# Streaming helpers for INCREMENTAL artifact-paren judgment (chars arrive one
# delta at a time, so partial words must stay eligible until disproven).
_ARTIFACT_CONNECTOR_WORD = re.compile(r"(?:đến|to)(?![^\W\d_])", re.IGNORECASE)
_WORD_RUN_RE = re.compile(r"[^\W\d][\w]*", re.UNICODE)
_FE_STEM_PREFIX_FULLMATCH = re.compile(r"f(?:e(?:-[0-9a-fA-F]{0,4})?)?", re.IGNORECASE)
_VIEW_NAME_FULLMATCH = re.compile(r"v_unit_[a-z_]{0,20}", re.IGNORECASE)


def _scan_artifact_paren(buffer: str, pos: int) -> tuple[bool, int | None]:
    """Judge paren content from ``pos`` incrementally; (resolved, close_end).

    (True, int)  — ")" reached: the group is artifact-only up to it (end past ")");
    (True, None) — a complete unknown word / stray character: REAL prose, the
                   opener never needs withholding;
    (False, None)— everything seen so far can still belong to an artifact-only
                   group (tokens consumed, or a partial fe/view/connector word
                   still arriving) — the opener must stay withheld.
    """
    while True:
        while pos < len(buffer) and buffer[pos] in ",;-–—() \t":
            if buffer[pos] == ")":
                return True, pos + 1
            pos += 1
        if pos >= len(buffer):
            return False, None
        token = _FE_ID_TOKEN.match(buffer, pos)
        if token is not None:
            pos = token.end()
            continue
        token = _V_UNIT_VIEW_TOKEN.match(buffer, pos)
        if token is not None:
            pos = token.end()
            continue
        connector = _ARTIFACT_CONNECTOR_WORD.match(buffer, pos)
        if connector is not None:
            pos = connector.end()
            continue
        head = _WORD_RUN_RE.match(buffer, pos)
        if head is not None and head.end() == len(buffer):
            # A word run reaching the buffer end may STILL complete into a
            # connector, fe id or view name ("đ", "to", "fe-00", "v_unit_es").
            lowered = head.group().lower()
            if (
                "đến".startswith(lowered)
                or "to".startswith(lowered)
                or _FE_STEM_PREFIX_FULLMATCH.fullmatch(lowered) is not None
                or _VIEW_NAME_FULLMATCH.fullmatch(lowered) is not None
            ):
                return False, None
        return True, None


# Order matters: key-leak fragments are removed first so their values (which may
# themselves be UUIDs) never reach the later standalone-UUID pass as orphans;
# full UUIDs are removed before partial ones for the same reason. Bracketed ids
# precede bare fe tokens so "[fe-002]" is removed WHOLE instead of shipping as
# "[]"; parenthesized artifact groups go before their bare-token parts so the
# whole shape is removed in one pass, and fe ranges precede standalone ids so
# connectors never dangle. Empty-paren residue runs LAST to clean shells left
# behind by all earlier passes.
_BLOCKED_PATTERNS = (
    _KEY_VALUE_LEAK,
    _BRACKETED_INTERNAL_ID,
    _PAREN_ARTIFACT_GROUP,
    _FE_RANGE_TOKEN,
    _FE_ID_TOKEN,
    _BARE_UUID_TOKEN,
    _PARTIAL_UUID,
    _V_UNIT_VIEW_TOKEN,
    _EMPTY_PAREN_RESIDUE,
)


def _drop_unterminated_bracket_tail(text: str) -> str:
    """Drop from the first ``[`` that never closes, unless it is a citation prefix.

    The stream is over, so an open bracket can no longer resolve into a citation
    ``]`` or a markdown link ``](url)``. Content that is not purely numeric is an
    internal-id marker (or a link label that can never complete) and must not
    reach the client, so the whole tail is removed.
    """
    for match in re.finditer(r"\[", text):
        start = match.start()
        if text.find("]", start) != -1:
            continue  # resolved bracket; the complete-form pass already judged it
        if text[start + 1 :].rstrip().isdigit():
            continue  # citation prefix like "[1" — preserved
        return text[:start]
    return text


def _drop_unterminated_artifact_paren(text: str) -> str:
    """Drop from the first "(" whose content is artifact-only and never closes.

    The stream is over, so such a group can never resolve into its wholesale
    removal form; the tokens inside are already stripped by the blocked passes
    and only a dangling "(", connectors or commas would remain — remove the tail.
    """
    for match in re.finditer(r"\(", text):
        start = match.start()
        pos = _ARTIFACT_PAREN_ATOM.match(text, start + 1).end()
        if pos >= len(text):
            return text[:start]
    return text


def _neutralize_unterminated(text: str) -> str:
    """Termination-only pass: drop fragments that can no longer resolve.

    ``feed`` only ever releases prefixes whose constructs are provably resolved,
    so these shapes cannot appear in a mid-stream batch — they surface only in
    ``flush()`` and in the authoritative ``done``-answer path.
    """
    text = _drop_unterminated_bracket_tail(text)
    text = _drop_unterminated_artifact_paren(text)
    text = _KEY_STEM_TERMINATED.sub("", text)
    text = _V_UNIT_STEM_TERMINATED.sub("", text)
    text = _FE_ID_TERMINATED.sub("", text)
    text = _BR_STEM_TERMINATED.sub("", text)
    return text


def _br_line_is_table(text: str, start: int, line_is_table_before: bool) -> bool:
    """Whether the ``<br>`` tag at ``start`` sits inside a markdown table row.

    A row is recognised by the first non-space character of its line being ``|``.
    When the line began BEFORE ``text`` (a streaming batch cut mid-line) the
    caller's carried state decides instead, which is what keeps the per-batch
    replacements byte-identical to the one-shot pass over the whole answer.
    """
    head = text[:start]
    newline = head.rfind("\n")
    if newline != -1:
        return head[newline + 1 :].lstrip()[:1] == "|"
    return line_is_table_before or head.lstrip()[:1] == "|"


def _merge_table_bullet_separator(left: str, right: str) -> tuple[str, int]:
    """Trim bullet runs abutting a table-row ``<br>`` so one " • " separates them.

    Returns the left text with any trailing bullet run removed, plus the number of
    leading characters of ``right`` (a bullet run) the caller must skip; the caller
    then emits a single " • " between the two. This mirrors the FE collapse of
    repeated " • " runs, but only touches the tag's immediate neighbourhood so a
    br-free cell or a prose line is never rewritten.
    """
    trimmed_left = _TRAILING_BULLET_RUN.sub("", left)
    lead = _LEADING_BULLET_RUN.match(right)
    return trimmed_left, (lead.end() if lead is not None else 0)


def _expand_br_cluster(buffer: str, start: int, end: int) -> tuple[int, int]:
    """Widen a complete ``<br>`` span to the whole adjacent bullet/tag cluster.

    The local " • " merge needs the bullets flanking the tag present in the same
    released batch; extending the atomic span over the contiguous run of
    whitespace, bullets and further ``<br>`` tags guarantees that, so a delta cut
    can never land between a tag and the bullet it would have collapsed with.
    """
    cluster_end = end
    while True:
        token = _BR_CLUSTER_TOKEN.match(buffer, cluster_end)
        if token is None or token.end() == cluster_end:
            break
        cluster_end = token.end()
    cluster_start = start
    while True:
        before = _BR_CLUSTER_BEFORE.search(buffer, 0, cluster_start)
        if before is None or before.start() == cluster_start:
            break
        cluster_start = before.start()
    return cluster_start, cluster_end


def _replace_br_tags(text: str, *, line_is_table_before: bool) -> str:
    """Swap every literal ``<br>`` variant for its markdown-safe separator.

    Runs LAST so no earlier removal pass can eat a newline this produces (their
    trailing-whitespace consumption is what keeps batch boundaries equivalent).
    Inside a table row the separator is " • " and any bullet run already abutting
    the tag is absorbed, so a cell like "• A<br>• B" never ships a doubled " • • "
    (FE parity); the prose branch keeps emitting a bare newline.
    """
    if "<" not in text:
        return text
    parts: list[str] = []
    position = 0
    for match in _BR_TAG.finditer(text):
        start, end = match.span()
        if _br_line_is_table(text, start, line_is_table_before):
            parts.append(text[position:start])
            merged_left, consumed = _merge_table_bullet_separator(
                "".join(parts), text[end:]
            )
            parts = [merged_left, " • "]
            position = end + consumed
        else:
            parts.append(text[position:start])
            parts.append("\n")
            position = end
    parts.append(text[position:])
    return "".join(parts)


def _table_line_state_after(state: bool | None, text: str) -> bool | None:
    """Carry the "current line is a table row" flag across released batches.

    ``None`` means the line has emitted only whitespace so far, so its first real
    character is still unseen; any newline in ``text`` reopens the judgment for
    the line that follows it.
    """
    if "\n" in text:
        state = None
        text = text[text.rfind("\n") + 1 :]
    if state is not None:
        return state
    stripped = text.lstrip()
    if not stripped:
        return None
    return stripped[0] == "|"


def sanitize_answer_text(
    answer_text: str,
    strip_leading_artifact_space: bool = True,
    *,
    terminated: bool = False,
    line_is_table_before: bool = False,
) -> str:
    """Remove every §8 blocked pattern from a complete answer; allowed text is untouched.

    Each removal also consumes the whitespace run FOLLOWING the fragment so
    "trả lời [chunk_x] tiếp" becomes "trả lời tiếp" instead of leaving a double
    space. Trailing-side consumption keeps streaming equivalence: a batch boundary
    can only land before a fragment, never inside its consumed whitespace. When a
    blocked fragment sits at position 0 the removal leaves a leading space behind;
    that artifact is stripped only when ``strip_leading_artifact_space`` is set
    (the streaming buffer passes False for continuation batches).

    ``terminated`` marks the end of the stream: unterminated ``[`` groups and
    dangling key-leak stems can no longer resolve, so they are neutralized too.
    ``feed()`` keeps it False (released prefixes are already provably clean);
    ``flush()`` and the ``done``-answer path pass True.

    ``line_is_table_before`` carries the table-row state of the line that was
    already emitted before ``answer_text`` begins; only the streaming buffer has
    a reason to set it, so the " • " vs newline choice for a literal ``<br>`` tag
    comes out identical whether the text is sanitized in batches or in one pass.
    """
    sanitized = answer_text
    for pattern in _BLOCKED_PATTERNS:
        sanitized = pattern.sub("", sanitized)
    if terminated:
        sanitized = _neutralize_unterminated(sanitized)
    sanitized = _replace_br_tags(sanitized, line_is_table_before=line_is_table_before)
    if strip_leading_artifact_space and sanitized[:1].isspace() and not answer_text[:1].isspace():
        sanitized = sanitized[1:]
    return sanitized


class StreamingSanitizer:
    """Hold-back buffer that sanitizes SSE token deltas without leaking split markers.

    ``feed`` returns only the prefix of the accumulated text that is provably free
    of unresolved blocked constructs; ``flush`` releases the remainder (already
    sanitized) at end of stream. The guards are deliberately conservative — a
    bracket left open to the end of the answer stalls its tail until ``flush``,
    which is the correct trade-off: a cosmetic latency tail is recoverable, a
    leaked internal id is not.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Drop buffered text; called per run so one instance can serve reruns safely."""
        self._pending_text = ""
        self._has_emitted_batch = False
        # Table-row state of the line currently being emitted; None while that
        # line has produced only whitespace (or nothing at all yet). Lets a <br>
        # whose row started in an earlier batch still be judged exactly as the
        # one-shot pass over the whole answer would judge it.
        self._line_is_table: bool | None = None

    def feed(self, delta: str) -> str:
        """Absorb one token delta; return the newly releasable, sanitized prefix."""
        self._pending_text += delta
        releasable_length = self._releasable_length()
        if releasable_length <= 0:
            return ""
        released = sanitize_answer_text(
            self._pending_text[:releasable_length],
            strip_leading_artifact_space=not self._has_emitted_batch,
            line_is_table_before=bool(self._line_is_table),
        )
        self._pending_text = self._pending_text[releasable_length:]
        self._line_is_table = _table_line_state_after(self._line_is_table, released)
        self._has_emitted_batch = True
        return released

    def flush(self) -> str:
        """Release everything still buffered, sanitized; the stream is over, so
        an unterminated bracket or a dangling key-leak stem can no longer resolve
        and is neutralized instead of reaching the client."""
        # Leading-artifact stripping stays reserved for the very start of the
        # answer so the assembled batches always equal the one-shot sanitize.
        # terminated=True judges the withheld tail wholesale: unterminated "["
        # groups and dangling key stems are removed, not released.
        remainder = sanitize_answer_text(
            self._pending_text,
            strip_leading_artifact_space=not self._has_emitted_batch,
            terminated=True,
            line_is_table_before=bool(self._line_is_table),
        )
        self.reset()
        return remainder

    def _releasable_length(self) -> int:
        """Longest prefix of the buffer that contains no partially-arrived marker.

        A released prefix is only ever extended to positions where every blocked
        construct is either fully contained in the release or fully withheld.
        Three mechanisms, combined by taking the minimum:

        - UUID guards: withhold the trailing 35 chars (a full UUID needs 36, so
          no complete UUID and no completable prefix sits in a released batch),
          plus every hex/dash run whose shape can still become a UUID is held
          atomically from its first char — a token that started arriving early
          would otherwise scroll out of the rolling window half-formed;
        - atomic segments: every "[" (until its "]" plus one paren-check char)
          and every key-leak stem (until its value terminates or prose-proves
          itself) forms an indivisible span; the cut may end at a span's start
          or past its end, never inside it — otherwise a fragment split across
          two releases would be sanitized as two harmless halves;
        - unresolved spans withhold from their start until they resolve (or,
          failing that, until ``flush`` judges the tail wholesale).
        """
        buffer = self._pending_text
        releasable_length = max(0, len(buffer) - (UUID_TOKEN_LENGTH - 1))

        for span_start, span_end in self._atomic_spans(buffer):
            if span_end is None:
                releasable_length = min(releasable_length, span_start)
            elif span_start < releasable_length < span_end:
                releasable_length = span_start
        return releasable_length

    @staticmethod
    def _atomic_spans(buffer: str) -> list[tuple[int, int | None]]:
        """Indivisible (start, end-or-None) spans of possibly-blocked constructs."""
        spans: list[tuple[int, int | None]] = []
        for match in re.finditer(r"\[", buffer):
            start = match.start()
            closing = buffer.find("]", start)
            if closing == -1:
                spans.append((start, None))
            else:
                # One char past "]" is the minimum needed to rule out "(url)".
                paren_check = closing + 2
                spans.append((start, paren_check if paren_check <= len(buffer) else None))
        for match in re.finditer(r"\(", buffer):
            # An artifact-only paren group must reach a released batch WHOLE:
            # its wholesale removal (parens included) only matches on the full
            # shape, so a mid-group cut would strand "(", connectors or commas
            # in released prose. Withhold from "(" while the content can still
            # belong to such a group; prose inside releases normally.
            start = match.start()
            resolved, close_end = _scan_artifact_paren(buffer, start + 1)
            if not resolved:
                spans.append((start, None))  # still forming — keep withholding
            elif close_end is not None:
                spans.append((start, close_end))
        for match in _KEY_LEAK_STEM.finditer(buffer):
            start = match.start()
            leak = _KEY_VALUE_LEAK.match(buffer, start)
            if leak is None:
                next_after_stem = match.end()
                following = buffer[next_after_stem] if next_after_stem < len(buffer) else ""
                if following and not (following.isspace() or following in "=:'\""):
                    # "doc_ids..." can never become a key=value leak anymore.
                    spans.append((start, next_after_stem))
                else:
                    spans.append((start, None))
            elif leak.end() < len(buffer):
                spans.append((start, leak.end()))
            else:
                # Matched to the very end — value chars may still be arriving.
                spans.append((start, None))
        for match in _V_UNIT_STEM.finditer(buffer):
            start = match.start()
            view_token = _V_UNIT_VIEW_TOKEN.match(buffer, start)
            if view_token is None:
                next_after_stem = match.end()
                following = buffer[next_after_stem] if next_after_stem < len(buffer) else ""
                # Only an underscore can still grow the stem into a view name
                # ("v_unit_estimates"); ANY other char (letters included:
                # "v_units...") proves prose forever. Empty means chars may
                # still be arriving, so the span stays withheld until it
                # resolves or flush() judges the tail wholesale. Unlike fe ids,
                # "v_unit_estimates" is NOT hexdash-safe and would otherwise be
                # released mid-token across batches.
                if following and following != "_":
                    continue
                spans.append((start, None))
            elif view_token.end() < len(buffer):
                spans.append((start, view_token.end()))
            else:
                # Matched to the very end — identifier chars may still be arriving.
                spans.append((start, None))
        for match in _FE_ID_STEM.finditer(buffer):
            start = match.start()
            range_token = _FE_RANGE_TOKEN.match(buffer, start)
            if range_token is not None:
                # A complete "id connector id" range is ONE removable unit:
                # keeping it atomic means a mid-range cut can never strand the
                # connector in released prose (assembled == one-shot).
                spans.append(
                    (start, range_token.end()) if range_token.end() < len(buffer) else (start, None)
                )
                continue
            id_token = _FE_ID_TOKEN.match(buffer, start)
            if id_token is not None:
                forming = _RANGE_CONNECTOR_RE.match(buffer, id_token.end())
                tail = buffer[forming.end() :] if forming is not None else ""
                if forming is not None and _RANGE_TAIL_GROWING.search(tail) is not None:
                    # Connector plus a still-growing second stem: a range may
                    # yet complete — judge the whole thing as one open span.
                    spans.append((start, None))
                elif id_token.end() < len(buffer):
                    spans.append((start, id_token.end()))
                else:
                    # Matched to the very end — trailing whitespace was eaten,
                    # so a connector may still arrive and grow a range.
                    spans.append((start, None))
                continue
            next_after_stem = match.end()
            following = buffer[next_after_stem] if next_after_stem < len(buffer) else ""
            if following and following not in "0123456789-":
                # Closed by prose ("feature") — this can never become an fe id
                # or range member anymore.
                continue
            spans.append((start, None))  # digits may still be arriving
        for match in _BR_STEM.finditer(buffer):
            start = match.start()
            tag = _BR_TAG.match(buffer, start)
            if tag is not None:
                # A complete tag is indivisible, and so is the bullet run around
                # it: cutting inside the cluster would strand a " • " separator in
                # one batch and the bullet it must collapse with in the next, so
                # the assembled stream would double the bullet where one-shot does
                # not. The span therefore covers the whole adjacent bullet/tag run.
                cluster_start, cluster_end = _expand_br_cluster(
                    buffer, tag.start(), tag.end()
                )
                spans.append(
                    (cluster_start, cluster_end)
                    if cluster_end < len(buffer)
                    else (cluster_start, None)
                )
                continue
            if match.end() == len(buffer):
                # "<", "<b", "<br ", "<br/" — a character that still completes a
                # tag may be arriving; withhold until it resolves or flush judges it.
                spans.append((start, None))
        for match in _HEXDASH_RUN.finditer(buffer):
            complete_token = _BARE_UUID_TOKEN.match(buffer, match.start())
            if complete_token is not None:
                # Full UUID (plus the trailing whitespace its removal eats).
                spans.append((complete_token.start(), complete_token.end()))
                continue
            core = match.group().rstrip("-")
            if not core:
                continue
            terminator_position = match.end()
            terminator = buffer[terminator_position] if terminator_position < len(buffer) else ""
            if terminator and terminator not in _HEX_DASH_CHARS:
                # Run closed by prose — this can never become a UUID anymore.
                continue
            if _UUID_SHAPE_PREFIX.fullmatch(core):
                spans.append((match.start(), None))
        return spans


__all__ = [
    "StreamingSanitizer",
    "UUID_TOKEN_LENGTH",
    "sanitize_answer_text",
]
