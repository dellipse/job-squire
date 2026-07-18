# Copyright (C) 2026 D. Brandmeyer
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Auto PDF export and ATS cleaning for saved application kits.

Two jobs run whenever a kit is saved (via the MCP save_kit tool or an API build):

1. ATS cleaning — replace the Unicode punctuation that Applicant Tracking System
   scanners choke on (em/en dashes, smart quotes, fancy bullets, exotic spaces,
   zero-width characters, (TM)/(R)/(c) symbols) with plain ASCII equivalents.
   Accented letters are left untouched.

2. PDF export — split the kit into its Tailored Resume and Cover Letter sections,
   flatten each to plain text, and render a dependency-free PDF that gets attached
   to the job record. This runs alongside the existing .docx kit attachment (see
   app/ai.py _save_kit_docx_attachment); the two do not replace each other.

The PDF writer here uses only the standard library — it emits a minimal PDF-1.4
file with the base-14 Courier font and WinAnsi encoding, so no third-party
package (reportlab, weasyprint, etc.) is required.
"""
import logging
import os
import re
import textwrap
import uuid

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# a) ATS cleaning
# ---------------------------------------------------------------------------

# Maps Unicode ordinals to plain ASCII. str.translate() applies it: an int value
# would map to a codepoint, a str replaces the char, and None deletes it. Because
# every key is a non-ASCII character and every replacement is ASCII, ats_clean is
# idempotent (a second pass finds nothing left to translate) and never touches
# accented letters like é, ñ, ü, which are absent from the table.
_ATS_TRANSLATION = {
    # Dashes and dash-like glyphs -> hyphen-minus
    0x2010: "-",  # hyphen
    0x2011: "-",  # non-breaking hyphen
    0x2012: "-",  # figure dash
    0x2013: "-",  # en dash
    0x2014: "-",  # em dash
    0x2015: "-",  # horizontal bar
    0x2212: "-",  # minus sign
    # Single quotes / apostrophes -> '
    0x2018: "'",  # left single quotation mark
    0x2019: "'",  # right single quotation mark
    0x201A: "'",  # single low-9 quotation mark
    0x201B: "'",  # single high-reversed-9 quotation mark
    0x2032: "'",  # prime
    0x02BC: "'",  # modifier letter apostrophe
    # Double quotes -> "
    0x201C: '"',  # left double quotation mark
    0x201D: '"',  # right double quotation mark
    0x201E: '"',  # double low-9 quotation mark
    0x201F: '"',  # double high-reversed-9 quotation mark
    0x2033: '"',  # double prime
    0x00AB: '"',  # left-pointing double angle quotation mark
    0x00BB: '"',  # right-pointing double angle quotation mark
    # Bullet glyphs -> hyphen
    0x2022: "-",  # bullet
    0x2023: "-",  # triangular bullet
    0x2043: "-",  # hyphen bullet
    0x2219: "-",  # bullet operator
    0x25AA: "-",  # black small square
    0x25AB: "-",  # white small square
    0x25CF: "-",  # black circle
    0x25CB: "-",  # white circle
    0x25E6: "-",  # white bullet
    0x2027: "-",  # hyphenation point
    0x00B7: "-",  # middle dot
    # Ellipsis
    0x2026: "...",
    # Non-breaking and exotic spaces -> regular space
    0x00A0: " ",  # no-break space
    0x1680: " ",  # ogham space mark
    0x2000: " ", 0x2001: " ", 0x2002: " ", 0x2003: " ", 0x2004: " ",
    0x2005: " ", 0x2006: " ", 0x2007: " ", 0x2008: " ", 0x2009: " ",
    0x200A: " ",
    0x202F: " ",  # narrow no-break space
    0x205F: " ",  # medium mathematical space
    0x3000: " ",  # ideographic space
    # Zero-width and invisible characters -> removed
    0x200B: "",   # zero-width space
    0x200C: "",   # zero-width non-joiner
    0x200D: "",   # zero-width joiner
    0x2060: "",   # word joiner
    0xFEFF: "",   # zero-width no-break space / BOM
    0x00AD: "",   # soft hyphen
    # Common symbols -> ASCII forms
    0x2122: "(TM)",
    0x00AE: "(R)",
    0x00A9: "(c)",
}


def ats_clean(text: str) -> str:
    """Return text with ATS-hostile Unicode replaced by ASCII. Idempotent."""
    return (text or "").translate(_ATS_TRANSLATION)


# ---------------------------------------------------------------------------
# b) Section extraction
# ---------------------------------------------------------------------------

_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")
_BOLD_HEADING_RE = re.compile(r"^\s*\*\*(.+?)\*\*:?\s*$")
_NUM_CAPS_HEADING_RE = re.compile(r"^\s*\d+[.)]\s+([A-Z0-9][A-Z0-9 &/'()\-]+)\s*$")

# Canonical (uppercased, number-prefix stripped) prefixes of the kit's top-level
# section titles. A heading only ends the current section when it names one of
# these — so bold sub-labels inside a resume ("**Summary**", "## Experience")
# do not prematurely cut the section short.
_SECTION_BOUNDARY_PREFIXES = (
    "FIT ASSESSMENT",
    "ATS KEYWORD",
    "RESEARCH NOTES",
    "TAILORED RESUME",
    "RESUME",
    "COVER LETTER",
    "APPLICATION EMAIL",
    "FOLLOW-UP",
    "FOLLOW UP",
    "FOLLOWUP",
    "ANTICIPATED INTERVIEW",
    "INTERVIEW QUESTION",
)


def _heading_text(line: str):
    """Return the plain title if line is a heading (any of the 3 styles), else None."""
    m = _MD_HEADING_RE.match(line)
    if m:
        return m.group(1).strip()
    m = _BOLD_HEADING_RE.match(line)
    if m:
        return m.group(1).strip()
    m = _NUM_CAPS_HEADING_RE.match(line)
    if m:
        return m.group(1).strip()
    return None


def _canonical(title: str) -> str:
    """Normalize a heading title for matching: drop markers/numbering, uppercase."""
    t = (title or "").replace("*", "").strip()
    t = re.sub(r"^\d+[.)]\s*", "", t)          # leading "1. " / "2) "
    t = re.sub(r"[:.\s]+$", "", t)             # trailing punctuation
    t = re.sub(r"\s+", " ", t)
    return t.upper()


def _is_boundary(canon: str) -> bool:
    return any(canon.startswith(p) for p in _SECTION_BOUNDARY_PREFIXES)


def _target_of(canon: str):
    """Map a boundary heading to a section key, or None if it's some other section."""
    if canon.startswith("TAILORED RESUME") or canon.startswith("RESUME"):
        return "resume"
    if canon.startswith("COVER LETTER"):
        return "cover_letter"
    return None


