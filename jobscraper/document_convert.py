"""Convert PDF files to DOCX and DOCX files to PDF."""

from __future__ import annotations

import io
import re
from pathlib import Path

from jobscraper.resume_parser import MAX_UPLOAD_BYTES

PDF_TYPE = "application/pdf"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_WINDOWS_FONTS = (
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path(r"C:\Windows\Fonts\calibri.ttf"),
    Path(r"C:\Windows\Fonts\segoeui.ttf"),
)
_LINUX_FONTS = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
)

SECTION_HEADINGS = {
    "education",
    "experience",
    "work experience",
    "professional experience",
    "employment",
    "work history",
    "skills",
    "technical skills",
    "core skills",
    "projects",
    "summary",
    "profile",
    "professional summary",
    "objective",
    "career objective",
    "certifications",
    "certificates",
    "languages",
    "contact",
    "achievements",
    "key achievements",
    "core competencies",
    "competencies",
    "professional summary",
    "professional experience",
    "awards",
    "interests",
    "publications",
    "about me",
    "declaration",
}


class ConvertError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def pdf_to_docx(data: bytes, filename: str = "") -> bytes:
    _validate_upload(data, filename, expected="pdf")
    from pypdf import PdfReader
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ConvertError("Could not read PDF. The file may be corrupt.", 422) from exc

    if not reader.pages:
        raise ConvertError("PDF has no pages.", 422)

    document = Document()
    section = document.sections[0]
    section.top_margin = Inches(0.7)
    section.bottom_margin = Inches(0.7)
    section.left_margin = Inches(0.8)
    section.right_margin = Inches(0.8)
    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    style.font.color.rgb = RGBColor(15, 23, 42)

    extracted = False
    first = True
    for index, page in enumerate(reader.pages):
        text = _page_text(page)
        lines = _split_lines(text)
        if index and lines:
            document.add_page_break()
        if not lines:
            continue
        extracted = True
        for line in lines:
            kind = "h1" if first else _heading_kind(line)
            first = False
            _add_docx_block(document, line, kind)

    if not extracted:
        raise ConvertError(
            "No extractable text in PDF. Image-only / scanned PDFs are not supported (no OCR).",
            422,
        )
    return _save_docx(document)


def docx_to_pdf(data: bytes, filename: str = "") -> bytes:
    _validate_upload(data, filename, expected="docx")
    from docx import Document

    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:
        raise ConvertError("Could not read DOCX. The file may be corrupt.", 422) from exc

    blocks = _blocks_from_docx(document)
    if not any(text for text, kind in blocks if kind != "blank"):
        raise ConvertError("DOCX has no extractable text.", 422)

    if blocks and blocks[0][1] == "p":
        blocks[0] = (blocks[0][0], "h1")

    try:
        return _render_pdf(blocks)
    except ConvertError:
        raise
    except Exception as exc:
        try:
            return _fallback_pdf(blocks)
        except Exception:
            raise ConvertError(f"Could not convert DOCX to PDF: {exc}", 422) from exc


def _page_text(page) -> str:
    try:
        text = page.extract_text(extraction_mode="layout") or ""
    except Exception:
        text = ""
    if len(text.strip()) < 8:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
    return text


