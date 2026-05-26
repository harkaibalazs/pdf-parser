#!/usr/bin/env python3
"""Extract text + images from a PDF, run OCR, emit a Markdown + JSON manifest.

Output layout:
    <output_dir>/
        document.md      Markdown text with inline image refs and metadata comments
        manifest.json    Per-image metadata (page, bbox, dims, OCR text, nearby text)
        images/          Extracted image files
"""

import argparse
import io
import json
import shutil
import sys
from pathlib import Path

import pymupdf
from PIL import Image


def has_tesseract() -> bool:
    return shutil.which("tesseract") is not None


def ocr_image(image_bytes: bytes, lang: str) -> str:
    import pytesseract
    img = Image.open(io.BytesIO(image_bytes))
    return pytesseract.image_to_string(img, lang=lang).strip()


def text_from_block(block: dict) -> str:
    lines = []
    for line in block.get("lines", []):
        spans = [span.get("text", "") for span in line.get("spans", [])]
        text = "".join(spans).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def next_text_block(blocks: list, start_idx: int) -> str:
    for nb in blocks[start_idx + 1:]:
        if nb.get("type") == 0:
            t = text_from_block(nb)
            if t:
                return t
    return ""


def parse_pdf(
    pdf_path: Path,
    output_dir: Path,
    ocr_enabled: bool,
    ocr_lang: str,
    min_image_size: int,
    context_chars: int,
) -> dict:
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(pdf_path)
    manifest = {
        "source": str(pdf_path),
        "pages": doc.page_count,
        "ocr_enabled": ocr_enabled,
        "ocr_lang": ocr_lang if ocr_enabled else None,
        "images": [],
    }
    md = [f"# {pdf_path.stem}\n"]

    for page_idx, page in enumerate(doc):
        page_num = page_idx + 1
        md.append(f"\n\n## Page {page_num}\n\n")

        blocks = page.get_text("dict").get("blocks", [])
        img_counter = 0
        prev_text = ""

        for b_idx, block in enumerate(blocks):
            btype = block.get("type")

            if btype == 0:  # text
                text = text_from_block(block)
                if text:
                    md.append(text + "\n\n")
                    prev_text = text
                continue

            if btype != 1:  # not image
                continue

            width = block.get("width", 0)
            height = block.get("height", 0)
            if width < min_image_size or height < min_image_size:
                continue

            img_counter += 1
            img_bytes = block.get("image")
            ext = block.get("ext", "png")
            img_id = f"page_{page_num}_img_{img_counter}"
            img_filename = f"{img_id}.{ext}"
            (images_dir / img_filename).write_bytes(img_bytes)

            ocr_text = ""
            if ocr_enabled:
                try:
                    ocr_text = ocr_image(img_bytes, lang=ocr_lang)
                except Exception as e:
                    ocr_text = f"[OCR failed: {e}]"

            nearby_before = prev_text[-context_chars:] if prev_text else ""
            nearby_after = next_text_block(blocks, b_idx)[:context_chars]

            manifest["images"].append({
                "id": img_id,
                "page": page_num,
                "path": f"images/{img_filename}",
                "bbox": list(block.get("bbox", [])),
                "width": width,
                "height": height,
                "ocr_text": ocr_text,
                "nearby_before": nearby_before,
                "nearby_after": nearby_after,
            })

            md.append(f"![{img_id}](images/{img_filename})\n")
            meta = {"id": img_id, "page": page_num, "size": f"{width}x{height}"}
            if ocr_text:
                meta["ocr"] = ocr_text.replace("\n", " ")[:500]
            meta_str = " ".join(f'{k}="{v}"' for k, v in meta.items())
            md.append(f"<!-- image-meta {meta_str} -->\n\n")

        if (page_idx + 1) % 10 == 0 or page_idx + 1 == doc.page_count:
            print(f"  page {page_idx + 1}/{doc.page_count}", file=sys.stderr)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "document.md").write_text("".join(md))
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False)
    )
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("pdf", type=Path, help="Path to input PDF")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="Output directory (default: output/<pdf-stem>/)")
    p.add_argument("--no-ocr", action="store_true", help="Skip OCR on extracted images")
    p.add_argument("--ocr-lang", default="eng",
                   help="Tesseract language(s), e.g. 'eng' or 'eng+hun'")
    p.add_argument("--min-image-size", type=int, default=50,
                   help="Drop images smaller than this (px) in either dim (default: 50)")
    p.add_argument("--context-chars", type=int, default=300,
                   help="Chars of surrounding text to record per image (default: 300)")
    args = p.parse_args()

    if not args.pdf.exists():
        sys.exit(f"PDF not found: {args.pdf}")

    output_dir = args.output or Path("output") / args.pdf.stem
    ocr_enabled = not args.no_ocr
    if ocr_enabled and not has_tesseract():
        print(
            "warning: tesseract binary not on PATH — continuing without OCR.\n"
            "         install with: brew install tesseract  (then re-run)",
            file=sys.stderr,
        )
        ocr_enabled = False

    print(f"parsing {args.pdf} → {output_dir} (ocr={ocr_enabled})", file=sys.stderr)
    m = parse_pdf(
        args.pdf, output_dir, ocr_enabled, args.ocr_lang,
        args.min_image_size, args.context_chars,
    )
    print(
        f"done. pages={m['pages']} images={len(m['images'])}\n"
        f"  → {output_dir / 'document.md'}\n"
        f"  → {output_dir / 'manifest.json'}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