def _strip_blank_edges(lines: list) -> str:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def extract_sections(kit_markdown: str) -> dict:
    """Split kit markdown into its resume and cover-letter bodies.

    Handles all three heading styles Claude uses for the section titles:
    Markdown ("## Tailored Resume"), bold ("**Cover Letter**"), and numbered
    all-caps ("1. TAILORED RESUME"). Each section body runs from its title up to
    the next recognized top-level section heading, with blank edges trimmed.

    Returns {"resume": str|None, "cover_letter": str|None}.
    """
    sections = {"resume": None, "cover_letter": None}
    current = None
    buffer: list = []

    def _flush():
        if current and buffer and sections[current] is None:
            body = _strip_blank_edges(buffer)
            if body:
                sections[current] = body

    for line in (kit_markdown or "").splitlines():
        title = _heading_text(line)
        if title is not None:
            canon = _canonical(title)
            if _is_boundary(canon):
                _flush()
                buffer = []
                current = _target_of(canon)
                continue
        if current:
            buffer.append(line)
    _flush()
    return sections


# ---------------------------------------------------------------------------
# c) Markdown -> plain text
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_UNDERSCORE_BOLD_RE = re.compile(r"__(.+?)__")
_ITALIC_RE = re.compile(r"\*(.+?)\*")
_TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")
_HEADING_MARKER_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_BULLET_RE = re.compile(r"^(\s*)[*+•]\s+")