def _add_docx_block(document, text: str, kind: str) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    navy = RGBColor(0x1B, 0x3A, 0x6B)
    blue = RGBColor(0x2E, 0x75, 0xB6)
    ink = RGBColor(0x33, 0x33, 0x33)
    gray = RGBColor(0x55, 0x55, 0x55)
    if kind == "h1":
        paragraph = _styled_paragraph(document, "Resume Name", text, 26, navy, bold=True)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _space(paragraph, before=0, after=2)
        return
    if kind == "subtitle":
        paragraph = _styled_paragraph(document, "Resume Headline", text, 12, blue, italic=True)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _space(paragraph, before=0, after=2)
        return
    if kind == "contact":
        paragraph = _styled_paragraph(document, "Resume Contact", text, 10, gray)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _space(paragraph, before=0, after=10)
        return
    if kind == "h2":
        paragraph = _styled_paragraph(document, "Resume Section", text.rstrip(":"), 11, navy, bold=True)
        _space(paragraph, before=12, after=2)
        _bottom_border(paragraph, "2E75B6")
        return
    if kind == "role":
        paragraph = document.add_paragraph(style=_style(document, "Resume Role"))
        for value, bold, color in _docx_role_runs(text, blue, gray):
            _add_run(paragraph, value, 10, color, bold=bold)
        _space(paragraph, before=8, after=1)
        return
    if kind == "blurb":
        paragraph = _styled_paragraph(document, "Resume Blurb", text, 10, gray, italic=True)
        _space(paragraph, before=0, after=2)
        return
    if kind == "date":
        paragraph = _styled_paragraph(document, "Resume Date", text, 10, gray)
        _space(paragraph, before=0, after=2)
        return
    if kind == "skill":
        paragraph = document.add_paragraph(style=_style(document, "Resume Skill"))
        split = _label_split(text)
        if split:
            _add_run(paragraph, split[0] + ":  ", 10, navy, bold=True)
            _add_run(paragraph, split[1], 10, ink)
        else:
            _add_run(paragraph, text, 10, ink)
        _space(paragraph, before=1, after=1)
        return
    if kind == "edu":
        paragraph = document.add_paragraph(style=_style(document, "Resume Education"))
        if "|" in text:
            degree, school = [part.strip() for part in text.split("|", 1)]
            _add_run(paragraph, degree + "   |   ", 11, navy, bold=True)
            _add_run(paragraph, school, 11, ink)
        else:
            _add_run(paragraph, text, 11, navy, bold=True)
        _space(paragraph, before=2, after=2)
        return
    if kind in {"li", "label-li"}:
        paragraph = document.add_paragraph(style=_style(document, "Resume Bullet"))
        paragraph.paragraph_format.left_indent = Pt(12)
        body = _strip_bullet(text)
        _add_run(paragraph, "•   ", 10, ink)
        split = _label_split(body) if kind == "label-li" else None
        if split:
            _add_run(paragraph, split[0] + ":  ", 10, ink, bold=True)
            _add_run(paragraph, split[1], 10, ink)
        else:
            _add_run(paragraph, body, 10, ink)
        _space(paragraph, before=1, after=1)
        return
    paragraph = _styled_paragraph(document, "Normal", text, 10, ink)
    _space(paragraph, before=0, after=2)


def _styled_paragraph(document, style_name: str, text: str, size: int, color, bold: bool = False, italic: bool = False):
    paragraph = document.add_paragraph(text, style=_style(document, style_name) if style_name != "Normal" else None)
    _run_font(paragraph, size, color, bold=bold, italic=italic)
    return paragraph


def _add_run(paragraph, text: str, size: int, color, bold: bool = False, italic: bool = False) -> None:
    from docx.shared import Pt

    run = paragraph.add_run(text)
    run.font.name = "Calibri"
    run.font.size = Pt(size)
    run.font.color.rgb = color
    run.bold = bold
    run.italic = italic


def _docx_role_runs(text: str, blue, gray) -> list[tuple[str, bool, object]]:
    pieces = _role_pieces(text, (0, 0, 0), (0, 0, 0))
    runs = []
    for value, style, _color in pieces:
        runs.append((value, style == "B", blue if style == "B" else gray))
    return runs


def _style(document, name: str):
    from docx.enum.style import WD_STYLE_TYPE

    try:
        return document.styles[name]
    except KeyError:
        style = document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = document.styles["Normal"]
        return style


