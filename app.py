import os
import re
import io
import zipfile
import requests
import traceback
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from flask import Flask, request, send_file, jsonify, Response
import concurrent.futures

app = Flask(__name__)
requests.packages.urllib3.disable_warnings()

# ================= HTML CODE (Giao diện) =================
HTML_CODE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VIP Web Dumper</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0a0e1a; color: #fff; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .container { background: #111827; padding: 40px; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); width: 100%; max-width: 500px; text-align: center; border: 1px solid #1f2937; }
        h1 { margin-top: 0; color: #4fc3f7; font-size: 24px; }
        p { color: #9ca3af; font-size: 14px; margin-bottom: 25px; }
        input { width: 100%; padding: 12px; box-sizing: border-box; border-radius: 8px; border: 1px solid #374151; background: #1f2937; color: #fff; font-size: 16px; margin-bottom: 15px; outline: none; }
        input:focus { border-color: #4fc3f7; }
        button { width: 100%; padding: 14px; border: none; border-radius: 8px; background: linear-gradient(135deg, #1976d2, #0288d1); color: white; font-size: 16px; font-weight: bold; cursor: pointer; transition: 0.2s; }
        button:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(25, 118, 210, 0.4); }
        button:disabled { background: #4b5563; cursor: not-allowed; transform: none; box-shadow: none; }
        #status { margin-top: 15px; font-size: 14px; color: #fbbf24; display: none; }
        #status.error { color: #f87171; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 VIP Web Dumper</h1>
        <p>Dán link web cần lấy mã nguồn. Hệ thống sẽ nén HTML, JS, CSS, Sourcemap và API lại thành file ZIP.</p>
        
        <input type="text" id="urlInput" placeholder="VD: https://example.com" autocomplete="off">
        <button id="dumpBtn" onclick="startDump()">📦 Tải File ZIP</button>
        
        <div id="status">⏳ Đang quét và tải dữ liệu, vui lòng đợi...</div>
    </div>

    <script>
        async function startDump() {
            const url = document.getElementById('urlInput').value.trim();
            const btn = document.getElementById('dumpBtn');
            const status = document.getElementById('status');

            if (!url) { alert('Vui lòng nhập link web!'); return; }

            btn.disabled = true;
            status.style.display = 'block';
            status.className = '';
            status.innerText = '⏳ Đang quét và tải dữ liệu, vui lòng đợi (có thể mất 30s-1p)...';

            try {
                const response = await fetch('/api/dump', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: url })
                });

                if (!response.ok) {
                    const err = await response.json();
                    throw new Error(err.error || 'Lỗi không xác định');
                }

                const blob = await response.blob();
                const downloadUrl = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = downloadUrl;
                a.download = 'dump.zip';
                document.body.appendChild(a);
                a.click();
                a.remove();
                status.innerText = '✅ Tải thành công!';
            } catch (error) {
                status.className = 'error';
                status.innerText = '❌ ' + error.message;
            } finally {
                btn.disabled = false;
            }
        }
    </script>
</body>
</html>
"""

# ================= LOGIC DUMPER =================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

def fetch_url(session, url, timeout=10):
    try:
        r = session.get(url, verify=False, timeout=timeout)
        if r.status_code == 200: return r
    except: pass
    return None

def dump_to_zip(target_url):
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        zip_buffer = io.BytesIO()
        zip_file = zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED)
        
        r = fetch_url(session, target_url)
        if not r: return None, "Không thể truy cập web."
        
        host = urlparse(target_url).netloc
        html = r.text
        zip_file.writestr(f"{host}/index.html", html)
        
        soup = BeautifulSoup(html, 'html.parser')
        urls = set()
        for tag, attr in [('script', 'src'), ('link', 'href'), ('img', 'src'), ('iframe', 'src')]:
            for el in soup.find_all(tag):
                val = el.get(attr)
                if val: urls.add(urljoin(target_url, val))
                
        for script in soup.find_all('script', string=True):
            matches = re.findall(r'["\']((?:https?://|/)[^"\']+\.(?:js|css|json|png|jpg|svg|map))["\']', script.string)
            for m in matches: urls.add(urljoin(target_url, m))

        js_files = []
        def download_and_zip(u):
            res = fetch_url(session, u)
            if not res: return
            if u.endswith('.js') and '<!DOCTYPE html>' in res.text[:100]: return
            
            parsed = urlparse(u)
            path = parsed.path.lstrip('/')
            if not path: path = 'index.txt'
            safe_path = f"{parsed.netloc}/{path.split('?')[0]}"
            
            is_bin = not any(u.endswith(ext) for ext in ['.js', '.css', '.html', '.json', '.svg', '.map'])
            if is_bin: zip_file.writestr(safe_path, res.content)
            else: zip_file.writestr(safe_path, res.text)
                
            if u.endswith('.js'): js_files.append((u, res.text))

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(download_and_zip, urls)

        for js_url, js_content in js_files:
            try:
                match = re.search(r'//[#@]\s*sourceMappingURL=([^\s]+)', js_content)
                if match:
                    map_url = urljoin(js_url, match.group(1))
                    res_map = fetch_url(session, map_url)
                    if res_map:
                        map_data = res_map.json()
                        if 'sourcesContent' in map_data:
                            for idx, src in enumerate(map_data['sourcesContent']):
                                if src:
                                    src_path = map_data['sources'][idx].replace('webpack://', '').replace('./', '')
                                    src_path = re.sub(r'[<>:"|?*]', '_', src_path)
                                    zip_file.writestr(f"{host}/sourcemap/{src_path}", src)
            except: pass

        apis = set()
        regexes = [r'fetch\(["\']([^"\']+)["\']', r'axios\.\w+\(["\']([^"\']+)["\']', r'url:\s*["\']([^"\']+)["\']', r'["\'](\/api\/[^"\']+)["\']']
        for _, content in js_files:
            for rex in regexes:
                for m in re.findall(rex, content):
                    if len(m) < 100 and not m.endswith(('.png','.jpg','.css')): apis.add(m)
        zip_file.writestr(f"{host}/API_ENDPOINTS.txt", '\n'.join(apis))
        
        zip_file.close()
        zip_buffer.seek(0)
        return zip_buffer, None
    except Exception as e:
        return None, f"Lỗi: {str(e)}"

# ================= ROUTES =================
@app.route('/')
def index():
    # Trả thẳng HTML, không cần thư mục templates
    return Response(HTML_CODE, mimetype='text/html')

@app.route('/api/dump', methods=['POST'])
def dump():
    data = request.json
    if not data or 'url' not in data:
        return jsonify({"error": "Thiếu link URL"}), 400
    url = data.get('url')
    if not url.startswith("http"): url = "https://" + url

    try:
        zip_data, error = dump_to_zip(url)
        if error: return jsonify({"error": error}), 500
        
        host = urlparse(url).netloc
        return send_file(zip_data, mimetype='application/zip', as_attachment=True, download_name=f"dump_{host}.zip")
    except Exception as e:
        return jsonify({"error": f"Lỗi Server: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
