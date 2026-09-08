"""Output sanitizer (spec §8) — blocked internal-id markers vs allowed citations.

Verifies:
1. Every BLOCKED example is removed from visible answer text (one-shot).
2. Every ALLOWED example survives byte-identical (numeric citations, markdown links).
3. StreamingSanitizer: markers sliced across 5 deltas never appear in any emitted
   chunk, and the assembled output equals the one-shot sanitize of the full text.
4. Workflow wiring: token deltas are sanitized on the emit path, metadata frames
   stay byte-identical, and the done-payload answer is re-sanitized while
   sources[]/facts[] remain deep-equal pre/post.
"""

import inspect
import math
import re

import pytest

from api.application.pipelines.conv_workflow import RagRgreConvWorkflow
from api.application.services.output_sanitizer import (
    StreamingSanitizer,
    _merge_table_bullet_separator,
    sanitize_answer_text,
)
from api.domain.value_objects.constants import (
    SSE_EVENT_SOURCES,
    SSE_EVENT_TOKEN,
)

# ==============================================================================
# 1. Blocked patterns (spec §8 table)
# ==============================================================================

BLOCKED_CASES = [
    # (answer_text_with_marker, marker_that_must_vanish)
    ("Chính sách bán hàng [id-của-chunk] như sau đây.", "[id-của-chunk]"),
    ("Ưu đãi [chunk_abc123] áp dụng đến hết tháng.", "[chunk_abc123]"),
    ("Phân khu này [node_9f2] có view hồ bơi.", "[node_9f2]"),
    ("Chính sách [doc_camellia_cs_bh_2024] mới nhất.", "[doc_camellia_cs_bh_2024]"),
    (
        "Mã tài liệu a1b2c3d4-e5f6-7890-abcd-ef1234567890 thuộc dự án.",
        "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    ),
    ("Thông tin chi tiết full_doc_id=camellia_x được cập nhật.", "full_doc_id=camellia_x"),
    ("Tra cứu theo doc_id: camellia_x để biết thêm.", "doc_id: camellia_x"),
    ("Nguồn dữ liệu source_node=rag_chunk_42 đã hợp nhất.", "source_node=rag_chunk_42"),
    # Bare fe ids / internal view names leaked as prose (reported bug answer).
    (
        "Bảng giá gồm 6 loại (fe-001 đến fe-004, v_unit_estimates) anh tham khảo.",
        "(fe-001 đến fe-004, v_unit_estimates)",
    ),
    ("Giá từ fe-001 đến fe-004 theo bảng định hướng.", "fe-001 đến fe-004"),
    ("Giá treo fe-001 - fe-002 tùy phương án.", "fe-001 - fe-002"),
    ("Mã evidence fe-001 nằm trong hệ thống.", "fe-001"),
    ("Nguồn v_unit_estimates của dự án.", "v_unit_estimates"),
    # Bracketed fe ids are removed WHOLE by the bracketed pass (never ship "[]").
    ("Mã tham chiếu [fe-002] trong hệ thống.", "[fe-002]"),
    # Case-insensitive view-name stripping.
    ("Nguồn V_unit_estimates của dự án.", "V_unit_estimates"),
    # Single-id artifact parens and shells left empty by earlier passes.
    ("Bảng giá (fe-001, v_unit_estimates) anh nhé.", "(fe-001, v_unit_estimates)"),
    ("Toàn bộ (, ) như vậy.", "(, )"),
]


@pytest.mark.parametrize(
    ("answer_text", "marker"), BLOCKED_CASES, ids=[marker for _, marker in BLOCKED_CASES]
)
def test_blocked_example_removed_from_visible_text(answer_text: str, marker: str):
    cleaned = sanitize_answer_text(answer_text)
    assert marker not in cleaned
    # Surrounding Vietnamese prose must stay readable (no doubled spaces).
    assert "  " not in cleaned


def test_blocked_marker_at_start_leaves_no_leading_space():
    cleaned = sanitize_answer_text("[chunk_abc123] là mã nội bộ.")
    assert cleaned == "là mã nội bộ."


# ==============================================================================
# 2. Allowed patterns — byte-identical survival
# ==============================================================================

ALLOWED_CASES = [
    "Căn 2PN view biển đang có chính sách bán hàng như sau [1] ...",
    "Liên hệ [2] để nhận bảng giá.",
    "Nguồn: [1][2] tổng hợp từ CSBH mới nhất.",
    "[Bảng giá Camellia](https://example.com/bg)",
    "Dự án mở bán đợt cuối với tiến độ thanh toán linh hoạt.",
    "",
]


@pytest.mark.parametrize("answer_text", ALLOWED_CASES)
def test_allowed_example_survives_byte_identical(answer_text: str):
    assert sanitize_answer_text(answer_text) == answer_text


# ==============================================================================
# 3. Streaming sanitizer — split-marker safety
# ==============================================================================


def slice_into_deltas(text: str, delta_count: int) -> list[str]:
    """Deterministically cut text into N non-empty deltas of similar size."""
    total = len(text)
    return [
        text[math.ceil(total * i / delta_count) : math.ceil(total * (i + 1) / delta_count)]
        for i in range(delta_count)
        if math.ceil(total * i / delta_count) < math.ceil(total * (i + 1) / delta_count)
    ]


SPLIT_MARKER_CASES = [
    "Bảng giá chi tiết [chunk_a1b2c3] cho căn 2PN nhé.",
    "Mã nội bộ [id-của-chunk] không hiển thị cho khách.",
    "Tham chiếu a1b2c3d4-e5f6-7890-abcd-ef1234567890 nội bộ.",
    "Kèm theo full_doc_id=camellia_x trong hệ thống.",
    "Đối chiếu doc_id: soleil_cs_bh để xác nhận.",
]


@pytest.mark.parametrize("full_text", SPLIT_MARKER_CASES)
def test_split_marker_never_appears_in_any_emitted_chunk(full_text: str):
    sanitizer = StreamingSanitizer()
    emitted_chunks = []
    for delta in slice_into_deltas(full_text, 5):
        chunk = sanitizer.feed(delta)
        emitted_chunks.append(chunk)
    emitted_chunks.append(sanitizer.flush())
    assembled = "".join(emitted_chunks)
    for fragment in _blocked_fragments(full_text):
        for chunk in emitted_chunks:
            assert fragment not in chunk
        assert fragment not in assembled
    assert assembled == sanitize_answer_text(full_text)


def _blocked_fragments(full_text: str) -> list[str]:
    """The exact substrings each case above must never let reach the client."""
    fragments = []
    for marker in (
        "[chunk_a1b2c3]",
        "[id-của-chunk]",
        "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "full_doc_id=camellia_x",
        "doc_id: soleil_cs_bh",
    ):
        if marker in full_text:
            fragments.append(marker)
    return fragments


# Reported bug answer shapes: "fe-001" is hexdash-only (the UUID guard alone
# SHREDS it once a closing char drops the hexdash span — fe ids have their own
# atomic stem/range spans) while "v_unit_estimates" contains non-hex chars and
# needs its own atomic stem span. Neither may EVER surface, in any single batch
# nor in the assembled stream, however the deltas split the tokens — and the
# assembled output must equal the one-shot sanitize (no "(, )" style residue).
SPLIT_TOKEN_CASES = [
    "Dạ dự án có 6 loại căn hộ (fe-001 đến fe-004, v_unit_estimates) anh nhé.",
    "Nguồn số liệu v_unit_estimates và mã fe-001 đến fe-008 nội bộ.",
    "Gửi anh Nguồn V_unit_estimates và mã FE-001 đến FE-008 nội bộ.",
]


@pytest.mark.parametrize("full_text", SPLIT_TOKEN_CASES)
@pytest.mark.parametrize("delta_size", [1, 2, 3, 5, 7])
def test_split_fe_and_v_unit_tokens_never_released(full_text: str, delta_size: int):
    sanitizer = StreamingSanitizer()
    chunks = [
        sanitizer.feed(full_text[i : i + delta_size]) for i in range(0, len(full_text), delta_size)
    ]
    chunks.append(sanitizer.flush())
    assembled = "".join(chunks)
    for chunk in chunks:
        assert not re.search(r"fe-\d|v_unit", chunk, re.IGNORECASE)
    assert not re.search(r"fe-\d|v_unit", assembled, re.IGNORECASE)
    # The prose around the artifacts survives the whole stream.
    assert "loại căn hộ" in assembled or "Nguồn số liệu" in assembled or "Gửi anh" in assembled
    # Streaming equivalence: batches+flush reproduce the one-shot sanitize.
    assert assembled == sanitize_answer_text(full_text)


def test_split_numeric_citation_survives_streaming_intact():
    sanitizer = StreamingSanitizer()
    full_text = "Giá liên quan [1] tham khảo nhé."
    assembled_parts = [sanitizer.feed(d) for d in slice_into_deltas(full_text, 7)]
    assembled_parts.append(sanitizer.flush())
    assembled = "".join(assembled_parts)
    assert "[1]" in assembled
    assert assembled == sanitize_answer_text(full_text)


def test_split_markdown_link_survives_streaming_intact():
    sanitizer = StreamingSanitizer()
    full_text = "Xem [Bảng giá Camellia](https://example.com/bg) ngay."
    assembled_parts = [sanitizer.feed(d) for d in slice_into_deltas(full_text, 8)]
    assembled_parts.append(sanitizer.flush())
    assembled = "".join(assembled_parts)
    assert "[Bảng giá Camellia](https://example.com/bg)" in assembled
    assert assembled == sanitize_answer_text(full_text)


def test_stream_assembled_equals_one_shot_for_mixed_answer():
    full_text = (
        "Chính sách [1] hiện hành [chunk_xyz789] gồm chiết khấu 7%; "
        "chi tiết tại [Bảng giá](https://example.com/bg) full_doc_id=cs_2024."
    )
    sanitizer = StreamingSanitizer()
    assembled_parts = [sanitizer.feed(d) for d in slice_into_deltas(full_text, 12)]
    assembled_parts.append(sanitizer.flush())
    assert "".join(assembled_parts) == sanitize_answer_text(full_text)


def test_reset_between_runs_clears_hold_back_state():
    sanitizer = StreamingSanitizer()
    sanitizer.feed("đang giữ [chu")
    sanitizer.reset()
    assert sanitizer.feed("mới hoàn toàn") == ""
    assert sanitizer.flush() == "mới hoàn toàn"


def test_unterminated_bracket_tail_released_by_flush_not_leaked():
    sanitizer = StreamingSanitizer()
    emitted = sanitizer.feed("Trả lời [không đóng")
    # Nothing resolved -> nothing released early; flush judges the tail as-is.
    assert emitted == ""
    flushed = sanitizer.flush()
    assert flushed.startswith("Trả lời")
    assert "[không đóng" not in flushed


# ==============================================================================
# 3b. Flush termination neutralization — fragments that can no longer resolve
# ==============================================================================

TERMINATED_CASES = [
    # (full_text ending mid-marker, fragment_that_must_vanish)
    ("Trả lời [chunk_abc", "[chunk_abc"),
    ("Chi tiết [doc_camellia_2026", "[doc_camellia_2026"),
    ("Nút [node_9f2", "[node_9f2"),
    ("Xem [internal_id_7", "[internal_id_7"),
    ("Tra cứu doc_id: ", "doc_id:"),
    ("Giá theo full_doc_id=", "full_doc_id="),
    ("Nguồn source_node", "source_node"),
    ("Mã a1b2c3d4-e5f6", "a1b2c3d4-e5f6"),
    ("Tham chiếu a1b2c3d4-e5f6-7890-abcd-ef12", "a1b2c3d4-e5f6-7890-abcd-ef12"),
    # Internal stems cut off at stream death.
    ("Nguồn số liệu v_unit", "v_unit"),
    ("Bảng giá v_unit_estimat", "v_unit_estimat"),
    ("Giá theo fe-", "fe-"),
]


@pytest.mark.parametrize(
    ("answer_text", "must_vanish"),
    TERMINATED_CASES,
    ids=[marker for _, marker in TERMINATED_CASES],
)
def test_flush_neutralizes_unterminated_tail(answer_text: str, must_vanish: str):
    sanitizer = StreamingSanitizer()
    parts = [sanitizer.feed(answer_text), sanitizer.flush()]
    assembled = "".join(parts)
    assert must_vanish not in assembled
    # Assembled stream (release prefix + flush) equals the one-shot terminated sanitize.
    assert assembled == sanitize_answer_text(answer_text, terminated=True)


def test_flush_keeps_citation_prefix_and_markdown_link():
    full_text = "Xem [1] và [Bảng giá](https://example.com/bg)"
    sanitizer = StreamingSanitizer()
    parts = [sanitizer.feed(d) for d in slice_into_deltas(full_text, 8)]
    parts.append(sanitizer.flush())
    assembled = "".join(parts)
    assert "[1]" in assembled
    assert "[Bảng giá](https://example.com/bg)" in assembled
    assert assembled == sanitize_answer_text(full_text, terminated=True)


def test_flush_keeps_unclosed_numeric_citation_prefix():
    sanitizer = StreamingSanitizer()
    sanitizer.feed("Giá [1")
    assert sanitizer.flush() == "Giá [1"


def test_terminated_keeps_legit_numbers_ranges_and_dates():
    # 8-hex-first-group + dash requirement must not eat plain prose.
    for text in (
        "Giảm 5-7% nếu mua trong tháng 8.",
        "Giá 100-200 triệu anh nhé.",
        "Cập nhật ngày 2024-08-24.",
        "Mã căn CH-12A, CH-09.",
    ):
        assert sanitize_answer_text(text, terminated=True) == text


# ==============================================================================
# 3c. Literal <br> tags — HTML must never reach the client (FE has no rehype-raw)
# ==============================================================================

BR_TAG_VARIANTS = ["<br>", "<BR>", "<Br>", "<bR>", "<br/>", "<br />", "<BR/>", "<Br  />"]


@pytest.mark.parametrize("tag", BR_TAG_VARIANTS)
def test_br_tag_in_prose_becomes_real_newline(tag: str):
    text = f"• 3,91-4,67 tỷ (Sớm 95%){tag}• 4,31-5,16 tỷ (TT Chuẩn)"
    cleaned = sanitize_answer_text(text)
    assert cleaned == "• 3,91-4,67 tỷ (Sớm 95%)\n• 4,31-5,16 tỷ (TT Chuẩn)"
    assert "<br" not in cleaned.lower()


@pytest.mark.parametrize("tag", BR_TAG_VARIANTS)
def test_br_tag_in_table_row_becomes_bullet_separator(tag: str):
    # A markdown cell cannot span lines, so the row must stay one physical line.
    cleaned = sanitize_answer_text(f"| Căn 2PN | 3,91 tỷ{tag}4,31 tỷ |")
    assert cleaned == "| Căn 2PN | 3,91 tỷ • 4,31 tỷ |"
    assert "<br" not in cleaned.lower()


# A table cell written "• A<br>• B" must not ship a doubled " • • " separator: the
# backend mirrors the FE run-collapse (packages/ui/src/inline-format.ts) so a <br>
# abutting an existing bullet yields exactly one " • ".
BR_TABLE_COLLAPSE_CASES = [
    ("| Sớm | • 3,91 tỷ<br>• 4,31 tỷ |", "| Sớm | • 3,91 tỷ • 4,31 tỷ |"),
    ("| a<br>• b |", "| a • b |"),
    ("| • x<br><br>• y |", "| • x • y |"),
    ("| a•<br>b |", "| a • b |"),
    ("| a<br>  •  b |", "| a • b |"),
    ("| a<br>b |", "| a • b |"),
    # FE parity: a lone space after the tag with no bullet to merge is preserved.
    ("| a<br> b |", "| a •  b |"),
]


@pytest.mark.parametrize(("raw", "expected"), BR_TABLE_COLLAPSE_CASES)
def test_br_table_row_bullet_run_collapses_to_one_separator(raw: str, expected: str):
    cleaned = sanitize_answer_text(raw)
    assert cleaned == expected
    assert " • • " not in cleaned and "••" not in cleaned


def test_br_bullet_run_collapse_is_table_only_prose_keeps_newline():
    # The collapse is scoped to table rows; a prose bullet list still breaks on \n.
    assert sanitize_answer_text("• 3,91 tỷ<br>• 4,31 tỷ") == "• 3,91 tỷ\n• 4,31 tỷ"


def test_merge_table_bullet_separator_helper():
    # Left bullet run trimmed; right bullet run consumed length returned.
    assert _merge_table_bullet_separator("| a•", "b |") == ("| a", 0)
    assert _merge_table_bullet_separator("| a", "• b |") == ("| a", 2)
    assert _merge_table_bullet_separator("| a • ", "• b") == ("| a", 2)
    assert _merge_table_bullet_separator("| a", "b") == ("| a", 0)
    # No bullet on the right: the ordinary cell spacing must NOT be eaten.
    assert _merge_table_bullet_separator("| a", "  b") == ("| a", 0)


def test_br_tag_multi_line_answer_mixes_both_rules():
    text = (
        "| Căn | Giá |\n"
        "|---|---|\n"
        "| 2PN | 3,91 tỷ<br>4,31 tỷ |\n"
        "Chi tiết anh xem thêm bảng hàng nhé.<br>Em hỗ trợ 24/7."
    )
    assert sanitize_answer_text(text) == (
        "| Căn | Giá |\n"
        "|---|---|\n"
        "| 2PN | 3,91 tỷ • 4,31 tỷ |\n"
        "Chi tiết anh xem thêm bảng hàng nhé.\nEm hỗ trợ 24/7."
    )


def test_br_replacement_leaves_legit_less_than_prose_untouched():
    # "<" is a stem, not a tag: plain prose must survive byte-identical.
    for text in ("Kích thước < 50 m² anh nhé.", "A < B và C < D.", "Tỷ lệ 1 < 2 < 3."):
        assert sanitize_answer_text(text, terminated=True) == text


def test_br_replacement_does_not_disturb_fe_range_em_dash():
    text = "Giá từ fe-001 — fe-004 theo bảng định hướng<br>Anh xem thêm nhé."
    cleaned = sanitize_answer_text(text)
    assert "fe-001" not in cleaned
    assert cleaned == "Giá từ theo bảng định hướng\nAnh xem thêm nhé."


def test_br_tag_split_across_deltas_replaced_exactly_once():
    prefix = "Bảng giá tham khảo các loại căn hộ hiện nay như sau đây ạ anh nhé "
    tail = "• 4,31-5,16 tỷ (TT Chuẩn) ạ"
    full_text = prefix + "<br>" + tail
    sanitizer = StreamingSanitizer()
    frames = [
        sanitizer.feed(prefix + "<br"),
        sanitizer.feed(">"),
        sanitizer.feed(tail),
    ]
    frames.append(sanitizer.flush())
    assembled = "".join(frames)
    # No released frame may carry a partial tag, and the replacement appears once.
    for frame in frames:
        assert "<br" not in frame.lower()
        assert "<b" not in frame.lower()
    assert assembled == prefix + "\n" + tail
    assert assembled.count("\n") == 1
    assert assembled == sanitize_answer_text(full_text)


def test_br_tag_in_table_row_streams_as_bullet_separator():
    # The row's leading "|" scrolls out of the hold-back window before the tag is
    # judged, so the table context must be carried across batches, not re-read.
    row = "| Căn 2PN 2WC view hồ | 3,91-4,67 tỷ (Sớm 95%) "
    tail = "4,31-5,16 tỷ (TT Chuẩn) |"
    sanitizer = StreamingSanitizer()
    frames = [sanitizer.feed(row + "<br"), sanitizer.feed(">" + tail)]
    frames.append(sanitizer.flush())
    assembled = "".join(frames)
    for frame in frames:
        assert "<br" not in frame.lower()
    assert assembled == row + " • " + tail
    assert assembled == sanitize_answer_text(row + "<br>" + tail)


def test_flush_drops_dangling_br_stem():
    text = "Bảng giá chi tiết cho các căn hộ như sau đây ạ anh tham khảo thêm<br"
    sanitizer = StreamingSanitizer()
    parts = [sanitizer.feed(text), sanitizer.flush()]
    assembled = "".join(parts)
    assert "<br" not in assembled.lower()
    assert assembled == sanitize_answer_text(text, terminated=True)


def test_flush_drops_dangling_bare_b_stem():
    text = "Bảng giá chi tiết cho các căn hộ như sau đây ạ anh tham khảo<b"
    sanitizer = StreamingSanitizer()
    parts = [sanitizer.feed(text), sanitizer.flush()]
    assert "<b" not in "".join(parts).lower()


BR_STREAM_CASES = [
    "• 3,91-4,67 tỷ (Sớm 95%)<br>• 4,31-5,16 tỷ (TT Chuẩn)<br>• 5,16 tỷ (TT Kéo Dài)",
    "| Căn | Giá |\n|---|---|\n| 2PN | 3,91 tỷ<br>4,31 tỷ |\n| 3PN | 5,2 tỷ<br>6,1 tỷ |",
    "Giá sớm 95%<br>Giá TT chuẩn<br>Anh xem thêm bảng hàng nhé [1].",
    "| 2PN | 3,91 tỷ<br>4,31 tỷ |\nĐó là giá định hướng anh nhé.<br>Em gửi thêm chi tiết.",
    # Table cells whose <br> abuts an existing bullet: the collapse must survive
    # every delta split identically to the one-shot pass (no " • • " ever appears).
    "| Sớm | • 3,91 tỷ<br>• 4,31 tỷ |\n| TT | • 4,31 tỷ<br>• 5,16 tỷ |",
    "| Căn hộ 2PN view công viên nội khu | • 3,91 tỷ sớm<br>• 4,31 tỷ chuẩn |",
    "| • x<br><br>• y |",
]


@pytest.mark.parametrize("full_text", BR_STREAM_CASES)
@pytest.mark.parametrize("delta_size", [1, 2, 3, 5, 7, 13])
def test_br_streaming_equals_one_shot_at_every_delta_size(full_text: str, delta_size: int):
    sanitizer = StreamingSanitizer()
    chunks = [
        sanitizer.feed(full_text[i : i + delta_size]) for i in range(0, len(full_text), delta_size)
    ]
    chunks.append(sanitizer.flush())
    assembled = "".join(chunks)
    for chunk in chunks:
        assert "<br" not in chunk.lower()
    assert "<br" not in assembled.lower()
    # The core invariant: streamed concatenation is byte-identical to done.answer.
    assert assembled == sanitize_answer_text(full_text)
    assert assembled == sanitize_answer_text(full_text, terminated=True)


@pytest.mark.parametrize("full_text", BR_STREAM_CASES[4:])
@pytest.mark.parametrize("delta_size", [1, 2, 3, 5, 7, 13, 17])
def test_br_table_collapse_streams_without_double_bullet(full_text: str, delta_size: int):
    # The bullet-run collapse must hold under streaming too: no delta split may
    # strand a " • " separator in one batch and the bullet it merges with in the
    # next, which would surface a doubled " • • " the one-shot pass never shows.
    sanitizer = StreamingSanitizer()
    chunks = [
        sanitizer.feed(full_text[i : i + delta_size]) for i in range(0, len(full_text), delta_size)
    ]
    chunks.append(sanitizer.flush())
    assembled = "".join(chunks)
    assert " • • " not in assembled and "••" not in assembled
    assert assembled == sanitize_answer_text(full_text)
    assert assembled == sanitize_answer_text(full_text, terminated=True)


# ==============================================================================
# 4. Workflow wiring (RagRgreConvWorkflow emit path + done payload)
# ==============================================================================


class _TokenStreamInner:
    """Fake inner workflow: streams canned token deltas through its on_event."""

    def __init__(self, deltas, result):
        self._deltas = deltas
        self._result = result
        self.on_event = None

    def run(self, **kwargs):
        return self._handler()

    async def _handler(self):
        for delta in self._deltas:
            outcome = self.on_event(SSE_EVENT_TOKEN, {"text": delta})
            if inspect.isawaitable(outcome):
                await outcome
        return self._result


@pytest.mark.asyncio
async def test_token_deltas_streamed_through_sanitizer_and_metadata_frames_identical():
    events = []
    wf = RagRgreConvWorkflow(on_event=lambda e, d: events.append((e, d)))
    sources_frame = {"sources": [{"doc_id": "raw_internal_id", "title": "CSBH"}]}
    deltas = [
        "Xin chào anh/chị, ",
        "dự án có chính sách bán hàng rất tốt trong tháng này. ",
        "[chunk_hidden99]",
        " nhé [1].",
    ]
    wf._inner = _TokenStreamInner(deltas, {"answer": "".join(deltas), "requires_review": False})
    # Bind the workflow's real sanitizing emit path into the stub so deltas are
    # routed exactly like they would be through RagQueryWorkflow.
    wf._inner.on_event = wf._emit_sanitized_stream

    # Metadata frames bypass the sanitizer entirely (byte-identical, same object).
    await wf._emit(SSE_EVENT_SOURCES, sources_frame)
    result = await wf.run(query="giá căn 2PN", session_id="sess_sanitize_wire", history=[])

    token_frames = [d for e, d in events if e == SSE_EVENT_TOKEN]
    assert len(token_frames) >= 1
    assert any(frame["text"] for frame in token_frames), "clean prefix should stream through"
    for frame in events:
        assert "[chunk_hidden99]" not in str(frame[1].get("text", ""))
    # Original metadata payload object was passed through untouched.
    assert (SSE_EVENT_SOURCES, sources_frame) in events
    assert sources_frame == {"sources": [{"doc_id": "raw_internal_id", "title": "CSBH"}]}
    # Authoritative done answer is fully sanitized.
    assert "[chunk_hidden99]" not in result["answer"]
    assert result["answer"] == (
        "Xin chào anh/chị, dự án có chính sách bán hàng rất tốt trong tháng này. nhé [1]."
    )


@pytest.mark.asyncio
async def test_emit_wrapper_copies_token_frame_and_supports_async_callback():
    received = []

    async def collector(event, data):
        received.append((event, data))

    wf = RagRgreConvWorkflow(on_event=collector)
    original_frame = {"text": "hello [chunk_x] world"}
    outcome = wf._emit_sanitized_stream(SSE_EVENT_TOKEN, original_frame)
    assert inspect.isawaitable(outcome)
    await outcome
    event_name, payload = received[-1]
    assert event_name == SSE_EVENT_TOKEN
    assert payload is not original_frame  # caller-owned frame never mutated
    assert original_frame == {"text": "hello [chunk_x] world"}
    wf._streaming_sanitizer.reset()


@pytest.mark.asyncio
async def test_done_payload_sources_facts_deep_equal_pre_post():
    sources = [{"doc_id": "camellia_x", "title": "CSBH 2024", "section": None}]
    facts = [{"fe_id": "fact_1", "content": "chiết khấu 7%"}]
    wf = RagRgreConvWorkflow(on_event=lambda e, d: None)
    wf._inner = _TokenStreamInner(
        [],
        {
            "answer": "Chính sách [doc_camellia_cs_bh_2024] chiết khấu 7% [1].",
            "sources": sources,
            "facts": facts,
            "requires_review": False,
        },
    )
    result = await wf.run(
        query="chính sách bán hàng", session_id="sess_sanitize_payload", history=[]
    )
    assert result["answer"] == "Chính sách chiết khấu 7% [1]."
    assert result["sources"] == [{"doc_id": "camellia_x", "title": "CSBH 2024", "section": None}]
    assert result["facts"] == [{"fe_id": "fact_1", "content": "chiết khấu 7%"}]
