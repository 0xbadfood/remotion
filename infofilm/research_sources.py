"""Extract readable source text before it reaches a research model."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from .core import digest

MAX_PDF_BYTES = 20 * 1024 * 1024


def readability_error(content: str) -> str | None:
    if content.lstrip().startswith("%PDF-"):
        return "Raw PDF data must be converted to readable text before summarization"
    if len(content.strip()) < 100:
        return f"Source contained too little readable material ({len(content.strip())} characters)"
    sample = content[:100_000]
    invalid = sum(character == "\ufffd" or (ord(character) < 32 and character not in "\n\r\t\f")
                  for character in sample)
    if invalid / max(1, len(sample)) > .01:
        return "Source contained binary data or extensive text decoding errors"
    return None


def fetch_pdf_text(url: str, timeout: int, archive_root: Path | None = None) -> str:
    executable = shutil.which("pdftotext")
    if not executable:
        raise ValueError("PDF text extraction requires pdftotext")
    chunks, size = [], 0
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_PDF_BYTES:
                    raise ValueError("PDF exceeded the 20 MiB source download limit")
                chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload[:1024].lstrip().startswith(b"%PDF-"):
        raise ValueError("PDF URL did not return a PDF document")
    if archive_root is not None:
        archive_root.mkdir(parents=True, exist_ok=True)
        (archive_root / (digest(url)[:16] + ".pdf")).write_bytes(payload)
    with tempfile.TemporaryDirectory(prefix="infofilm-pdf-") as temporary:
        source = Path(temporary) / "source.pdf"
        source.write_bytes(payload)
        extracted = subprocess.run([executable, "-layout", str(source), "-"],
                                   capture_output=True, text=True, timeout=30, check=False)
    if extracted.returncode:
        raise ValueError("PDF text extraction failed: " + extracted.stderr.strip()[:400])
    error = readability_error(extracted.stdout)
    if error:
        raise ValueError(error + "; this PDF may require OCR")
    return extracted.stdout