def _run_font(paragraph, size: int, color, bold: bool = False, italic: bool = False) -> None:
    from docx.shared import Pt

    for run in paragraph.runs:
        run.font.name = "Calibri"
        run.font.size = Pt(size)
        run.font.color.rgb = color
        run.bold = bold
        run.italic = italic


def _space(paragraph, before: int, after: int) -> None:
    from docx.shared import Pt

    paragraph.paragraph_format.space_before = Pt(before)
    paragraph.paragraph_format.space_after = Pt(after)


def _bottom_border(paragraph, color: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    border = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "8")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color)
    border.append(bottom)
    paragraph._p.get_or_add_pPr().append(border)


def _blocks_from_docx(document) -> list[tuple[str, str]]:
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    blocks: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add_text(text: str, style_name: str = "") -> None:
        text = (text or "").strip()
        if not text:
            if blocks and blocks[-1][1] != "blank":
                blocks.append(("", "blank"))
            return
        key = re.sub(r"\s+", " ", text).lower()
        if key in seen:
            return
        seen.add(key)
        kind = "li" if _strip_bullet(text) != text else _heading_kind(text, style_name)
        blocks.append((text, kind))

    def add_paragraph(paragraph) -> None:
        try:
            style_name = (paragraph.style.name or "").lower() if paragraph.style else ""
        except Exception:
            style_name = ""
        add_text(paragraph.text or "", style_name)

    def add_table(table) -> None:
        try:
            rows = table.rows
        except Exception:
            return
        for row in rows:
            for cell in _unique_cells(row):
                for paragraph in cell.paragraphs:
                    add_paragraph(paragraph)
                try:
                    nested = cell.tables
                except Exception:
                    nested = []
                for nested_table in nested:
                    add_table(nested_table)

    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            add_paragraph(Paragraph(child, document))
        elif tag == "tbl":
            add_table(Table(child, document))

    for txbx in document.element.iter(qn("w:txbxContent")):
        for para in txbx.iter(qn("w:p")):
            text = "".join(node.text or "" for node in para.iter(qn("w:t"))).strip()
            add_text(text)

    for section in document.sections:
        for part in (section.header, section.footer):
            try:
                for paragraph in part.paragraphs:
                    add_paragraph(paragraph)
            except Exception:
                continue

    return blocks


def _unique_cells(row):
    seen = []
    for cell in row.cells:
        ident = id(cell._tc)
        if ident in seen:
            continue
        seen.append(ident)
        yield cell


def _heading_kind(text: str, style_name: str = "") -> str:
    style_name = (style_name or "").lower()
    if "resume headline" in style_name or style_name == "subtitle":
        return "subtitle"
    if "resume contact" in style_name:
        return "contact"
    compact = re.sub(r"[\s|:.\-]+$", "", text or "").strip()
    if compact.lower() in SECTION_HEADINGS:
        return "h2"
    words = [word for word in re.split(r"\s+", compact) if word]
    letters = re.sub(r"[^A-Za-z ]", "", compact)
    if letters.isupper() and len(words) >= 2 and 3 <= len(letters) <= 40:
        return "h2"
    if _strip_bullet(text) != (text or "").strip():
        return "li"
    if "resume name" in style_name or style_name == "title" or "heading 1" in style_name:
        return "h1"
    if "heading" in style_name:
        return "h2"
    return "p"


def _strip_bullet(text: str) -> str:
    return re.sub(r"^[\-•●○▪◦\*]+\s*", "", (text or "").strip())


def cv_text_to_docx(text: str) -> bytes:
    """Turn plain CV text into a Word file. The first line is the name."""
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor

    blocks = _cv_blocks(text)
    if not blocks:
        raise ConvertError("Resume has no text to download.")

    document = Document()
    section = document.sections[0]
    section.top_margin = Inches(0.65)
    section.bottom_margin = Inches(0.65)
    section.left_margin = Inches(0.75)
    section.right_margin = Inches(0.75)
    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    style.font.color.rgb = RGBColor(30, 41, 59)

    for line, kind in blocks:
        _add_docx_block(document, line, kind)
    return _save_docx(document)


