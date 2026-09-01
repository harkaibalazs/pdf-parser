#!/usr/bin/env python3
"""Minimal web GUI for the pdf-parser CLI.

Upload a PDF -- or a ZIP archive of PDFs -- configure the parser options, run
`main.py` server-side, then download the generated output as a zip.

For a single PDF the output zip holds document.md + manifest.json + images/.
For a ZIP upload every PDF found in the archive (recursively) is parsed and the
input directory layout is mirrored in the result, with each `foo.pdf` replaced
by a `foo/` directory holding that PDF's output, plus a `_batch_report.json`
summarising per-file success or failure.

Run with:
    .venv/bin/python webgui.py
then open http://127.0.0.1:5000
"""

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

from flask import (
    Flask,
    abort,
    render_template_string,
    request,
    send_file,
)
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
MAIN_SCRIPT = BASE_DIR / "main.py"
RUNS_DIR = BASE_DIR / "web_runs"


def _env_mb(name: str, default_mb: int) -> int:
    """Read a megabyte-valued env override, returning bytes.

    Sizes are configured in whole MB (MAX_UPLOAD_MB=512) because that is how
    the matching limits on the proxy and the pod are usually expressed.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default_mb * 1024 * 1024
    try:
        value = int(raw)
    except ValueError:
        return default_mb * 1024 * 1024
    return max(1, value) * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


MAX_UPLOAD_BYTES = _env_mb("MAX_UPLOAD_MB", 512)

# Limits applied to uploaded ZIPs. The archive is attacker-controlled input, so
# every one of these is a hard stop rather than a best-effort hint.
MAX_ZIP_ENTRIES = _env_int("MAX_ZIP_ENTRIES", 2_000)   # entries in the archive
MAX_ENTRY_BYTES = _env_mb("MAX_ENTRY_MB", 512)         # uncompressed, one entry
MAX_TOTAL_BYTES = _env_mb("MAX_TOTAL_MB", 4096)        # uncompressed, whole archive
MAX_PDFS = _env_int("MAX_PDFS", 200)                   # PDFs parsed per batch
PER_PDF_TIMEOUT = int(os.environ.get("PER_PDF_TIMEOUT", "300"))  # seconds
RUN_TTL_SECONDS = int(os.environ.get("RUN_TTL_SECONDS", str(24 * 3600)))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Created at import time so it also exists under gunicorn (which never runs
# the __main__ block below). Per-run files are keyed by job_id on disk, so
# any worker can serve a download regardless of which one produced it.
RUNS_DIR.mkdir(exist_ok=True)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF Parser</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 680px;
         margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
  h1 { margin-bottom: .25rem; }
  .sub { color: #888; margin-top: 0; }
  form { display: grid; gap: 1rem; margin-top: 1.5rem; }
  fieldset { border: 1px solid #8884; border-radius: 8px; padding: 1rem; }
  legend { padding: 0 .4rem; font-weight: 600; }
  label { display: block; margin: .6rem 0 .2rem; font-weight: 500; }
  input[type=text], input[type=number] { width: 100%; padding: .5rem;
    border: 1px solid #8886; border-radius: 6px; box-sizing: border-box;
    background: transparent; color: inherit; }
  .row { display: flex; gap: 1rem; }
  .row > div { flex: 1; }
  .check { display: flex; align-items: center; gap: .5rem; font-weight: 500; }
  .check input { width: auto; }
  button { padding: .7rem 1.2rem; font-size: 1rem; border: 0; border-radius: 8px;
    background: #2563eb; color: #fff; cursor: pointer; }
  button:hover { background: #1d4ed8; }
  .hint { font-size: .85rem; color: #888; margin: .1rem 0 0; }
  .flash { background: #ef44441a; border: 1px solid #ef4444; color: #b91c1c;
    padding: .75rem 1rem; border-radius: 8px; }
  .result { background: #22c55e1a; border: 1px solid #22c55e; padding: 1rem;
    border-radius: 8px; }
  pre { background: #8881; padding: .75rem; border-radius: 8px; overflow-x: auto;
    white-space: pre-wrap; font-size: .85rem; }
  .dl { display: inline-block; margin-top: .5rem; padding: .6rem 1rem;
    background: #16a34a; color: #fff; border-radius: 8px; text-decoration: none; }
</style>
</head>
<body>
  <h1>PDF Parser</h1>
  <p class="sub">Upload a PDF, or a ZIP of PDFs, tune the options, and download the parsed output.</p>

  {% if error %}<div class="flash">{{ error }}</div>{% endif %}

  {% if result %}
  <div class="result">
    <strong>Done.</strong> Parsed <code>{{ result.filename }}</code>{% if result.total %}
    &mdash; {{ result.ok }}/{{ result.total }} PDF(s) succeeded{% endif %}.
    <a class="dl" href="{{ url_for('download', job_id=result.job_id) }}">Download output (.zip)</a>
  </div>
  <label>Parser log</label>
  <pre>{{ result.log }}</pre>
  {% endif %}

  <form method="post" action="{{ url_for('run') }}" enctype="multipart/form-data">
    <fieldset>
      <legend>Input</legend>
      <label for="pdf">PDF or ZIP file</label>
      <input type="file" id="pdf" name="pdf" accept="application/pdf,.pdf,application/zip,.zip" required>
      <p class="hint">A ZIP is searched recursively for PDFs; the result mirrors its
      directory layout, with each <code>foo.pdf</code> replaced by a <code>foo/</code>
      directory of parsed output. Maximum upload size: {{ max_upload_mb }} MB.</p>
    </fieldset>

    <fieldset>
      <legend>Options</legend>
      <label class="check">
        <input type="checkbox" name="ocr" value="on" checked> Run OCR on extracted images
      </label>
      <p class="hint">Requires the Tesseract binary on the server. If missing, parsing continues without OCR.</p>

      <label for="ocr_lang">OCR language(s)</label>
      <input type="text" id="ocr_lang" name="ocr_lang" value="eng">
      <p class="hint">Tesseract language code(s), e.g. <code>eng</code> or <code>eng+hun</code>.</p>

      <div class="row">
        <div>
          <label for="min_image_size">Min image size (px)</label>
          <input type="number" id="min_image_size" name="min_image_size" value="50" min="0">
          <p class="hint">Drop images smaller than this in either dimension.</p>
        </div>
        <div>
          <label for="context_chars">Context chars</label>
          <input type="number" id="context_chars" name="context_chars" value="300" min="0">
          <p class="hint">Surrounding text recorded per image.</p>
        </div>
      </div>
    </fieldset>

    <button type="submit">Parse</button>
  </form>
</body>
</html>
"""


