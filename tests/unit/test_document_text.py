"""Unit tests for document extraction and chunking (``documents/text.py``).

The chunking tests are the important ones here. Chunk boundaries are not a
cosmetic detail: when sections were packed together into fixed-size windows, each
chunk's embedding sat in the average of several unrelated topics and *no* relevance
threshold could separate a real match from noise. These tests pin the properties
that made retrieval work — one topic per chunk, headings preserved as boundaries,
no chunk starting mid-word — so that regression cannot come back silently.
"""

from __future__ import annotations

import io

import pytest

from clinic_front_desk.documents.text import (
    DEFAULT_CHUNK_CHARS,
    MAX_UPLOAD_BYTES,
    MIN_CHUNK_CHARS,
    DocumentExtractionError,
    chunk_text,
    extract_text,
)

# A practice sheet in the shape clinics actually write them: short headed
# sections, each on one topic.
SHEET = """Springfield Hearing Clinic

Where to find us
We are on the third floor of the Maple Medical Building, 123 Main Street.
Suite 302 is at the end of the corridor.

Parking
Patient parking is free in the surface lot behind the building, off Elm Street.
Street parking on Main Street is metered and limited to one hour.

Holiday closures
We are closed on New Year's Day, Thanksgiving and the day after, and from
December 24th through January 1st.

Cancellation policy
We ask for at least 24 hours notice to cancel or move an appointment.
"""


def _texts(sheet: str = SHEET) -> list[str]:
    """Chunk ``sheet`` and return just the passage texts."""
    return [text for text, _ in chunk_text(extract_text(sheet.encode(), filename="s.txt"))]


# ---------------------------------------------------------------------------
# extract_text: format handling and rejections
# ---------------------------------------------------------------------------


def test_plain_text_is_extracted_as_a_single_pageless_document() -> None:
    extracted = extract_text(b"Hello clinic", filename="notes.txt")

    assert extracted.full_text == "Hello clinic"
    # Plain text has no pages, which must be distinguishable from "one page" so a
    # citation never claims a page number the format cannot have.
    assert extracted.page_count is None
    assert extracted.pages[0].page is None


def test_empty_upload_is_rejected() -> None:
    with pytest.raises(DocumentExtractionError):
        extract_text(b"", filename="empty.txt")


def test_whitespace_only_upload_is_rejected() -> None:
    with pytest.raises(DocumentExtractionError):
        extract_text(b"   \n\n\t  ", filename="blank.txt")


def test_oversized_upload_is_rejected_before_parsing() -> None:
    with pytest.raises(DocumentExtractionError) as excinfo:
        extract_text(b"x" * (MAX_UPLOAD_BYTES + 1), filename="huge.txt")

    assert "limit" in str(excinfo.value).lower()


def test_a_pdf_that_is_not_a_pdf_is_rejected_with_a_readable_message() -> None:
    with pytest.raises(DocumentExtractionError) as excinfo:
        extract_text(b"%PDF-1.4 truncated nonsense", filename="broken.pdf")

    # The doctor reads this, so it must not be a stack trace.
    assert "pdf" in str(excinfo.value).lower()


def test_unsupported_binary_format_is_rejected() -> None:
    with pytest.raises(DocumentExtractionError):
        extract_text(b"\x89PNG\r\n\x1a\n\x00\x00", filename="scan.png")


# ---------------------------------------------------------------------------
# Chunking: one topic per chunk
# ---------------------------------------------------------------------------


def test_each_heading_starts_a_new_chunk() -> None:
    texts = _texts()

    # Four headed sections plus the title, which merges into the first section
    # because a title alone carries no answer.
    assert len(texts) == 4
    assert texts[0].startswith("Springfield Hearing Clinic")
    assert "Where to find us" in texts[0]


def test_unrelated_topics_never_share_a_chunk() -> None:
    # The regression: packing to a character target merged Parking + Holiday
    # closures + Cancellation policy into one passage, and its embedding then
    # matched no question well.
    texts = _texts()
    parking = next(t for t in texts if t.startswith("Parking"))

    assert "Holiday closures" not in parking
    assert "Cancellation policy" not in parking