def cv_text_to_pdf(text: str) -> bytes:
    """Turn plain CV text into a PDF download."""
    blocks = _cv_blocks(text)
    if not blocks:
        raise ConvertError("Resume has no text to download.")
    try:
        return _render_pdf(blocks)
    except Exception as exc:
        raise ConvertError(f"Could not build the resume PDF: {exc}", 422) from exc


def _cv_blocks(text: str) -> list[tuple[str, str]]:
    """Name, headline, contact, then each resume section in the original structure."""
    lines = _reflow_lines(_split_lines(text))
    blocks: list[tuple[str, str]] = []
    header = True
    section = ""
    for line in lines:
        if header and not blocks:
            blocks.append((line, "h1"))
            continue
        if header and _is_headline_line(line):
            blocks.append((line, "subtitle"))
            continue
        if header and _is_contact_line(line):
            blocks.append((_pretty_contact(line), "contact"))
            header = False
            continue
        header = False
        kind = _heading_kind(line)
        if kind == "h2":
            section = line.strip().upper()
            blocks.append((line, "h2"))
            continue
        blocks.append((line, _body_kind(line, section)))
    return blocks


_EXPERIENCE_SECTIONS = {"PROFESSIONAL EXPERIENCE", "EXPERIENCE", "WORK EXPERIENCE", "WORK HISTORY"}
_SKILL_SECTIONS = {"CORE COMPETENCIES", "SKILLS", "TECHNICAL SKILLS", "CORE SKILLS", "COMPETENCIES"}
_ROLE_WORD_RE = re.compile(
    r"(?i)\b(manager|leader|lead|head|engineer|director|consultant|officer|specialist|analyst)\b"
)


def _body_kind(line: str, section: str) -> str:
    kind = _heading_kind(line)
    if kind == "li":
        body = _strip_bullet(line)
        if _label_split(body):
            return "label-li"
        return "li"
    if section in _EXPERIENCE_SECTIONS:
        if _is_role_line(line):
            return "role"
        if _is_date_line(line):
            return "date"
        if _is_company_blurb(line):
            return "blurb"
    if section in _SKILL_SECTIONS and _label_split(line):
        return "skill"
    if section == "EDUCATION":
        return "edu"
    return "p"


_LABEL_VERBS = {
    "delivered", "own", "owned", "led", "lead", "built", "managed", "improved",
    "launched", "drove", "drive", "define", "defined", "align", "aligned",
}


def _label_split(line: str) -> tuple[str, str] | None:
    if ":" not in (line or ""):
        return None
    label, rest = line.split(":", 1)
    label = label.strip(" -•")
    if not rest.strip() or not (2 <= len(label) <= 42):
        return None
    words = label.split()
    if len(words) > 5 or words[0].lower() in _LABEL_VERBS:
        return None
    return label, rest.strip()


def _is_role_line(line: str) -> bool:
    text = (line or "").strip()
    if not text or "@" in text or len(text) > 160 or text.endswith("."):
        return False
    if "|" in text and _ROLE_WORD_RE.search(text):
        return True
    return bool(_ROLE_WORD_RE.search(text) and len(text) <= 80)


_MONTH_ONLY_RE = re.compile(
    r"(?i)^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\.?$"
)


def _is_date_line(line: str) -> bool:
    text = (line or "").strip(" -–—")
    if not text or len(text) > 40:
        return False
    if _MONTH_ONLY_RE.match(text):
        return True
    return bool(
        re.search(
            r"(?i)^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{4}).{0,24}(present|current|now|\d{4})$",
            text,
        )
    )


