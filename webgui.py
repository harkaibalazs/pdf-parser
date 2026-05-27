#!/usr/bin/env python3
"""Minimal web GUI for the pdf-parser CLI.

Upload a PDF, configure the parser options, run `main.py` server-side, then
download the generated output (document.md + manifest.json + images/) as a zip.

Run with:
    .venv/bin/python webgui.py
then open http://127.0.0.1:5000
"""

import shutil
import subprocess
import sys
import tempfile
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
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

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
  <p class="sub">Upload a PDF, tune the options, and download the parsed output.</p>

  {% if error %}<div class="flash">{{ error }}</div>{% endif %}

  {% if result %}
  <div class="result">
    <strong>Done.</strong> Parsed <code>{{ result.filename }}</code>.
    <a class="dl" href="{{ url_for('download', job_id=result.job_id) }}">Download output (.zip)</a>
  </div>
  <label>Parser log</label>
  <pre>{{ result.log }}</pre>
  {% endif %}

  <form method="post" action="{{ url_for('run') }}" enctype="multipart/form-data">
    <fieldset>
      <legend>Input</legend>
      <label for="pdf">PDF file</label>
      <input type="file" id="pdf" name="pdf" accept="application/pdf,.pdf" required>
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

    <button type="submit">Parse PDF</button>
  </form>
</body>
</html>
"""


def render(error=None, result=None):
    return render_template_string(PAGE, error=error, result=result)


@app.get("/")
def index():
    return render()


@app.post("/run")
def run():
    file = request.files.get("pdf")
    if file is None or not file.filename:
        return render(error="No file uploaded."), 400

    filename = secure_filename(file.filename)
    if not filename.lower().endswith(".pdf"):
        return render(error="Please upload a .pdf file."), 400

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

    job_id = uuid.uuid4().hex
    job_dir = RUNS_DIR / job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    input_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = input_dir / filename
    file.save(pdf_path)

    cmd = [
        sys.executable, str(MAIN_SCRIPT), str(pdf_path),
        "-o", str(output_dir),
        "--ocr-lang", ocr_lang,
        "--min-image-size", str(min_image_size),
        "--context-chars", str(context_chars),
    ]
    if not ocr_enabled:
        cmd.append("--no-ocr")

    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE_DIR)
    log = (proc.stdout + proc.stderr).strip()

    if proc.returncode != 0 or not output_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
        return render(error=f"Parsing failed (exit {proc.returncode}).\n\n{log}"), 500

    zip_path = job_dir / f"{Path(filename).stem}_output.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in output_dir.rglob("*"):
            if item.is_file():
                zf.write(item, item.relative_to(output_dir))

    return render(result={"job_id": job_id, "filename": filename, "log": log})


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
    RUNS_DIR.mkdir(exist_ok=True)
    app.run(host="127.0.0.1", port=5000, debug=True)