def test_every_section_is_retrievable_as_its_own_chunk() -> None:
    texts = _texts()
    heads = [t.split("\n")[0] for t in texts]

    assert "Parking" in heads
    assert "Holiday closures" in heads
    assert "Cancellation policy" in heads


def test_the_heading_stays_in_the_chunk_it_titles() -> None:
    # The heading words ("Parking") are the highest-signal terms a caller's
    # question shares, so dropping them would cost the match.
    parking = next(t for t in _texts() if "surface lot" in t)

    assert "Parking" in parking


def test_no_chunk_begins_mid_word() -> None:
    for text in _texts():
        assert text[0].isupper() or text[0].isdigit(), f"chunk starts oddly: {text[:40]!r}"


def test_layout_line_wrapping_inside_a_paragraph_is_rejoined() -> None:
    # A single newline inside a sentence is a layout artifact and must become a
    # space, or chunks carry ragged line breaks into the embedding and the answer.
    body = next(t for t in _texts() if "New Year" in t)

    assert "from\nDecember" not in body
    assert "from December 24th" in body


def test_a_wrapped_body_line_is_not_mistaken_for_a_heading() -> None:
    # PDF extraction emits one line per line *drawn*, so a paragraph arrives as a
    # stack of fragments. A fragment breaking after a word carries no terminal
    # punctuation and used to be read as a heading, which split the holiday
    # section into four chunks and separated the heading from the dates.
    sheet = (
        "Holiday closures\n"
        "The clinic is closed on New Year's Day, Memorial Day,\n"
        "Independence Day, Labor Day, Thanksgiving and the day after\n"
        "Thanksgiving, and from December 24th through January 1st\n"
        "inclusive.\n"
    )

    texts = _texts(sheet)

    assert len(texts) == 1
    assert texts[0].startswith("Holiday closures")
    assert "Memorial Day" in texts[0]
    assert "January 1st" in texts[0]


def test_an_enumerating_line_is_never_a_heading() -> None:
    # Commas mean the line is listing something, which body text does constantly
    # and headings do not.
    sheet = (
        "Insurance\n"
        "We accept Aetna, Blue Cross Blue Shield, Cigna and Medicare\n"
        "for most visits at this practice location.\n"
    )

    texts = _texts(sheet)

    assert len(texts) == 1
    assert "Cigna" in texts[0]


def test_a_document_with_no_headings_still_chunks() -> None:
    prose = "\n\n".join(f"This is paragraph number {i} of some running prose." * 6
                        for i in range(6))
    texts = _texts(prose)

    assert texts
    assert all(len(t) <= DEFAULT_CHUNK_CHARS for t in texts)


def test_an_over_long_section_is_split_and_repeats_its_heading() -> None:
    long_body = " ".join(f"Sentence number {i} about where to park." for i in range(120))
    sheet = f"Parking\n{long_body}"

    texts = _texts(sheet)

    assert len(texts) > 1
    # Every part keeps the topic, so a question about parking matches whichever
    # half holds the answer.
    assert all(t.startswith("Parking") for t in texts)
    assert all(len(t) <= DEFAULT_CHUNK_CHARS for t in texts)


def test_short_list_items_are_merged_rather_than_dropped() -> None:
    # Holiday dates are usually written as a short list. Each line is too short to
    # be a chunk on its own, and dropping them would lose the dates entirely.
    sheet = (
        "Holiday closures\n\n"
        "New Year's Day\n\n"
        "Thanksgiving\n\n"
        "Christmas Eve through New Year's Day inclusive\n\n"
        "The second Friday of every month for staff training\n"
    )
    texts = _texts(sheet)

    joined = " ".join(texts)
    assert "Thanksgiving" in joined
    assert "staff training" in joined


def test_chunks_never_span_pages_so_a_citation_stays_honest() -> None:
    from clinic_front_desk.documents.text import ExtractedPage, ExtractedText

    extracted = ExtractedText(
        pages=[
            ExtractedPage(
                page=1,
                text=(
                    "Parking\nPatient parking is free in the surface lot behind "
                    "the building, accessed from Elm Street."
                ),
            ),
            ExtractedPage(
                page=2,
                text=(
                    "Insurance\nWe accept Aetna, Blue Cross Blue Shield and "
                    "Medicare. Payment is due at the time of the visit."
                ),
            ),
        ],
        page_count=2,
    )

    chunks = chunk_text(extracted)

    assert {page for _, page in chunks} == {1, 2}
    for text, page in chunks:
        if page == 1:
            assert "Aetna" not in text