def _reflow_lines(lines: list[str]) -> list[str]:
    """Join PDF line breaks so a job, bullet, or competency is one line again."""
    merged: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line in {"-", "•", "●"} and index + 1 < len(lines):
            index += 1
            chunk = [lines[index]]
            index += 1
            while index < len(lines) and not _starts_new_item(lines[index]):
                chunk.append(lines[index])
                index += 1
            merged.append("- " + " ".join(chunk))
            continue
        if _is_role_line(line):
            parts = [line]
            index += 1
            while index < len(lines) and _role_continuation(lines[index]):
                parts.append(lines[index])
                index += 1
            merged.append(re.sub(r"\s*\|\s*", " | ", " ".join(parts)))
            continue
        if line.endswith(":") and _label_split(line + " x"):
            parts = [line]
            index += 1
            while index < len(lines) and not _starts_new_item(lines[index]) and not lines[index].endswith(":"):
                parts.append(lines[index])
                index += 1
            merged.append(" ".join(parts))
            continue
        if index + 1 < len(lines) and lines[index + 1].startswith("|"):
            parts = [line, lines[index + 1].lstrip("|").strip()]
            index += 2
            while index < len(lines) and (
                _is_date_line(lines[index])
                or lines[index].lower() in {"in progress", "present", "university", "college"}
            ):
                parts.append(lines[index])
                index += 1
            merged.append(parts[0] + " | " + " ".join(part for part in parts[1:] if part))
            continue
        if line.endswith("|") or line == "|" or line.startswith("|"):
            parts = [line]
            index += 1
            while index < len(lines) and index < len(parts) + 4 and not _starts_new_item(lines[index]):
                parts.append(lines[index])
                if _is_date_line(lines[index]) or lines[index].endswith("."):
                    index += 1
                    break
                index += 1
            merged.append(re.sub(r"\s*\|\s*", " | ", " ".join(parts)))
            continue
        merged.append(line)
        index += 1
    return merged


def _starts_new_item(line: str) -> bool:
    return line in {"-", "•", "●"} or _heading_kind(line) == "h2" or _is_role_line(line)


def _role_continuation(line: str) -> bool:
    text = (line or "").strip()
    if not text or text.endswith(".") or _heading_kind(text) == "h2" or text in {"-", "•", "●"}:
        return False
    if text == "|" or text.startswith("|"):
        return True
    if _is_date_line(text) or _MONTH_ONLY_RE.match(text.strip(" -–—")):
        return True
    return len(text) <= 40 and not _is_role_line(text)


def _is_company_blurb(line: str) -> bool:
    text = (line or "").strip()
    if len(text) < 40 or text.startswith("-"):
        return False
    return bool(re.match(r"^[A-Z][\w&.'-]+(?:\s+[A-Z][\w&.'-]+){0,4}\s+is\b", text))


def _role_pieces(line: str, blue: tuple[int, int, int], gray: tuple[int, int, int]) -> list[tuple[str, str, tuple[int, int, int]]]:
    text = line.strip()
    date = ""
    match = re.search(
        r"(?i)((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}.*|\b(?:19|20)\d{2}\b.*)$",
        text,
    )
    if match and len(match.group(1)) <= 40:
        date = match.group(1).strip()
        text = text[: match.start()].strip(" |-")
    if "|" in text:
        left, right = [part.strip() for part in text.split("|", 1)]
        pieces = [(f"{left}  |  {right}", "B", blue)]
    else:
        pieces = [(text, "B", blue)]
    if date:
        pieces.append(("   |   " + date, "", gray))
    return pieces


def _label_pieces(line: str, label_color: tuple[int, int, int], ink: tuple[int, int, int], bold_rest: bool = False) -> list[tuple[str, str, tuple[int, int, int]]]:
    split = _label_split(line)
    if not split:
        return [(line, "B" if bold_rest else "", ink)]
    return [(split[0] + ":  ", "B", label_color), (split[1], "B" if bold_rest else "", ink)]


