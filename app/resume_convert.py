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
"""Deterministic, non-AI document -> markdown conversion for uploaded resumes.

Used by the "Base Resume" upload path (app/main.py:settings_asset_upload) so
a plain document upload can satisfy the Getting Started "Resume & documents"
step the same way the AI-driven resume interview does
(app/onboarding.py:save_resume_draft) -- without requiring any AI provider.

Intentionally best-effort: this exists so the resulting markdown can be
reviewed and hand-edited by the user on the Getting Started profile step, not
to be a pixel-perfect round-trip converter. docx gets real structure
(headings, bold/italic, lists, simple tables) via python-docx; pdf gets plain
extracted text only (PDFs don't expose reusable structure); txt/md pass
through unchanged.
"""

from __future__ import annotations

import io
import logging
import re

log = logging.getLogger(__name__)

# .pdf is handled separately (best-effort text extraction, no structure) but
# is still a "supported" upload type for conversion purposes.
SUPPORTED_EXTENSIONS = ("docx", "pdf", "txt", "md")

_HEADING_RE = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)


class ResumeConversionError(Exception):
    """Raised when a document can't be converted to usable markdown text."""


def convert_to_markdown(data: bytes, ext: str) -> str:
    """Best-effort conversion of an uploaded document's bytes to markdown.

    Supports docx (via python-docx, mapping headings/bold/italic/lists/simple
    tables), pdf (via pypdf, plain text only), and txt/md (passthrough).
    Anything else -- or a file that fails to parse, or one with no
    extractable text -- raises ResumeConversionError so the caller can fall
    back to the manual paste-back / AI interview paths instead.
    """
    ext = (ext or "").lower().lstrip(".")
    if ext not in SUPPORTED_EXTENSIONS:
        raise ResumeConversionError(
            f"Automatic conversion isn't supported for .{ext or '?'} files yet. "
            "Use the resume interview below, or open the file and paste its "
            "text into the markdown box.")

    try:
        if ext == "docx":
            markdown = _docx_to_markdown(data)
        elif ext == "pdf":
            markdown = _pdf_to_markdown(data)
        else:
            markdown = _text_to_markdown(data)
    except ResumeConversionError:
        raise
    except Exception as exc:  # noqa: BLE001 - any parser failure becomes our error type
        log.warning("resume_convert: .%s conversion failed: %s", ext, exc)
        raise ResumeConversionError(
            f"Could not read this .{ext} file (it may be corrupted or password-protected)."
        ) from exc

    markdown = markdown.strip()
    if not markdown:
        raise ResumeConversionError("That file didn't contain any extractable text.")
    return markdown


def _text_to_markdown(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ResumeConversionError("Could not decode this file as text.")


def _run_markdown(run) -> str:
    """A single docx run rendered as markdown, preserving leading/trailing
    whitespace outside the emphasis markers (CommonMark won't treat
    space-adjacent ** or * as emphasis, so wrapping the raw run would
    silently fail to render)."""
    text = run.text or ""
    if not text.strip():
        return text
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    core = text.strip()
    if run.bold and run.italic:
        core = f"***{core}***"
    elif run.bold:
        core = f"**{core}**"
    elif run.italic:
        core = f"*{core}*"
    return f"{lead}{core}{trail}"


def _docx_to_markdown(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    blocks: list[str] = []
    prev_is_list = False

    for block in doc.iter_inner_content():
        if type(block).__name__ == "Table":
            table_md = _table_to_markdown(block)
            if table_md:
                blocks.append(table_md)
            prev_is_list = False
            continue

        style_name = (block.style.name if block.style else "") or ""
        style_lower = style_name.lower()
        text = "".join(_run_markdown(r) for r in block.runs).strip()
        if not text:
            # Some paragraphs (e.g. fields) carry visible text outside runs.
            text = (block.text or "").strip()
        if not text:
            continue

        heading_match = _HEADING_RE.match(style_name)
        is_list = "list" in style_lower or "bullet" in style_lower

        if style_lower == "title" or heading_match:
            level = min(max(int(heading_match.group(1)), 1), 6) if heading_match else 1
            blocks.append(("#" * level) + " " + text)
            prev_is_list = False
        elif is_list:
            marker = "1." if "number" in style_lower else "-"
            if prev_is_list and blocks:
                blocks[-1] = blocks[-1] + f"\n{marker} {text}"
            else:
                blocks.append(f"{marker} {text}")
            prev_is_list = True
        else:
            blocks.append(text)
            prev_is_list = False

    return "\n\n".join(blocks)


def _table_to_markdown(table) -> str:
    rows = [
        [cell.text.strip().replace("\n", " ").replace("|", "\\|") for cell in row.cells]
        for row in table.rows
    ]
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return ""
    header, *body = rows
    out = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    out.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(out)


def _pdf_to_markdown(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise ResumeConversionError("Could not read this PDF -- it may be corrupted.") from exc
    if reader.is_encrypted:
        raise ResumeConversionError(
            "This PDF is password-protected -- remove the password and re-upload.")
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    pages = [p for p in pages if p]
    return "\n\n".join(pages)