def test_chunks_shorter_than_the_minimum_are_not_emitted_alone() -> None:
    for text, _ in chunk_text(extract_text(SHEET.encode(), filename="s.txt")):
        assert len(text) >= MIN_CHUNK_CHARS


def test_max_chunks_bounds_a_pathological_document() -> None:
    sheet = "\n\n".join(
        f"Section {i}\nSome body text for section {i} that is long enough to keep."
        for i in range(500)
    )
    chunks = chunk_text(extract_text(sheet.encode(), filename="s.txt"), max_chunks=10)

    assert len(chunks) == 10


# ---------------------------------------------------------------------------
# PDF path (a genuine, minimal PDF built in-process)
# ---------------------------------------------------------------------------


def _pdf(lines_per_page: list[list[str]]) -> bytes:
    """Build a genuine multi-page text PDF with a correct cross-reference table.

    Written by hand rather than with a PDF library because none is a dependency,
    and PDF is the format the doctor will actually upload — mocking the reader
    would leave the one path that matters untested.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)  # object numbers are 1-based

    page_count = len(lines_per_page)
    # Reserve: 1 = catalog, 2 = pages tree, 3 = font, then per page a page object
    # and a content stream.
    catalog_num, pages_num, font_num = 1, 2, 3
    page_nums = [4 + i * 2 for i in range(page_count)]
    content_nums = [5 + i * 2 for i in range(page_count)]

    add(f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode())
    kids = " ".join(f"{n} 0 R" for n in page_nums)
    add(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode())
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for index, lines in enumerate(lines_per_page):
        add(
            (
                f"<< /Type /Page /Parent {pages_num} 0 R "
                f"/MediaBox [0 0 612 792] /Contents {content_nums[index]} 0 R "
                f"/Resources << /Font << /F1 {font_num} 0 R >> >> >>"
            ).encode()
        )
        drawn = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
        for line in lines:
            escaped = (
                line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            )
            drawn.append(f"({escaped}) Tj T*")
        drawn.append("ET")
        stream = "\n".join(drawn).encode("latin-1")
        add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)

    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog_num,
        xref_at,
    )
    return bytes(out)


def _one_page_pdf(text: str) -> bytes:
    return _pdf([[text]])


def test_a_real_pdf_is_read_and_reports_its_page_count() -> None:
    data = _one_page_pdf("Parking is free behind the building")

    extracted = extract_text(data, filename="info.pdf")

    assert "Parking is free behind the building" in extracted.full_text
    assert extracted.page_count == 1
    assert extracted.pages[0].page == 1


def test_a_pdf_is_detected_by_its_magic_bytes_not_its_filename() -> None:
    # Browsers report inconsistent content types, and doctors rename files. The
    # bytes are the only reliable signal.
    data = _one_page_pdf("Free parking behind the building")

    extracted = extract_text(data, filename="practice-info", content_type="")

    assert "Free parking behind the building" in extracted.full_text


def test_pdf_chunks_carry_the_page_they_came_from() -> None:
    data = _pdf(
        [
            ["Parking", "Patient parking is free in the lot off Elm Street."],
            ["Insurance", "We accept Aetna, Blue Cross Blue Shield and Medicare."],
        ]
    )

    chunks = chunk_text(extract_text(data, filename="info.pdf"))

    pages = {page for _, page in chunks}
    assert pages == {1, 2}
    parking = next(text for text, page in chunks if page == 1)
    assert "Elm Street" in parking
    assert "Aetna" not in parking


def test_a_pdf_with_no_extractable_text_is_rejected_as_a_scan() -> None:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)

    with pytest.raises(DocumentExtractionError) as excinfo:
        extract_text(buffer.getvalue(), filename="scanned.pdf")

    # The message has to tell the doctor what to do about it, since a scan is the
    # single most likely upload failure.
    message = str(excinfo.value).lower()
    assert "scan" in message or "image" in message