class UnsafeArchive(Exception):
    """Raised when an uploaded ZIP violates one of the safety limits."""


def _member_target(name: str, dest: Path) -> Path:
    """Resolve a ZIP entry name to a path inside `dest`, or raise.

    Guards against zip-slip: absolute paths, `..` traversal, and anything that
    resolves outside `dest`. `dest` is expected to be already resolved.
    """
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise UnsafeArchive(f"absolute path in archive: {name!r}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts:
        raise UnsafeArchive(f"empty entry name in archive: {name!r}")
    if ".." in parts:
        raise UnsafeArchive(f"path traversal in archive: {name!r}")

    target = (dest / Path(*parts)).resolve()
    if target != dest and dest not in target.parents:
        raise UnsafeArchive(f"entry escapes extraction root: {name!r}")
    return target


def extract_zip_safely(zip_path: Path, dest: Path) -> None:
    """Extract `zip_path` into `dest`, enforcing the MAX_* limits.

    Only regular files are written -- symlinks and other special entries are
    rejected rather than skipped, since a symlink is the other half of a
    zip-slip write. Sizes are counted from the bytes actually decompressed, not
    from the header, because a crafted archive can lie in its header.
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest = dest.resolve()
    total_written = 0

    with zipfile.ZipFile(zip_path) as zf:
        entries = zf.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise UnsafeArchive(
                f"archive has {len(entries)} entries (limit {MAX_ZIP_ENTRIES})")

        for info in entries:
            if info.is_dir():
                _member_target(info.filename.rstrip("/"), dest).mkdir(
                    parents=True, exist_ok=True)
                continue

            mode = info.external_attr >> 16
            if mode and (mode & 0o170000) not in (0o100000, 0):
                raise UnsafeArchive(
                    f"archive contains a non-regular file: {info.filename!r}")

            target = _member_target(info.filename, dest)
            target.parent.mkdir(parents=True, exist_ok=True)

            written = 0
            with zf.open(info) as src_fh, open(target, "wb") as out_fh:
                while True:
                    chunk = src_fh.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    total_written += len(chunk)
                    if written > MAX_ENTRY_BYTES:
                        raise UnsafeArchive(
                            f"entry {info.filename!r} exceeds "
                            f"{MAX_ENTRY_BYTES // (1024 * 1024)} MB uncompressed")
                    if total_written > MAX_TOTAL_BYTES:
                        raise UnsafeArchive(
                            f"archive exceeds {MAX_TOTAL_BYTES // (1024 * 1024)} MB "
                            "uncompressed (possible zip bomb)")
                    out_fh.write(chunk)


def find_pdfs(root: Path) -> list:
    """Every .pdf under `root`, recursively, in a stable order."""
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() == ".pdf"
    )


def parse_one(pdf_path: Path, out_dir: Path, ocr_enabled: bool, ocr_lang: str,
              min_image_size: int, context_chars: int):
    """Run main.py on a single PDF. Returns (ok, log).

    argv is a list and no shell is involved, and `pdf_path` is always absolute,
    so a filename cannot be read as an option or injected into a command.
    """
    cmd = [
        sys.executable, str(MAIN_SCRIPT), str(pdf_path),
        "-o", str(out_dir),
        "--ocr-lang", ocr_lang,
        "--min-image-size", str(min_image_size),
        "--context-chars", str(context_chars),
    ]
    if not ocr_enabled:
        cmd.append("--no-ocr")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=BASE_DIR, timeout=PER_PDF_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {PER_PDF_TIMEOUT}s"

    log = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0 or not out_dir.exists():
        return False, log or f"exit {proc.returncode}"
    return True, log


def cleanup_old_runs() -> None:
    """Drop run directories older than RUN_TTL_SECONDS. Best effort."""
    cutoff = time.time() - RUN_TTL_SECONDS
    try:
        candidates = list(RUNS_DIR.iterdir())
    except OSError:
        return
    for entry in candidates:
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def render(error=None, result=None):
    return render_template_string(
        PAGE, error=error, result=result,
        max_upload_mb=MAX_UPLOAD_BYTES // (1024 * 1024))


@app.errorhandler(413)
def upload_too_large(_exc):
    """Werkzeug aborts oversized uploads before `run()` sees the request.

    Without this the client gets Flask's bare 413 page -- or, because the
    response goes out while the browser is still sending the body, what looks
    like a dropped connection. Render the normal page with a real explanation
    instead.
    """
    limit = MAX_UPLOAD_BYTES // (1024 * 1024)
    return render(
        error=f"That upload is over the {limit} MB limit. Split the archive into "
              f"smaller ZIPs, or raise MAX_UPLOAD_MB on the server (and the "
              f"matching body-size limit on the proxy in front of it)."), 413


@app.get("/")
def index():
    return render()


@app.post("/run")
def run():
    file = request.files.get("pdf")
    if file is None or not file.filename:
        return render(error="No file uploaded."), 400

    filename = secure_filename(file.filename)
    lower = filename.lower()
    if not (lower.endswith(".pdf") or lower.endswith(".zip")):
        return render(error="Please upload a .pdf or .zip file."), 400
    is_zip = lower.endswith(".zip")

    # Parse + validate options.
    ocr_enabled = request.form.get("ocr") == "on"
    ocr_lang = (request.form.get("ocr_lang") or "eng").strip() or "eng"
    if not all(c.isalnum() or c in "+_-" for c in ocr_lang):
        return render(error="Invalid OCR language code."), 400
    try:
        min_image_size = int(request.form.get("min_image_size", "50"))
        context_chars = int(request.form.get("context_chars", "300"))
    except ValueError:
        return render(error="Min image size and context chars must be integers."), 400
    if min_image_size < 0 or context_chars < 0:
        return render(error="Numeric options must be non-negative."), 400

    cleanup_old_runs()

    job_id = uuid.uuid4().hex
    job_dir = RUNS_DIR / job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    input_dir.mkdir(parents=True, exist_ok=True)

    upload_path = input_dir / filename
    file.save(upload_path)

    # Work out the list of PDFs to parse and the root their paths are relative
    # to, so the output can mirror the input layout.
    if is_zip:
        extract_root = input_dir / "extracted"
        try:
            extract_zip_safely(upload_path, extract_root)
        except UnsafeArchive as exc:
            shutil.rmtree(job_dir, ignore_errors=True)
            return render(error=f"Rejected archive: {exc}"), 400
        except zipfile.BadZipFile:
            shutil.rmtree(job_dir, ignore_errors=True)
            return render(error="That file is not a readable ZIP archive."), 400

        pdfs = find_pdfs(extract_root)
        if not pdfs:
            shutil.rmtree(job_dir, ignore_errors=True)
            return render(error="No PDFs found in that archive."), 400
        if len(pdfs) > MAX_PDFS:
            shutil.rmtree(job_dir, ignore_errors=True)
            return render(
                error=f"Archive holds {len(pdfs)} PDFs (limit {MAX_PDFS})."), 400
    else:
        extract_root = input_dir
        pdfs = [upload_path]

    # Parse each PDF. One bad file fails only its own entry.
    report = []
    logs = []
    ok_count = 0
    used_dirs = set()

    for pdf_path in pdfs:
        rel = pdf_path.relative_to(extract_root)
        if is_zip:
            # foo/bar.pdf -> foo/bar/ , de-duplicated if two names collide
            # (e.g. "a.pdf" and "a.PDF" in the same directory).
            out_dir = output_dir / rel.parent / rel.stem
            suffix = 1
            while out_dir in used_dirs:
                suffix += 1
                out_dir = output_dir / rel.parent / f"{rel.stem}_{suffix}"
            used_dirs.add(out_dir)
        else:
            # Single PDF: keep the original flat layout in the zip.
            out_dir = output_dir

        ok, log = parse_one(pdf_path, out_dir, ocr_enabled, ocr_lang,
                            min_image_size, context_chars)
        if ok:
            ok_count += 1
        report.append({
            "source": rel.as_posix(),
            "output": out_dir.relative_to(output_dir).as_posix() or ".",
            "status": "ok" if ok else "failed",
            "log": log,
        })
        logs.append(f"[{'ok' if ok else 'FAILED'}] {rel.as_posix()}"
                    + ("" if ok else f"\n{log}"))

    if ok_count == 0:
        detail = "\n\n".join(logs)
        shutil.rmtree(job_dir, ignore_errors=True)
        return render(error=f"Parsing failed for every PDF.\n\n{detail}"), 500

    if is_zip:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "_batch_report.json").write_text(
            json.dumps({"total": len(pdfs), "ok": ok_count, "files": report},
                       indent=2, ensure_ascii=False)
        )

    zip_path = job_dir / f"{Path(filename).stem}_output.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in output_dir.rglob("*"):
            if item.is_file():
                zf.write(item, item.relative_to(output_dir))

    return render(result={
        "job_id": job_id,
        "filename": filename,
        "log": "\n\n".join(logs),
        "total": len(pdfs) if is_zip else 0,
        "ok": ok_count,
    })


@app.get("/download/<job_id>")
def download(job_id):
    if not job_id.isalnum():
        abort(400)
    job_dir = RUNS_DIR / job_id
    zips = list(job_dir.glob("*_output.zip")) if job_dir.exists() else []
    if not zips:
        abort(404)
    return send_file(zips[0], as_attachment=True, download_name=zips[0].name)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
