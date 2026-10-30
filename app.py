import os
import re
import io
import json
import time
import zipfile
import ipaddress
import requests
import traceback
from urllib.parse import urljoin, urlparse, urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from flask import Flask, request, send_file, jsonify, Response
import socket

app = Flask(__name__)
requests.packages.urllib3.disable_warnings()

# ================= HTML (giữ nguyên UI, thêm note) =================
HTML_CODE = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Web Dumper</title>
<style>
body{font-family:system-ui,sans-serif;background:#0a0e1a;color:#fff;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;padding:20px;box-sizing:border-box}
.container{background:#111827;padding:40px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.5);width:100%;max-width:520px;text-align:center;border:1px solid #1f2937}
h1{margin-top:0;color:#4fc3f7;font-size:24px}
p{color:#9ca3af;font-size:14px;margin-bottom:25px}
input{width:100%;padding:12px;box-sizing:border-box;border-radius:8px;border:1px solid #374151;background:#1f2937;color:#fff;font-size:16px;margin-bottom:15px;outline:none}
input:focus{border-color:#4fc3f7}
button{width:100%;padding:14px;border:none;border-radius:8px;background:linear-gradient(135deg,#1976d2,#0288d1);color:#fff;font-size:16px;font-weight:bold;cursor:pointer;transition:.2s}
button:hover{transform:translateY(-2px);box-shadow:0 5px 15px rgba(25,118,210,.4)}
button:disabled{background:#4b5563;cursor:not-allowed;transform:none;box-shadow:none}
#status{margin-top:15px;font-size:14px;color:#fbbf24;display:none}
#status.error{color:#f87171}
</style>
</head>
<body>
<div class="container">
<h1>🚀 Web Dumper</h1>
<p>Quét HTML, JS, CSS, sourcemap, API, config sót → ZIP.</p>
<input type="text" id="urlInput" placeholder="VD: https://example.com" autocomplete="off">
<button id="dumpBtn" onclick="startDump()">📦 Tải File ZIP</button>
<div id="status">⏳ Đang quét...</div>
</div>
<script>
async function startDump(){
 const url=document.getElementById('urlInput').value.trim();
 const btn=document.getElementById('dumpBtn'),status=document.getElementById('status');
 if(!url){alert('Nhập link!');return}
 btn.disabled=true;status.style.display='block';status.className='';
 status.innerText='⏳ Đang quét, có thể mất 30s–2p...';
 try{
  const r=await fetch('/api/dump',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
  if(!r.ok){const e=await r.json();throw new Error(e.error||'Lỗi')}
  const b=await r.blob();
  const a=document.createElement('a');
  a.href=URL.createObjectURL(b);
  a.download=r.headers.get('Content-Disposition')?.match(/filename="?([^"]+)"?/)?.[1]||'dump.zip';
  document.body.appendChild(a);a.click();a.remove();
  status.innerText='✅ Xong!';
 }catch(e){status.className='error';status.innerText='❌ '+e.message}
 finally{btn.disabled=false}
}
</script>
</body>
</html>"""

# ================= CONFIG =================
MAX_FILE_BYTES   = 8 * 1024 * 1024        # 8MB / file
MAX_TOTAL_BYTES  = 200 * 1024 * 1024      # 200MB tổng
MAX_FILES        = 400
FETCH_TIMEOUT    = (5, 15)                # (connect, read)
WORKERS          = 8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ================= SSRF GUARD =================
def is_public_url(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ("http", "https"): return False
        host = p.hostname
        if not host: return False
        # hostname là IP trực tiếp
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local \
               or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                return False
            return True
        except ValueError:
            pass
        # resolve domain
        for fam, _, _, _, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local \
               or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                return False
        return True
    except Exception:
        return False

# ================= FETCH =================
def fetch(session, url, stream=False):
    if not is_public_url(url): return None, "blocked"
    try:
        r = session.get(url, verify=False, timeout=FETCH_TIMEOUT,
                        stream=stream, allow_redirects=True)
        if r.status_code != 200:
            return None, f"http_{r.status_code}"
        cl = r.headers.get("Content-Length")
        if cl and int(cl) > MAX_FILE_BYTES:
            return None, "too_big"
        return r, None
    except requests.exceptions.Timeout:
        return None, "timeout"
    except Exception as e:
        return None, str(e)[:60]

def read_body(r):
    """Đọc giới hạn MAX_FILE_BYTES."""
    buf = b""
    for chunk in r.iter_content(64 * 1024):
        buf += chunk
        if len(buf) > MAX_FILE_BYTES:
            break
    return buf[:MAX_FILE_BYTES]

def is_html(content_type, body_start):
    ct = (content_type or "").lower()
    if "text/html" in ct: return True
    head = body_start[:256].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")

# ================= URL EXTRACTORS =================
RE_JS_SRC   = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+\.(?:js|mjs)(?:\?[^"']*)?)["']""", re.I)
RE_CSS_SRC  = re.compile(r"""href\s*=\s*["']([^"']+\.css(?:\?[^"']*)?)["']""", re.I)
RE_MAP_TAIL = re.compile(r"""//[#@]\s*sourceMappingURL=([^\s'"]+)""")
RE_IMPORT   = re.compile(r"""(?:import\s*\(|require\s*\()\s*["']([^"']+\.(?:js|mjs))["']""")
RE_CSS_URL  = re.compile(r"""url\(\s*["']?([^"')\s]+)["']?\s*\)""")
RE_APIS = [
    re.compile(r"""fetch\s*\(\s*[`'"]([^`'"]+)[`'"]"""),
    re.compile(r"""axios\s*\.\s*(?:get|post|put|delete|patch|request)\s*\(\s*[`'"]([^`'"]+)[`'"]"""),
    re.compile(r"""\burl\s*:\s*[`'"]([^`'"]+)[`'"]"""),
    re.compile(r"""["'`](/api/[^"'`\s]+)["'`]"""),
    re.compile(r"""["'`](https?://[^"'`\s]+/api/[^"'`\s]+)["'`]"""),
]

def extract_urls(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    urls = set()

    # <base href>
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = urljoin(base_url, base_tag["href"])

    for tag, attr in [("script","src"),("link","href"),("img","src"),
                      ("iframe","src"),("source","src"),("video","src"),
                      ("audio","src"),("form","action")]:
        for el in soup.find_all(tag):
            v = el.get(attr)
            if v and not v.startswith(("data:","javascript:","mailto:","#","blob:")):
                urls.add(urljoin(base_url, v))

    # regex fallback — bắt những gì soup bỏ sót
    raw = html
    for rx in (RE_JS_SRC, RE_CSS_SRC):
        for m in rx.findall(raw):
            if not m.startswith(("data:","javascript:","#")):
                urls.add(urljoin(base_url, m))

    # API endpoints inline trong HTML
    for rx in RE_APIS:
        for m in rx.findall(raw):
            pass  # giữ lại cho bước sau, không fetch

    return urls, base_url

def extract_from_js(js_text, js_url):
    urls = set()
    for m in RE_IMPORT.findall(js_text):
        if m.startswith((".", "/")):
            urls.add(urljoin(js_url, m))
    return urls

def extract_css_assets(css_text, css_url):
    urls = set()
    for m in RE_CSS_URL.findall(css_text):
        if not m.startswith(("data:","#","http")):
            urls.add(urljoin(css_url, m))
    return urls

# ================= SOURCEMAP =================
def unpack_sourcemap(map_url, map_text, host, zip_file):
    try:
        data = json.loads(map_text)
    except Exception:
        return
    sources   = data.get("sources", [])
    contents  = data.get("sourcesContent", [])
    for i, src in enumerate(contents):
        if not src: continue
        path = sources[i] if i < len(sources) else f"unknown_{i}.ts"
        path = path.replace("webpack://", "").replace("vite://", "").lstrip("./")
        path = re.sub(r'[<>:"|?*]', "_", path)
        zip_file.writestr(f"{host}/sourcemap/{path}", src)

# ================= SNIFF WELL-KNOWN =================
SNIFF_PATHS = [
    "/robots.txt", "/sitemap.xml", "/manifest.json", "/sw.js",
    "/.well-known/security.txt",
    "/.git/HEAD", "/.git/config",
    "/.env", "/.env.local", "/.env.production",
    "/package.json", "/composer.json",
    "/backup.zip", "/www.zip", "/site.zip",
    "/.DS_Store", "/config.php.bak",
    "/.htaccess", "/web.config",
]

def probe_wellknown(session, base_url, host, zip_file):
    hits = []
    for path in SNIFF_PATHS:
        u = urljoin(base_url, path)
        r, err = fetch(session, u)
        if not r: continue
        body = read_body(r)
        if not body: continue
        # loại false positive: HTML 200 trả về cho mọi path
        if path in ("/.env", "/.git/HEAD", "/package.json") \
           and body[:64].lstrip().lower().startswith(b"<!doctype"):
            continue
        safe = path.lstrip("/").replace("/", "_")
        zip_file.writestr(f"{host}/_wellknown/{safe}", body)
        hits.append(f"{path}  [{len(body)} bytes]")
    return hits

# ================= MAIN DUMP =================
def dump_to_zip(target_url):
    session = requests.Session()
    session.headers.update(HEADERS)

    zip_buffer = io.BytesIO()
    zip_file   = zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED)
    lock       = __import__("threading").Lock()
    total_bytes = [0]
    file_count  = [0]
    report_lines = []

    def safe_write(path, data):
        with lock:
            if file_count[0] >= MAX_FILES:
                return False
            if isinstance(data, str):
                data = data.encode("utf-8", "replace")
            if total_bytes[0] + len(data) > MAX_TOTAL_BYTES:
                return False
            total_bytes[0] += len(data)
            file_count[0]  += 1
            zip_file.writestr(path, data)
            return True

    # ---- 1. Fetch trang chủ ----
    r, err = fetch(session, target_url)
    if not r:
        return None, f"Không fetch được trang chủ: {err}"
    home_body = read_body(r)
    home_ct   = r.headers.get("Content-Type", "")
    try:    html = home_body.decode(r.encoding or "utf-8", "replace")
    except: html = home_body.decode("utf-8", "replace")

    host = urlparse(target_url).netloc.replace(":", "_")
    safe_write(f"{host}/index.html", home_body)
    report_lines.append(f"== {target_url}")
    report_lines.append(f"time        : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append(f"status      : {r.status_code}")
    report_lines.append(f"server      : {r.headers.get('Server','-')}")
    report_lines.append(f"powered-by  : {r.headers.get('X-Powered-By','-')}")
    report_lines.append(f"content-type: {home_ct}")

    # ---- 2. Extract URLs ----
    urls, base_url = extract_urls(html, r.url)
    if base_url != r.url:
        report_lines.append(f"base        : {base_url}")

    # ---- 3. Worker fetch ----
    fetched_js  = []  # (url, text)
    fetched_css = []
    failed      = []
    visited     = set()

    def worker(u):
        if u in visited: return None
        visited.add(u)
        res, err = fetch(session, u)
        if not res:
            return ("fail", u, err)
        body = read_body(res)
        if not body:
            return ("fail", u, "empty")
        ct = res.headers.get("Content-Type", "")

        if u.split("?")[0].endswith((".js", ".mjs")):
            if is_html(ct, body):
                return ("fail", u, "html-not-js")
            text = body.decode(res.encoding or "utf-8", "replace")
            return ("js", u, text, body)
        if u.split("?")[0].endswith(".css"):
            text = body.decode(res.encoding or "utf-8", "replace")
            return ("css", u, text, body)
        return ("other", u, None, body, ct)

    tasks = list(urls)[:MAX_FILES]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(worker, u): u for u in tasks}
        for fut in as_completed(futures):
            try: res = fut.result()
            except Exception as e:
                failed.append((futures[fut], str(e))); continue
            if not res: continue
            kind = res[0]
            if kind == "fail":
                failed.append((res[1], res[2])); continue
            u = res[1]
            path = urlparse(u).path.lstrip("/") or "index.txt"
            safe_path = f"{host}/{path.split('?')[0]}"

            if kind == "js":
                _, _, text, body = res
                if safe_write(safe_path, body):
                    fetched_js.append((u, text))
            elif kind == "css":
                _, _, text, body = res
                if safe_write(safe_path, body):
                    fetched_css.append((u, text))
            else:
                _, _, _, body, ct = res
                safe_write(safe_path, body)

    # ---- 4. CSS → assets ----
    css_assets = set()
    for cu, ct in fetched_css:
        css_assets |= extract_css_assets(ct, cu)
    css_assets -= visited
    if css_assets:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for u, res in zip(css_assets, pool.map(lambda x: fetch(session, x)[0], list(css_assets)[:100])):
                if res:
                    body = read_body(res)
                    path = urlparse(u).path.lstrip("/") or "asset.bin"
                    safe_write(f"{host}/{path.split('?')[0]}", body)

    # ---- 5. JS → dynamic imports + sourcemap ----
    extra_js = set()
    for ju, jt in fetched_js:
        extra_js |= extract_from_js(jt, ju)
    extra_js -= visited
    if extra_js:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(lambda x: (x, fetch(session, x)), list(extra_js)[:80]))
        for u, (res, _e) in results:
            if not res: continue
            body = read_body(res)
            path = urlparse(u).path.lstrip("/") or "chunk.js"
            if safe_write(f"{host}/{path}", body):
                fetched_js.append((u, body.decode("utf-8", "replace")))

    # ---- 6. Sourcemaps ----
    sm_count = 0
    for ju, jt in fetched_js:
        m = RE_MAP_TAIL.search(jt)
        if not m: continue
        map_url = urljoin(ju, m.group(1))
        res, _e = fetch(session, map_url)
        if not res: continue
        body = read_body(res)
        try:
            unpack_sourcemap(map_url, body.decode("utf-8", "replace"), host, zip_file)
            sm_count += 1
        except Exception:
            pass
    report_lines.append(f"sourcemaps  : {sm_count} unpacked")

    # ---- 7. Well-known sniff ----
    hits = probe_wellknown(session, base_url, host, zip_file)
    if hits:
        report_lines.append("well-known  :")
        for h in hits: report_lines.append(f"  + {h}")

    # ---- 8. API endpoints ----
    apis = set()
    for _ju, jt in fetched_js:
        for rx in RE_APIS:
            for m in rx.findall(jt):
                if len(m) < 200 and not m.endswith((".png",".jpg",".jpeg",".gif",".svg",".css",".woff",".woff2",".ico")):
                    apis.add(m)
    for rx in RE_APIS:
        for m in rx.findall(html):
            apis.add(m)

    if apis:
        safe_write(f"{host}/API_ENDPOINTS.txt", "\n".join(sorted(apis)))

    # ---- 9. Report ----
    report_lines.append(f"files       : {file_count[0]}")
    report_lines.append(f"total bytes : {total_bytes[0]}")
    report_lines.append(f"api count   : {len(apis)}")
    if failed:
        report_lines.append(f"failed      : {len(failed)}")
        for u, e in failed[:30]:
            report_lines.append(f"  - {u}  ({e})")
    report_lines.append(f"truncated   : {'YES' if file_count[0]>=MAX_FILES or total_bytes[0]>=MAX_TOTAL_BYTES else 'no'}")
    safe_write(f"{host}/_SUMMARY.txt", "\n".join(report_lines))

    zip_file.close()
    zip_buffer.seek(0)
    return zip_buffer, None

# ================= ROUTES =================
@app.route("/")
def index():
    return Response(HTML_CODE, mimetype="text/html")

@app.route("/api/dump", methods=["POST"])
def dump():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Thiếu URL"}), 400
    if not url.startswith(("http://","https://")):
        url = "https://" + url
    if not is_public_url(url):
        return jsonify({"error": "URL bị chặn (private/loopback/internal)"}), 400

    try:
        zip_data, error = dump_to_zip(url)
        if error:
            return jsonify({"error": error}), 500
        host = urlparse(url).netloc.replace(":", "_")
        return send_file(zip_data, mimetype="application/zip",
                         as_attachment=True,
                         download_name=f"dump_{host}_{int(time.time())}.zip")
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Lỗi server: " + traceback.format_exc(limit=2)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