def _edu_pieces(line: str, navy: tuple[int, int, int], ink: tuple[int, int, int]) -> list[tuple[str, str, tuple[int, int, int]]]:
    if "|" not in line:
        return [(line, "B", navy)]
    degree, school = [part.strip() for part in line.split("|", 1)]
    return [(degree + "   |   ", "B", navy), (school, "", ink)]


def _is_headline_line(line: str) -> bool:
    text = (line or "").strip()
    if not text or "@" in text or _heading_kind(text) == "h2":
        return False
    if not ("|" in text or "•" in text):
        return False
    digits = re.sub(r"\D", "", text)
    return len(digits) < 8 and len(text) <= 120


def _is_contact_line(line: str) -> bool:
    text = (line or "").strip()
    if "@" in text or re.search(r"(?i)\biqama\b", text):
        return True
    digits = re.sub(r"\D", "", text)
    return len(digits) >= 8 and ("|" in text or "•" in text or "," in text)


def _pretty_contact(line: str) -> str:
    parts = [part.strip() for part in re.split(r"\s*(?:\||•|·)\s*", line) if part.strip()]
    if len(parts) < 2:
        return _pretty_phone_text(line, assume_ksa=_saudi_context(line))
    saudi = _saudi_context(line)
    places: list[str] = []
    phones: list[str] = []
    emails: list[str] = []
    extras: list[str] = []
    for part in parts:
        digits = re.sub(r"\D", "", part)
        if "@" in part:
            emails.append(part)
        elif len(digits) >= 8 and sum(char.isalpha() for char in part) < 3:
            phones.append(_pretty_phone_text(part, assume_ksa=saudi))
        elif re.search(r"(?i)\biqama\b", part):
            extras.append(part)
        else:
            places.append(part)
    return "  •  ".join([*places, *phones, *emails, *extras])


def _saudi_context(text: str) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in ("riyadh", "saudi", "ksa", "iqama", "jeddah", "dammam"))


