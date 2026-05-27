# pdf-parser

Extract text and images from large PDF study materials into a format that downstream LLMs can reason about — including OCR text and surrounding context for every image, so a study-guide generator can decide whether to include each figure.

## What it does

For each PDF, the parser produces:

- **`document.md`** — the full text in reading order, with inline image references and metadata comments next to each figure.
- **`manifest.json`** — structured per-image metadata: page number, bounding box, dimensions, OCR text, and surrounding paragraphs.
- **`images/`** — every extracted image, named `page_<N>_img_<idx>.<ext>`.

Example fragment of `document.md`:

```markdown
## Page 12

The mitochondrion is a double-membrane-bound organelle...

![page_12_img_1](images/page_12_img_1.png)
<!-- image-meta id="page_12_img_1" page="12" size="640x420" ocr="Inner membrane Outer membrane Cristae Matrix" -->

These structures are responsible for ATP production...
```

The OCR text and the surrounding paragraphs give an LLM enough signal to judge whether to embed the image when generating a study guide on a given topic.

## Install

Requires Python 3.10+ and (optionally) the Tesseract binary for OCR.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# OCR (optional — the parser runs without it and skips ocr_text)
brew install tesseract                # macOS
# brew install tesseract-lang         # for non-English language packs
```

Dependencies:

- [PyMuPDF](https://pymupdf.readthedocs.io/) — PDF parsing and image extraction
- [pytesseract](https://github.com/madmaze/pytesseract) — Tesseract wrapper
- [Pillow](https://python-pillow.org/) — image decoding for OCR

## Usage

```bash
.venv/bin/python main.py input/study.pdf
```

Default output goes to `output/<pdf-stem>/`.

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `-o, --output DIR` | `output/<stem>/` | Output directory |
| `--no-ocr` | off | Skip OCR even if Tesseract is installed |
| `--ocr-lang LANG` | `eng` | Tesseract language code(s), e.g. `eng+hun` |
| `--min-image-size N` | `50` | Drop images smaller than N px in either dimension (filters decorative icons) |
| `--context-chars N` | `300` | Chars of surrounding text recorded per image |

### Examples

```bash
# English study material, full OCR
.venv/bin/python main.py input/biology.pdf

# Hungarian + English content
.venv/bin/python main.py input/biology.pdf --ocr-lang hun+eng

# Fast pass without OCR (useful for a first look)
.venv/bin/python main.py input/biology.pdf --no-ocr

# Keep tiny icons too
.venv/bin/python main.py input/biology.pdf --min-image-size 10
```

If the Tesseract binary is not on `PATH`, the parser prints a warning and continues without OCR — `ocr_text` fields will be empty but everything else still works.

## Web GUI

A small Flask front-end (`webgui.py`) wraps the CLI so you can upload a PDF,
set the options in a browser, run the parser server-side, and download the
output as a zip.

```bash
.venv/bin/pip install -r requirements.txt   # includes Flask
.venv/bin/python webgui.py                   # serves http://127.0.0.1:5000
```

Open the URL, pick a PDF, adjust the options (OCR on/off, OCR language, min
image size, context chars), and click **Parse PDF**. The parser runs as a
subprocess of `main.py`; on success a download button returns
`document.md` + `manifest.json` + `images/` packed into a single `.zip`.
Per-run files live under `web_runs/` (gitignored).

## Output schema

### `manifest.json`

```json
{
  "source": "input/biology.pdf",
  "pages": 312,
  "ocr_enabled": true,
  "ocr_lang": "eng",
  "images": [
    {
      "id": "page_12_img_1",
      "page": 12,
      "path": "images/page_12_img_1.png",
      "bbox": [72.0, 150.0, 412.0, 470.0],
      "width": 640,
      "height": 420,
      "ocr_text": "Inner membrane\nOuter membrane\nCristae\nMatrix",
      "nearby_before": "The mitochondrion is a double-membrane-bound organelle...",
      "nearby_after": "These structures are responsible for ATP production..."
    }
  ]
}
```

`bbox` is in PDF points (1 pt = 1/72 inch), ordered `[x0, y0, x1, y1]` with the origin in the top-left of the page.

### Image-meta comments

Each image reference in `document.md` is followed by an HTML comment carrying its key metadata inline, so an LLM reading the Markdown alone (no JSON) can still reason about the figure:

```html
<!-- image-meta id="page_12_img_1" page="12" size="640x420" ocr="..." -->
```

The OCR snippet inside the comment is truncated to 500 characters; the full text lives in `manifest.json`.

## Downstream use

The intended consumer is an LLM-based study-guide generator. A typical flow:

1. Parse the PDF once with this tool.
2. Feed `document.md` (or chunks of it) to the LLM along with a topic prompt.
3. When the model encounters an `image-meta` comment, it can check the OCR text and nearby context to decide whether to embed the referenced image in the generated guide.
4. Resolve the referenced path against `manifest.json` to attach the actual image file.

## Limitations

- **Reading order** follows PyMuPDF's block order. Multi-column layouts and complex page furniture (sidebars, callouts) may interleave awkwardly.
- **No image deduplication.** Repeated logos and headers are extracted on every page they appear on.
- **Tesseract OCR** is fine for printed text inside diagrams but weak on handwriting and stylised labels. Swap in a vision LLM if accuracy matters more than cost.
- **Vector graphics** drawn directly in the PDF (not embedded as raster images) are not extracted — only raster images are pulled out.

## Project layout

```
pdf-parser/
├── main.py            # CLI + parser
├── requirements.txt
├── input/             # put source PDFs here (gitignored if you wish)
└── output/            # generated per-PDF subdirectories
```
