"""
PDF → XML Converter — Web App (Railway / Render compatible)
"""

import io
import os
import xml.etree.ElementTree as ET
from xml.dom import minidom

import pdfplumber
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB max batch


# ── Converter logic ──────────────────────────────────────────────────────────

def pdf_bytes_to_xml(pdf_bytes: bytes, filename: str) -> str:
    root = ET.Element("document")
    root.set("source", filename)

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        meta_el = ET.SubElement(root, "metadata")
        for k, v in (pdf.metadata or {}).items():
            m = ET.SubElement(meta_el, "meta", name=str(k))
            m.text = str(v)

        for page_num, page in enumerate(pdf.pages, start=1):
            page_el = ET.SubElement(
                root, "page",
                number=str(page_num),
                width=str(round(page.width, 2)),
                height=str(round(page.height, 2)),
            )

            tables = page.find_tables()
            table_bboxes = []

            for t_idx, tobj in enumerate(tables):
                table_el = ET.SubElement(page_el, "table", index=str(t_idx))
                table_bboxes.append(tobj.bbox)
                for r_idx, row in enumerate(tobj.extract()):
                    row_el = ET.SubElement(table_el, "row", index=str(r_idx))
                    for c_idx, cell in enumerate(row):
                        cell_el = ET.SubElement(row_el, "cell", col=str(c_idx))
                        cell_el.text = (cell or "").strip()

            cropped = page
            for bbox in table_bboxes:
                try:
                    cropped = cropped.filter(
                        lambda obj, b=bbox: not (
                            obj.get("x0", 0) >= b[0]
                            and obj.get("top", 0) >= b[1]
                            and obj.get("x1", 0) <= b[2]
                            and obj.get("bottom", 0) <= b[3]
                        )
                    )
                except Exception:
                    pass

            txt = cropped.extract_text(x_tolerance=3, y_tolerance=3)
            if txt and txt.strip():
                text_el = ET.SubElement(page_el, "text")
                for line in txt.splitlines():
                    line = line.strip()
                    if line:
                        ET.SubElement(text_el, "line").text = line

    raw = ET.tostring(root, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ")


# ── Routes ───────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PDF → XML Converter</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0d0d0d; --surface: #161616; --border: #2a2a2a;
    --accent: #00e5a0; --accent-dim: #00e5a018;
    --text: #e8e8e8; --muted: #666; --danger: #ff4d4d; --warn: #f5a623;
  }
  body {
    background: var(--bg); color: var(--text);
    font-family: 'IBM Plex Sans', sans-serif;
    min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; padding: 48px 24px 80px;
  }
  header { width: 100%; max-width: 780px; margin-bottom: 48px;
    border-bottom: 1px solid var(--border); padding-bottom: 24px; }
  .logo { font-family: 'IBM Plex Mono', monospace; font-size: 11px;
    letter-spacing: 0.25em; color: var(--accent); text-transform: uppercase; margin-bottom: 12px; }
  h1 { font-size: 32px; font-weight: 300; letter-spacing: -0.02em; }
  h1 span { color: var(--accent); font-weight: 600; }
  .subtitle { font-size: 12px; color: var(--muted); margin-top: 8px;
    font-family: 'IBM Plex Mono', monospace; }
  main { width: 100%; max-width: 780px; }
  #dropzone {
    border: 1px dashed var(--border); background: var(--surface);
    padding: 52px 32px; text-align: center; cursor: pointer;
    transition: border-color 0.2s, background 0.2s; margin-bottom: 24px;
  }
  #dropzone.drag-over { border-color: var(--accent); background: var(--accent-dim); }
  .drop-icon { font-size: 40px; margin-bottom: 16px; display: block; }
  .drop-label { font-size: 16px; font-weight: 600; margin-bottom: 6px; }
  .drop-sub { font-size: 12px; color: var(--muted); font-family: 'IBM Plex Mono', monospace; }
  #file-input { display: none; }
  #queue { display: flex; flex-direction: column; gap: 8px; margin-bottom: 24px; }
  .file-row {
    background: var(--surface); border: 1px solid var(--border);
    border-left: 3px solid var(--border);
    padding: 12px 16px; display: flex; align-items: center; gap: 12px;
    font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  }
  .file-row.done { border-left-color: var(--accent); }
  .file-row.error { border-left-color: var(--danger); }
  .file-row.processing { border-left-color: var(--warn); }
  .file-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-size { color: var(--muted); flex-shrink: 0; }
  .file-status { flex-shrink: 0; font-size: 11px; min-width: 80px; text-align: right; }
  .file-status.ok { color: var(--accent); }
  .file-status.err { color: var(--danger); }
  .file-status.proc { color: var(--warn); }
  .dl-btn {
    background: var(--accent); color: #000; border: none;
    padding: 5px 14px; font-family: 'IBM Plex Mono', monospace;
    font-size: 11px; font-weight: 600; cursor: pointer; flex-shrink: 0;
  }
  .dl-btn:hover { background: #00ffb3; }
  .rm-btn {
    background: transparent; color: var(--muted); border: none;
    font-size: 14px; cursor: pointer; flex-shrink: 0; padding: 0 4px;
  }
  .rm-btn:hover { color: var(--danger); }
  .actions { display: flex; gap: 12px; margin-bottom: 28px; flex-wrap: wrap; }
  .btn { padding: 12px 28px; font-family: 'IBM Plex Mono', monospace;
    font-size: 12px; font-weight: 600; letter-spacing: 0.08em;
    text-transform: uppercase; border: none; cursor: pointer; }
  .btn-primary { background: var(--accent); color: #000; }
  .btn-primary:hover { background: #00ffb3; }
  .btn-primary:disabled { background: #2a2a2a; color: #444; cursor: not-allowed; }
  .btn-ghost { background: transparent; color: var(--muted); border: 1px solid var(--border); }
  .btn-ghost:hover { color: var(--text); border-color: #555; }
  .log-label { font-family: 'IBM Plex Mono', monospace; font-size: 10px;
    letter-spacing: 0.15em; color: var(--muted); text-transform: uppercase; margin-bottom: 8px; }
  #log { background: var(--surface); border: 1px solid var(--border);
    padding: 16px; font-family: 'IBM Plex Mono', monospace; font-size: 11px;
    color: var(--muted); max-height: 180px; overflow-y: auto; line-height: 2; }
  .log-ok { color: var(--accent); }
  .log-err { color: var(--danger); }
  .log-info { color: var(--warn); }
  .progress-bar-wrap { height: 2px; background: var(--border); margin-bottom: 24px; }
  .progress-bar { height: 2px; background: var(--accent); width: 0%; transition: width 0.3s; }
  .status-line { font-family: 'IBM Plex Mono', monospace; font-size: 11px;
    color: var(--muted); margin-bottom: 24px; }
</style>
</head>
<body>
<header>
  <div class="logo">Ground Ops Tooling · Local</div>
  <h1>PDF <span>→</span> XML</h1>
  <div class="subtitle">batch converter · runs locally · no data leaves your machine</div>
</header>
<main>
  <div id="dropzone" onclick="document.getElementById('file-input').click()">
    <span class="drop-icon">📄</span>
    <div class="drop-label">Drop PDF files here</div>
    <div class="drop-sub">or click to browse &nbsp;·&nbsp; multiple files supported</div>
    <input type="file" id="file-input" accept=".pdf" multiple>
  </div>

  <div id="queue"></div>

  <div class="progress-bar-wrap"><div class="progress-bar" id="progress"></div></div>
  <div class="status-line" id="status-line">No files loaded.</div>

  <div class="actions">
    <button class="btn btn-primary" id="convert-btn" disabled>Convert All</button>
    <button class="btn btn-ghost" id="dl-all-btn" disabled>⬇ Download All</button>
    <button class="btn btn-ghost" id="clear-btn">Clear</button>
  </div>

  <div class="log-label">Console</div>
  <div id="log"><span class="log-info">Ready. Add PDF files to begin.</span></div>
</main>

<script>
const files = [];  // {file, name, size, status, xmlBlob}

const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('file-input');
const queueEl  = document.getElementById('queue');
const convertBtn = document.getElementById('convert-btn');
const dlAllBtn   = document.getElementById('dl-all-btn');
const clearBtn   = document.getElementById('clear-btn');
const logEl      = document.getElementById('log');
const progressEl = document.getElementById('progress');
const statusLine = document.getElementById('status-line');

function log(msg, cls='') {
  const d = document.createElement('div');
  if (cls) d.className = 'log-' + cls;
  d.textContent = msg;
  logEl.appendChild(d);
  logEl.scrollTop = logEl.scrollHeight;
}

function fmt(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/1048576).toFixed(1) + ' MB';
}

function updateStatus() {
  const total = files.length;
  const done  = files.filter(f => f.status === 'done').length;
  const errs  = files.filter(f => f.status === 'error').length;
  statusLine.textContent = total === 0
    ? 'No files loaded.'
    : `${total} file(s) · ${done} converted · ${errs} error(s)`;
  progressEl.style.width = total ? (done / total * 100) + '%' : '0%';
  dlAllBtn.disabled = done === 0;
}

function renderQueue() {
  queueEl.innerHTML = '';
  files.forEach((f, i) => {
    const row = document.createElement('div');
    row.className = 'file-row ' + f.status;
    row.innerHTML = `
      <span class="file-name">${f.name}</span>
      <span class="file-size">${fmt(f.size)}</span>
      <span class="file-status ${f.status==='done'?'ok':f.status==='error'?'err':f.status==='processing'?'proc':''}">
        ${f.status==='pending'?'–':f.status==='processing'?'converting…':f.status==='done'?'✓ done':'✗ error'}
      </span>
      ${f.status==='done' ? `<button class="dl-btn" data-i="${i}">Download XML</button>` : ''}
      <button class="rm-btn" data-rm="${i}" title="Remove">✕</button>
    `;
    queueEl.appendChild(row);
  });
  queueEl.querySelectorAll('.dl-btn').forEach(b =>
    b.addEventListener('click', () => downloadOne(+b.dataset.i)));
  queueEl.querySelectorAll('.rm-btn').forEach(b =>
    b.addEventListener('click', () => { files.splice(+b.dataset.rm, 1); renderQueue(); updateStatus(); convertBtn.disabled = files.length===0; }));
  updateStatus();
}

dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('drag-over'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag-over'));
dropzone.addEventListener('drop', e => { e.preventDefault(); dropzone.classList.remove('drag-over'); addFiles([...e.dataTransfer.files]); });
fileInput.addEventListener('change', () => { addFiles([...fileInput.files]); fileInput.value=''; });

function addFiles(newFiles) {
  newFiles.filter(f => f.name.toLowerCase().endsWith('.pdf')).forEach(f => {
    if (!files.find(x => x.name === f.name && x.size === f.size))
      files.push({ file: f, name: f.name, size: f.size, status: 'pending', xmlBlob: null });
  });
  renderQueue();
  convertBtn.disabled = files.length === 0;
}

async function convertAll() {
  convertBtn.disabled = true;
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    if (f.status === 'done') continue;
    f.status = 'processing';
    renderQueue();
    log(`Converting ${f.name}…`, 'info');
    try {
      const fd = new FormData();
      fd.append('file', f.file, f.name);
      const res = await fetch('/convert', { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'Server error');
      f.xmlBlob = new Blob([data.xml], { type: 'application/xml' });
      f.status = 'done';
      log(`✓ ${f.name} converted (${data.pages} page(s))`, 'ok');
    } catch(e) {
      f.status = 'error';
      log(`✗ ${f.name}: ${e.message}`, 'err');
    }
    renderQueue();
  }
  convertBtn.disabled = false;
}

function downloadOne(i) {
  const f = files[i];
  if (!f.xmlBlob) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(f.xmlBlob);
  a.download = f.name.replace(/\.pdf$/i, '.xml');
  a.click();
}

async function downloadAll() {
  for (let i = 0; i < files.length; i++) {
    if (files[i].status === 'done') {
      downloadOne(i);
      await new Promise(r => setTimeout(r, 400));
    }
  }
}

convertBtn.addEventListener('click', convertAll);
dlAllBtn.addEventListener('click', downloadAll);
clearBtn.addEventListener('click', () => {
  files.length = 0; renderQueue(); convertBtn.disabled = true;
  log('Queue cleared.', 'info');
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/convert", methods=["POST"])
def convert():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Not a PDF"}), 400
    try:
        pdf_bytes = f.read()
        xml_str = pdf_bytes_to_xml(pdf_bytes, f.filename)
        # count pages
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = len(pdf.pages)
        return jsonify({"xml": xml_str, "pages": pages})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