def _strip_markdown(text: str) -> str:
    """Flatten the markdown subset our kits use to plain text for PDF bodies."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        # Drop table separator rows like |---|:--:|---|
        if _TABLE_SEP_RE.match(stripped):
            continue

        # Flatten genuine table rows (start AND end with a pipe) to space-joined
        # cells. Inline pipes in a normal line (e.g. "Name | City | email") are
        # left alone because such a line does not start with "|".
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            line = "  ".join(c for c in cells if c)
        else:
            line = _HEADING_MARKER_RE.sub("", line)      # drop leading #'s
            line = _BULLET_RE.sub(r"\1- ", line)         # *, +, • bullets -> "- "

        out.append(line)

    text = "\n".join(out)
    text = _LINK_RE.sub(r"\1 (\2)", text)                # [text](url) -> text (url)
    text = _BOLD_RE.sub(r"\1", text)                     # **bold**
    text = _UNDERSCORE_BOLD_RE.sub(r"\1", text)          # __bold__
    text = _ITALIC_RE.sub(r"\1", text)                   # *italic*
    text = text.replace("`", "")                         # inline code backticks
    text = re.sub(r"\n{3,}", "\n\n", text)               # 3+ blank lines -> one
    return text.strip("\n")


# ---------------------------------------------------------------------------
# d) Dependency-free PDF writer (base-14 Courier, WinAnsi encoding)
# ---------------------------------------------------------------------------

_PAGE_W, _PAGE_H = 612.0, 792.0       # US Letter, points
_MARGIN = 0.75 * 72.0                  # 0.75 inch
_BODY_SIZE = 10.0
_TITLE_SIZE = 13.0
_COURIER_ADVANCE = 0.6                 # Courier glyph width = 0.6 em
_BODY_LEADING = _BODY_SIZE * 1.2


def _pdf_escape(s: str) -> bytes:
    """Encode text as WinAnsi (cp1252) and escape the PDF string delimiters."""
    data = (s or "").encode("cp1252", "replace")
    out = bytearray()
    for byte in data:
        if byte in (0x5C, 0x28, 0x29):   # backslash, ( , )
            out.append(0x5C)
        out.append(byte)
    return bytes(out)


def _wrap_text(body: str, cols: int) -> list:
    """Word-wrap each paragraph to `cols` monospace columns, keeping blank lines."""
    lines = []
    for para in (body or "").split("\n"):
        if not para.strip():
            lines.append("")
            continue
        wrapped = textwrap.wrap(
            para, width=cols, break_long_words=True, break_on_hyphens=False
        )
        lines.extend(wrapped or [""])
    return lines


def _content_stream(page_lines: list) -> bytes:
    parts = []
    for font, size, x, y, esc in page_lines:
        parts.append(("BT /%s %.2f Tf %.2f %.2f Td (" % (font, size, x, y)).encode("ascii"))
        parts.append(esc)
        parts.append(b") Tj ET\n")
    return b"".join(parts)


def _assemble_pdf(pages: list) -> bytes:
    """Serialize laid-out pages to PDF-1.4 bytes with a valid xref table."""
    objs = {}
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = b" ".join(b"%d 0 R" % (5 + 2 * i) for i in range(len(pages)))
    objs[2] = b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(pages)
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"
    objs[4] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding >>"
    for i, page_lines in enumerate(pages):
        page_obj = 5 + 2 * i
        content_obj = 6 + 2 * i
        stream = _content_stream(page_lines)
        objs[page_obj] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            b"/Contents %d 0 R >>" % content_obj
        )
        objs[content_obj] = (
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    total = len(objs)
    for num in range(1, total + 1):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    xref_start = len(out)
    out += b"xref\n0 %d\n" % (total + 1)
    out += b"0000000000 65535 f \n"
    for num in range(1, total + 1):
        out += ("%010d 00000 n \n" % offsets[num]).encode("ascii")
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        total + 1, xref_start
    )
    return bytes(out)


def render_pdf(title: str, body: str) -> bytes:
    """Render a title + body to PDF bytes using base-14 Courier. Auto-paginates.

    The title is bold Courier at 13pt; the body is regular Courier at 10pt,
    word-wrapped to the US Letter text width with 0.75in margins. Text is encoded
    as cp1252 (WinAnsi) so accented characters survive. Returns bytes beginning
    with "%PDF-1.4".
    """
    usable_w = _PAGE_W - 2 * _MARGIN
    body_cols = max(1, int(usable_w // (_BODY_SIZE * _COURIER_ADVANCE)))
    title_cols = max(1, int(usable_w // (_TITLE_SIZE * _COURIER_ADVANCE)))
    top = _PAGE_H - _MARGIN
    bottom = _MARGIN

    title = ats_clean(title or "")
    if len(title) > title_cols:
        title = title[: max(1, title_cols - 3)] + "..."

    wrapped = _wrap_text(body, body_cols)

    pages: list = []
    page: list = []
    y = top - _TITLE_SIZE
    page.append(("F2", _TITLE_SIZE, _MARGIN, y, _pdf_escape(title)))
    y -= _BODY_LEADING + 6.0                       # blank gap under the title
    for line in wrapped:
        if y < bottom:                             # start a new page
            pages.append(page)
            page = []
            y = top - _BODY_SIZE
        page.append(("F1", _BODY_SIZE, _MARGIN, y, _pdf_escape(line)))
        y -= _BODY_LEADING
    pages.append(page)

    return _assemble_pdf(pages)


# ---------------------------------------------------------------------------
# e) Main entry point
# ---------------------------------------------------------------------------

# Marks the PDFs this module creates so a re-save can refresh them without
# touching the .docx kit attachment (uploaded_by "AI (...)") or any real user
# upload. Stored in Attachment.uploaded_by.
_KIT_PDF_UPLOADER = "Application Kit"


def _slug(text: str) -> str:
    """ASCII, hyphen-separated slug for filenames: spaces -> '-', specials stripped."""
    text = ats_clean(text or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


def _original_name(job, kind: str) -> str:
    parts = [_slug(job.company), _slug(job.title), kind.replace(" ", "-")]
    stem = "-".join(p for p in parts if p)[:120] or "application-kit"
    return f"{stem}.pdf"


def sync_kit_attachments(job) -> list:
    """Extract Resume/Cover Letter sections from job.kit_output, ATS-clean them,
    render each to a PDF, and attach it to the job record.

    Existing PDFs this module created for the job (uploaded_by "Application Kit",
    kind Resume/Cover Letter) are removed first, so re-saving a kit refreshes the
    attachments instead of duplicating them. Attachments uploaded by a real user
    are never touched. All exceptions are logged and swallowed — a kit save must
    never fail because of a PDF error.

    Returns the list of kinds created, e.g. ["Resume", "Cover Letter"].
    """
    from flask import current_app

    from .db_utils import commit
    from .extensions import db
    from .models import Attachment

    try:
        sections = extract_sections(job.kit_output or "")
        wanted = []
        if sections.get("resume"):
            wanted.append(("Resume", sections["resume"]))
        if sections.get("cover_letter"):
            wanted.append(("Cover Letter", sections["cover_letter"]))
        if not wanted:
            return []

        upload_dir = current_app.config["UPLOAD_DIR"]

        # Refresh: drop previously auto-generated kit PDFs (and their files).
        for old in list(job.attachments):
            if old.uploaded_by == _KIT_PDF_UPLOADER and old.kind in ("Resume", "Cover Letter"):
                old_path = os.path.join(upload_dir, old.stored_name)
                try:
                    if os.path.exists(old_path):
                        os.remove(old_path)
                except OSError:
                    log.warning("could not remove old kit PDF %s", old_path)
                db.session.delete(old)

        created = []
        for kind, section_body in wanted:
            body = ats_clean(_strip_markdown(section_body))
            pdf_title = ats_clean(f"{kind} - {job.title} at {job.company}")
            pdf_bytes = render_pdf(pdf_title, body)

            stored_name = f"{uuid.uuid4().hex}.pdf"
            dest = os.path.join(upload_dir, stored_name)
            with open(dest, "wb") as fh:
                fh.write(pdf_bytes)

            db.session.add(Attachment(
                job_id=job.id,
                kind=kind,
                original_name=_original_name(job, kind),
                stored_name=stored_name,
                content_type="application/pdf",
                size=len(pdf_bytes),
                uploaded_by=_KIT_PDF_UPLOADER,
            ))
            created.append(kind)

        commit()
        log.info("kit PDFs synced for job %d: %s", job.id, ", ".join(created))
        return created
    except Exception:  # noqa: BLE001 — kit saves must never fail on a PDF error
        log.exception("sync_kit_attachments failed for job %s", getattr(job, "id", "?"))
        try:
            from .extensions import db as _db
            _db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []
