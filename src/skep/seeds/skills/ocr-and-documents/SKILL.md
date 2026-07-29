---
name: ocr-and-documents
description: extract text from scanned documents and images via tesseract OCR
---

# OCR — scanned documents and images

Tools: dispatch_run, get_run, read_file

Requires the `ocr` extra (`uv sync --extra ocr` installs `pytesseract`
and `pillow`) AND the tesseract SYSTEM BINARY — `pip install
pytesseract` succeeds on a machine with no tesseract at all, so an
import check proves nothing here.

1. The run's FIRST step is the functional probe: `tesseract --version`
   (or `pytesseract.get_tesseract_version()`). If it fails, stop and
   report the install line — `sudo apt install tesseract-ocr` (Linux)
   / `brew install tesseract` (macOS) — instead of a dead traceback
   at the first OCR call.
2. Images: `pytesseract.image_to_string(Image.open(path))`. Scanned
   PDFs: render pages to images first, then OCR per page; keep page
   numbers in the output.
3. OCR output is noisy — report it as "extracted text" with a
   confidence caveat, never silently treat it as ground truth for
   downstream numbers.
4. Verify with an acceptance term: a word or number known to be in the
   document must appear in the extracted text.
