"""Plain-text extraction from uploaded documents (PDF, DOCX, plain text).

Feeds extraction_llm.py: the LLM only ever sees text, never raw bytes, so
this module is the one place format-specific parsing lives.
"""

from __future__ import annotations

from io import BytesIO

_DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def extract_text(content: bytes, content_type: str) -> str:
    if content_type == "application/pdf":
        return _extract_pdf_text(content)
    if content_type == _DOCX_CONTENT_TYPE:
        return _extract_docx_text(content)
    if content_type.startswith("text/"):
        return content.decode("utf-8", errors="replace")
    raise ValueError(f"unsupported document type for text extraction: {content_type}")


def _extract_pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx_text(content: bytes) -> str:
    from docx import Document as DocxDocument

    doc = DocxDocument(BytesIO(content))
    return "\n".join(paragraph.text for paragraph in doc.paragraphs)
