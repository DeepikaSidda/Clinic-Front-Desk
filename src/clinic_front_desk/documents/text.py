"""Extract and chunk uploaded clinic documents.

The doctor uploads whatever they already have — a practice information sheet, a
"visiting us" page saved as PDF, a plain-text list of holiday closures. This module
turns that into retrievable passages, and it is deliberately pure: no AWS, no
network, no store. That keeps the messy part (real-world PDFs) fully testable.

Two decisions worth stating:

**Pages are preserved.** A retrieved answer cites the page it came from, so the
doctor can check it against the source. An answer the doctor cannot audit is not
much better than a guess.

**Chunks overlap.** A clinic's opening hours and its holiday closures often sit in
adjacent sentences; a hard split between them means a question about one retrieves a
passage missing the other. The overlap is small but it stops that class of miss.

Scanned PDFs are **not** handled. A page of images yields no text, and this module
reports that as an extraction error rather than silently storing an empty document —
so the doctor is told the upload was unusable instead of wondering why the agent
never mentions it. OCR would be a separate, much larger piece of work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Target characters per chunk. Sized so a chunk is a coherent passage — a
#: paragraph or two — rather than a single sentence (too little context to answer
#: from) or a whole page (dilutes the embedding, so nothing matches strongly).
DEFAULT_CHUNK_CHARS = 900

#: Characters of the previous chunk repeated at the start of the next.
DEFAULT_OVERLAP_CHARS = 150

#: Chunks shorter than this are dropped: page furniture like a bare page number or
#: a stray header retrieves noisily and answers nothing.
MIN_CHUNK_CHARS = 40

#: Guard against a pathological upload producing thousands of chunks, each of which
#: would cost an embedding call.
MAX_CHUNKS = 400

#: Upload size ceiling. A clinic information sheet is a few pages; anything far
#: larger is a mistake and should be rejected before it is parsed.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class DocumentExtractionError(Exception):
    """The upload could not be turned into usable text."""


@dataclass(frozen=True)
class ExtractedPage:
    """Text recovered from one page (or one whole plain-text file)."""

    page: int | None
    text: str


@dataclass(frozen=True)
class ExtractedText:
    """The result of reading an upload."""

    pages: list[ExtractedPage]
    page_count: int | None

    @property
    def full_text(self) -> str:
        """All pages joined, for structured extraction over the whole document."""
        return "\n\n".join(page.text for page in self.pages if page.text.strip())


def _normalize(text: str) -> str:
    """Tidy extracted text without changing its meaning.

    PDF text extraction produces ragged output: words split across lines, runs of
    spaces from justified layout, form feeds. Left alone, these leak into chunks and
    degrade both embedding quality and how an answer reads aloud.
    """
    text = text.replace("\u00ad", "")  # soft hyphens
    text = text.replace("\f", "\n")
    # Re-join words hyphenated across a line break ("appoint-\nment").
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = _rejoin_wrapped_lines(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _rejoin_wrapped_lines(text: str) -> str:
    """Undo layout line wrapping while keeping headings on their own line.

    A single newline in extracted text is usually a layout artifact and should
    become a space. Blanket-collapsing them, though, glues a heading onto the
    paragraph beneath it — "Parking" and its body become one run of prose, the
    heading stops being a visible boundary, and :func:`chunk_text` can no longer
    tell where one topic ends and the next begins. That produced chunks spanning
    three unrelated topics, which is measurably unsearchable.

    So a heading line keeps its break, promoted to a paragraph break so it reads
    as a structural boundary downstream. Everything else is joined.

    A known limitation: a bare list of short items — a service menu one name per
    line — is textually indistinguishable from a run of headings, so each item
    becomes its own passage. The names are all still retrievable, but a question
    like "what ear services do you offer" matches one procedure rather than the
    list. Attempts to separate the two by position or line length each broke the
    prose case, which is the common one, so this is left alone deliberately: a
    document that writes its lists as prose ("We offer X, Y and Z") chunks better,
    and that is worth saying in the upload guidance rather than guessing here.
    """
    blocks: list[str] = []
    for block in re.split(r"\n{2,}", text):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        units: list[str] = []
        buffer = ""
        for line in lines:
            if _is_heading(line):
                if buffer:
                    units.append(buffer)
                    buffer = ""
                units.append(line)
                continue
            # Not a heading, so this line continues the paragraph above it.
            buffer = f"{buffer} {line}" if buffer else line
        if buffer:
            units.append(buffer)
        blocks.append("\n\n".join(units))
    return "\n\n".join(blocks)


def extract_text(data: bytes, *, filename: str = "", content_type: str = "") -> ExtractedText:
    """Read an upload into per-page text.

    Supports PDF (via ``pypdf``) and plain text / markdown. The format is decided by
    the PDF magic bytes first and the filename or content type second, because
    browsers report inconsistent content types for the same file.

    Raises:
        DocumentExtractionError: if the upload is empty, too large, an unsupported
            format, or yields no extractable text (a scanned PDF being the common
            case).
    """
    if not data:
        raise DocumentExtractionError("the uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentExtractionError(
            f"the file is {len(data) / 1_048_576:.1f} MB; the limit is "
            f"{MAX_UPLOAD_BYTES // 1_048_576} MB"
        )

    lowered = (filename or "").lower()
    is_pdf = data[:5] == b"%PDF-" or lowered.endswith(".pdf") or "pdf" in content_type

    if is_pdf:
        return _extract_pdf(data)
    if lowered.endswith((".txt", ".md", ".markdown")) or content_type.startswith("text/"):
        return _extract_plain(data)

    raise DocumentExtractionError(
        "unsupported file type; upload a PDF, .txt, or .md file"
    )


def _extract_pdf(data: bytes) -> ExtractedText:
    import io

    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise DocumentExtractionError(
            "PDF support needs pypdf; install the documents extra"
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # An empty-password decrypt covers the common "protected but not
            # actually locked" case; a real password is a hard stop.
            try:
                reader.decrypt("")
            except Exception as exc:
                raise DocumentExtractionError(
                    "the PDF is password protected; upload an unprotected copy"
                ) from exc
        raw_pages = [(index + 1, page.extract_text() or "") for index, page in enumerate(reader.pages)]
    except DocumentExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same to the user
        raise DocumentExtractionError(f"could not read the PDF: {exc}") from exc

    pages = [
        ExtractedPage(page=number, text=_normalize(text))
        for number, text in raw_pages
    ]
    if not any(page.text.strip() for page in pages):
        raise DocumentExtractionError(
            "no text could be read from this PDF. If it is a scan or a photo, the "
            "text is an image — save or export a text-based PDF and try again."
        )
    return ExtractedText(pages=pages, page_count=len(raw_pages))


def _extract_plain(data: bytes) -> ExtractedText:
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 decodes any byte string
        raise DocumentExtractionError("could not decode the file as text")

    normalized = _normalize(text)
    if not normalized:
        raise DocumentExtractionError("the file contains no readable text")
    return ExtractedText(pages=[ExtractedPage(page=None, text=normalized)], page_count=None)


#: A line no longer than this may be a section heading.
#:
#: Tuned against a real PDF. PDF extraction emits one line per line *drawn*, so a
#: paragraph arrives as a stack of ~50-80 character fragments, and a fragment that
#: happens to break after a word ("...Thanksgiving and the day after") carries no
#: terminal punctuation and looks exactly like a heading. At 80 characters that
#: split the holiday section into four chunks and separated the heading from the
#: dates beneath it.
#:
#: Real clinic headings are short — "Parking", "Holiday closures", "Cancellation
#: and late arrival" are all under 30 characters — while wrapped body lines run to
#: the layout width. 45 sits between the two. A longer genuine heading is simply
#: not treated as a boundary, which merges its section into the previous one:
#: worse retrieval, not wrong retrieval.
HEADING_MAX_CHARS = 45


def chunk_text(
    extracted: ExtractedText,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    max_chunks: int = MAX_CHUNKS,
) -> list[tuple[str, int | None]]:
    """Split extracted text into ``(text, page)`` passages, one topic each.

    Chunks break at **section headings**, not at a character count. This is the
    difference between a corpus that can be searched and one that cannot, and it
    was measured rather than assumed: packing paragraphs greedily up to
    ``chunk_chars`` merged "Accessibility", "Holiday closures" and "Cancellation
    policy" into one passage, whose embedding then sat in the average of three
    unrelated topics. A question about holidays scored no better against it than a
    question about the weather, so no relevance threshold could tell a real match
    from noise.

    One topic per chunk gives each embedding a single subject to be close to. The
    heading text stays in the chunk because words like "Parking" and "Holiday
    closures" are precisely the high-signal terms a caller's question shares.

    ``chunk_chars`` is therefore a **ceiling for an over-long section**, not a
    packing target. A section past it is split on sentence boundaries with the
    heading repeated on each part, so every part keeps its topic. Documents with no
    headings at all (a wall of prose) fall back to paragraph packing.

    Chunks never span pages, which keeps the page citation honest.
    """
    chunks: list[tuple[str, int | None]] = []

    for page in extracted.pages:
        if not page.text.strip():
            continue
        for body in _split_sections(page.text, chunk_chars, overlap_chars):
            if len(body) >= MIN_CHUNK_CHARS:
                chunks.append((body, page.page))
            if len(chunks) >= max_chunks:
                return chunks
    return chunks


def _is_heading(line: str) -> bool:
    """Whether ``line`` reads as a section heading rather than body text."""
    stripped = line.strip()
    if not stripped or len(stripped) > HEADING_MAX_CHARS:
        return False
    # Sentence-ending or mid-sentence punctuation means it is prose. A trailing
    # colon is allowed, since "Parking:" is a heading.
    if stripped.endswith((".", "!", "?", ",", ";")):
        return False
    # A comma inside the line means it is enumerating something, which headings do
    # not do but wrapped body text does constantly ("New Year's Day, Memorial Day,
    # Independence Day"). This is what separates a heading from a mid-paragraph
    # line break in extracted PDF text.
    if "," in stripped:
        return False
    if not re.search(r"[A-Za-z]", stripped):
        return False
    return len(stripped.split()) <= 8


def _paragraphs(text: str) -> list[str]:
    """Blank-line-separated paragraphs, splitting a leading heading line off.

    PDF extraction often loses the blank line after a heading, leaving
    ``"Parking\\nPatient parking is free..."`` as one paragraph. Splitting the
    heading out here is what lets it act as a boundary.
    """
    out: list[str] = []
    for block in re.split(r"\n{2,}", text):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        if len(lines) > 1 and _is_heading(lines[0]):
            out.append(lines[0].strip())
            rest = "\n".join(lines[1:]).strip()
            if rest:
                out.append(rest)
        else:
            out.append(block)
    return out


def _split_sections(text: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    """Group paragraphs into one-topic sections bounded by headings."""
    sections: list[list[str]] = []
    for paragraph in _paragraphs(text):
        starts_section = _is_heading(paragraph)
        if starts_section or not sections:
            sections.append([paragraph])
        else:
            sections[-1].append(paragraph)

    # A section that is only a heading (or a stray short line, e.g. one bullet of
    # a list) carries no answer on its own. Merge it forward into the next section
    # rather than emitting or dropping it — dropping would lose list items
    # entirely, which is how holiday dates tend to be written.
    merged: list[str] = []
    carry = ""
    for section in sections:
        body = "\n".join(section).strip()
        if carry:
            body = f"{carry}\n{body}"
            carry = ""
        if len(body) < MIN_CHUNK_CHARS:
            carry = body
            continue
        merged.append(body)
    if carry:
        if merged:
            merged[-1] = f"{merged[-1]}\n{carry}"
        else:
            merged.append(carry)

    out: list[str] = []
    for body in merged:
        if len(body) <= chunk_chars:
            out.append(body)
        else:
            out.extend(_split_long_section(body, chunk_chars, overlap_chars))
    return out


def _split_long_section(section: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    """Split an over-long section on sentence boundaries, repeating its heading.

    The heading is prepended to every part so a long "Parking" section split in
    two still has both halves matching a question about parking. Overlap is a
    whole trailing sentence rather than a character tail — a chunk that begins
    mid-word ("t. There is additional overflow...") embeds badly and reads even
    worse when spoken.
    """
    lines = section.split("\n")
    heading = lines[0].strip() if _is_heading(lines[0]) else ""
    body = "\n".join(lines[1:]).strip() if heading else section
    prefix = f"{heading}\n" if heading else ""
    budget = max(chunk_chars - len(prefix), MIN_CHUNK_CHARS)

    sentences = [s for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= budget or not current:
            current = candidate
            continue
        parts.append(current)
        tail = current.rsplit(". ", 1)[-1] if overlap_chars > 0 else ""
        carry = tail if 0 < len(tail) <= overlap_chars else ""
        current = f"{carry} {sentence}".strip() if carry else sentence
    if current:
        parts.append(current)

    # A single sentence longer than the budget (a run-on address block, say) is
    # hard-split so it cannot exceed the embedding input limit.
    sized: list[str] = []
    for part in parts:
        while len(part) > budget:
            sized.append(part[:budget])
            part = part[budget:]
        if part.strip():
            sized.append(part.strip())
    return [f"{prefix}{part}" for part in sized]


__all__ = [
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_OVERLAP_CHARS",
    "MIN_CHUNK_CHARS",
    "MAX_CHUNKS",
    "MAX_UPLOAD_BYTES",
    "DocumentExtractionError",
    "ExtractedPage",
    "ExtractedText",
    "extract_text",
    "chunk_text",
]
