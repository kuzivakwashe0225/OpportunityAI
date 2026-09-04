from io import BytesIO

import pytest
from docx import Document as DocxDocument
from pypdf import PdfWriter

from opportunity_agent.document_text import extract_text


def _make_pdf_bytes(text: str) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    # pypdf can't easily draw text without reportlab; assert on structure
    # instead for the PDF case (see test below) rather than real content.
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    doc = DocxDocument()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_extract_text_from_plain_text():
    content = "Software Engineer, Acme Corp (2021-2024)".encode("utf-8")

    assert extract_text(content, "text/plain") == "Software Engineer, Acme Corp (2021-2024)"


def test_extract_text_from_docx():
    content = _make_docx_bytes(["BSc Computer Science, University of Zimbabwe", "Software Engineer, Acme Corp"])

    text = extract_text(
        content,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert "BSc Computer Science, University of Zimbabwe" in text
    assert "Software Engineer, Acme Corp" in text


def test_extract_text_from_pdf_does_not_crash_on_a_blank_page():
    content = _make_pdf_bytes("irrelevant")

    text = extract_text(content, "application/pdf")

    assert text == "" or isinstance(text, str)


def test_unsupported_content_type_raises_a_clear_error():
    with pytest.raises(ValueError, match="unsupported"):
        extract_text(b"whatever", "application/x-unknown")


def test_extract_text_from_docx_joins_paragraphs_with_newlines():
    content = _make_docx_bytes(["Line one", "Line two"])

    text = extract_text(
        content,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert text == "Line one\nLine two"