def _pretty_phone_text(text: str, assume_ksa: bool = False) -> str:
    def repl(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if digits.startswith("00"):
            digits = digits[2:]
        local = ""
        if digits.startswith("966") and len(digits) >= 12:
            local = digits[-9:]
        elif assume_ksa and len(digits) == 9 and digits.startswith("5"):
            local = digits
        if not local:
            return match.group(0)
        return f"+966 {local[:2]} {local[2:5]} {local[5:]}"

    return re.sub(r"(?:\+|00)?\d[\d\s\-()]{7,}\d", repl, text)


def preview_text_from_docx(data: bytes) -> str:
    from docx import Document

    document = Document(io.BytesIO(data))
    lines = []
    for text, kind in _blocks_from_docx(document):
        if kind == "blank":
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lines.append(text)
    return "\n".join(lines).strip()


def _validate_upload(data: bytes, filename: str, expected: str) -> None:
    if not data:
        raise ConvertError("Empty file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ConvertError("File is larger than 8 MB.", 413)

    ext = _extension(filename)
    if expected == "pdf":
        if data.startswith(b"%PDF"):
            return
        if ext == ".doc":
            raise ConvertError("Old .doc is not supported. Save as PDF or DOCX first.", 415)
        raise ConvertError("Upload a PDF file (.pdf).", 415)
    if expected == "docx":
        if _looks_like_docx(data):
            return
        if ext == ".doc":
            raise ConvertError("Old .doc is not supported. Save as DOCX or convert to PDF first.", 415)
        raise ConvertError("Upload a Word file (.docx).", 415)


def _looks_like_docx(data: bytes) -> bool:
    return data[:2] == b"PK"


def _extension(filename: str) -> str:
    name = (filename or "").strip().lower()
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1]


def _split_lines(text: str) -> list[str]:
    cleaned = (text or "").replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("\u00a0", " ")
    lines = []
    for raw in cleaned.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


def _save_docx(document) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _font_file() -> Path | None:
    for path in (*_WINDOWS_FONTS, *_LINUX_FONTS):
        if path.exists():
            return path
    return None


def _render_pdf(blocks: list[tuple[str, str]]) -> bytes:
    try:
        from fpdf import FPDF
    except ImportError:
        return _fallback_pdf(blocks)

    pdf = FPDF(format="Letter", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(19, 16, 19)

    font_path = _font_file()
    family = "Helvetica"
    has_italic = False
    if font_path is not None:
        try:
            family = "Body"
            pdf.add_font("Body", "", str(font_path))
            bold_path = _bold_companion(font_path)
            pdf.add_font("Body", "B", str(bold_path or font_path))
            italic_path = _italic_companion(font_path)
            if italic_path is not None:
                pdf.add_font("Body", "I", str(italic_path))
                has_italic = True
        except Exception:
            family = "Helvetica"
            font_path = None

    navy = (0x1B, 0x3A, 0x6B)
    blue = (0x2E, 0x75, 0xB6)
    gray = (0x55, 0x55, 0x55)
    ink = (0x33, 0x33, 0x33)

    def use_font(style: str, size: float) -> None:
        chosen = style
        if chosen == "I" and not has_italic:
            chosen = ""
        try:
            pdf.set_font(family, style=chosen, size=size)
        except Exception:
            pdf.set_font("Helvetica", size=size)

    def write(text: str, size: float, style: str = "", space_after: float = 1.0, color=ink, indent: float = 0, align: str = "L") -> None:
        payload = _pdf_text(text)
        if font_path is None:
            payload = _latin1(payload)
        pdf.set_text_color(*color)
        pdf.set_x(pdf.l_margin + indent)
        use_font(style, size)
        width = pdf.w - pdf.l_margin - pdf.r_margin - indent
        try:
            pdf.multi_cell(width, max(size * 0.42, 4.4), payload, align=align)
        except Exception:
            pdf.set_font("Helvetica", size=size)
            pdf.multi_cell(width, 5, _latin1(payload), align=align)
        pdf.ln(space_after)

    def rich(pieces: list[tuple[str, str, tuple[int, int, int]]], size: float, space_after: float = 0.8, indent: float = 0) -> None:
        line_h = max(size * 0.42, 4.4)
        pdf.set_x(pdf.l_margin + indent)
        for text, style, color in pieces:
            payload = _pdf_text(text)
            if font_path is None:
                payload = _latin1(payload)
            pdf.set_text_color(*color)
            use_font(style, size)
            pdf.write(line_h, payload)
        pdf.ln(line_h + space_after)

    for text, kind in blocks:
        if kind == "blank":
            pdf.ln(1.5)
            continue
        if kind == "h1":
            write(text, 26, "B", 1.5, navy, align="C")
            continue
        if kind == "subtitle":
            write(text, 12, "I", 1.2, blue, align="C")
            continue
        if kind == "contact":
            write(text, 9.5, "", 4, gray, align="C")
            continue
        if kind == "h2":
            pdf.ln(2.5)
            write(text.rstrip(":"), 11, "B", 0.6, navy)
            pdf.set_draw_color(*blue)
            pdf.set_line_width(0.26)
            y = pdf.get_y()
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
            pdf.ln(1.6)
            continue
        if kind == "role":
            rich(_role_pieces(text, blue, gray), 10, 0.8)
            continue
        if kind == "date":
            write(text, 10, "", 0.6, gray)
            continue
        if kind == "blurb":
            write(text, 9.5, "I", 0.8, gray)
            continue
        if kind == "skill":
            rich(_label_pieces(text, navy, ink, bold_rest=False), 10, 0.7)
            continue
        if kind == "edu":
            rich(_edu_pieces(text, navy, ink), 10.5, 1.0)
            continue
        if kind in {"li", "label-li"}:
            body = _strip_bullet(text)
            split = _label_split(body) if kind == "label-li" else None
            if split:
                pieces = [(f"•  {split[0]}: ", "B", ink), (split[1], "", ink)]
            else:
                pieces = [(f"•  {body}", "", ink)]
            rich(pieces, 10, 0.7, indent=2)
            continue
        write(text, 10, "", 0.8, ink)

    output = pdf.output()
    return bytes(output)


def _pdf_text(text: str) -> str:
    cleaned = "".join(ch if ch == "\n" or ord(ch) >= 32 else " " for ch in (text or ""))
    cleaned = cleaned.replace("\x00", " ").strip()
    parts = []
    for token in cleaned.split(" "):
        if len(token) > 90:
            parts.extend(token[i : i + 90] for i in range(0, len(token), 90))
        else:
            parts.append(token)
    return " ".join(parts)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _fallback_pdf(blocks: list[tuple[str, str]]) -> bytes:
    lines: list[str] = []
    for text, kind in blocks:
        if kind == "blank":
            lines.append("")
            continue
        payload = _latin1(_pdf_text(text))
        if kind == "h2":
            payload = payload.upper()
        if kind == "li":
            payload = "- " + _latin1(_strip_bullet(text))
        while len(payload) > 90:
            lines.append(payload[:90])
            payload = payload[90:]
        lines.append(payload)
    if not lines:
        lines = [" "]

    per_page = 50
    pages = [lines[i : i + per_page] for i in range(0, len(lines), per_page)]
    content_streams: list[bytes] = []
    for page_lines in pages:
        cmds = ["BT", "/F1 11 Tf", "50 742 Td"]
        for index, line in enumerate(page_lines):
            esc = _pdf_escape(line)
            cmds.append(f"({esc}) Tj" if index == 0 else f"0 -14 Td ({esc}) Tj")
        cmds.append("ET")
        content_streams.append("\n".join(cmds).encode("latin-1", "replace"))

    n_pages = len(pages)
    page_ids = list(range(4, 4 + n_pages))
    content_ids = list(range(4 + n_pages, 4 + 2 * n_pages))
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids = " ".join(f"{item} 0 R" for item in page_ids)
    objects[2] = f"<< /Type /Pages /Count {n_pages} /Kids [{kids}] >>".encode("ascii")
    for page_id, content_id, stream in zip(page_ids, content_ids, content_streams):
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_id} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode("ascii")
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )

    max_id = max(objects)
    out = bytearray(b"%PDF-1.4\n")
    offsets = {0: 0}
    for obj_id in range(1, max_id + 1):
        offsets[obj_id] = len(out)
        out += f"{obj_id} 0 obj\n".encode("ascii") + objects[obj_id] + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {max_id + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for obj_id in range(1, max_id + 1):
        out += f"{offsets[obj_id]:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def _italic_companion(regular: Path) -> Path | None:
    name = regular.name.lower()
    mapping = {
        "arial.ttf": "ariali.ttf",
        "calibri.ttf": "calibrii.ttf",
        "segoeui.ttf": "segoeuii.ttf",
        "dejavusans.ttf": "DejaVuSans-Oblique.ttf",
        "liberationsans-regular.ttf": "LiberationSans-Italic.ttf",
    }
    italic_name = mapping.get(name)
    if not italic_name:
        return None
    candidate = regular.with_name(italic_name)
    return candidate if candidate.exists() else None


def _bold_companion(regular: Path) -> Path | None:
    name = regular.name.lower()
    mapping = {
        "arial.ttf": "arialbd.ttf",
        "calibri.ttf": "calibrib.ttf",
        "segoeui.ttf": "segoeuib.ttf",
        "dejavusans.ttf": "DejaVuSans-Bold.ttf",
        "liberationsans-regular.ttf": "LiberationSans-Bold.ttf",
    }
    bold_name = mapping.get(name)
    if not bold_name:
        return None
    candidate = regular.with_name(bold_name)
    return candidate if candidate.exists() else None


def _latin1(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")
