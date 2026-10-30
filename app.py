import os
import re
import io
import zipfile
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, send_file, jsonify
import concurrent.futures

app = Flask(__name__)
requests.packages.urllib3.disable_warnings()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

def fetch_url(session, url, timeout=10):
    try:
        r = session.get(url, verify=False, timeout=timeout)
        if r.status_code == 200:
            return r
    except:
        pass
    return None

def dump_to_zip(target_url):
    session = requests.Session()
    session.headers.update(HEADERS)
    
    zip_buffer = io.BytesIO()
    zip_file = zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED)
    
    # 1. Tải HTML gốc
    r = fetch_url(session, target_url)
    if not r:
        return None, "Không thể truy cập web. Kiểm tra lại link hoặc web chặn bot."
    
    host = urlparse(target_url).netloc
    html = r.text
    zip_file.writestr(f"{host}/index.html", html)
    
    # 2. Quét file tĩnh
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
        # Lọc SPA fallback
        if u.endswith('.js') and '<!DOCTYPE html>' in res.text[:100]: return
        
        parsed = urlparse(u)
        path = parsed.path.lstrip('/')
        if not path: path = 'index.txt'
        
        # Đảm bảo tên file hợp lệ trong zip
        safe_path = f"{parsed.netloc}/{path.split('?')[0]}"
        
        is_bin = not any(u.endswith(ext) for ext in ['.js', '.css', '.html', '.json', '.svg', '.map'])
        content = res.content if is_bin else res.text
        
        zip_file.writestr(safe_path, content)
        if u.endswith('.js'):
            js_files.append((u, res.text))

    # Tải song song
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        executor.map(download_and_zip, urls)

    # 3. Bóc Sourcemap
    for js_url, js_content in js_files:
        match = re.search(r'//[#@]\s*sourceMappingURL=([^\s]+)', js_content)
        if match:
            map_url = urljoin(js_url, match.group(1))
            res_map = fetch_url(session, map_url)
            if res_map:
                try:
                    map_data = res_map.json()
                    if 'sourcesContent' in map_data:
                        for idx, src in enumerate(map_data['sourcesContent']):
                            if src:
                                src_path = map_data['sources'][idx].replace('webpack://', '').replace('./', '')
                                src_path = re.sub(r'[<>:"|?*]', '_', src_path)
                                zip_file.writestr(f"{host}/sourcemap/{src_path}", src)
                except: pass

    # 4. Tìm API
    apis = set()
    regexes = [r'fetch\(["\']([^"\']+)["\']', r'axios\.\w+\(["\']([^"\']+)["\']', r'url:\s*["\']([^"\']+)["\']', r'["\'](\/api\/[^"\']+)["\']']
    for _, content in js_files:
        for rex in regexes:
            for m in re.findall(rex, content):
                if len(m) < 100 and not m.endswith(('.png','.jpg','.css')):
                    apis.add(m)
                    
    zip_file.writestr(f"{host}/API_ENDPOINTS.txt", '\n'.join(apis))
    
    zip_file.close()
    zip_buffer.seek(0)
    return zip_buffer, None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/dump', methods=['POST'])
def dump():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({"error": "Thiếu link URL"}), 400

    if not url.startswith("http"):
        url = "https://" + url

    try:
        zip_data, error = dump_to_zip(url)
        if error:
            return jsonify({"error": error}), 500
        
        host = urlparse(url).netloc
        filename = f"dump_{host}.zip"
        
        return send_file(
            zip_data,
            mimetype='application/zip',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Lấy port từ Render (mặc định 5000 nếu chạy local)
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
