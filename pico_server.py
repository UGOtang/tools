from machine import UART, Pin
import time
import os
import json
import gc

# ========== 配置区域 ==========
WIFI_SSID = "wifi_name"
WIFI_PASSWORD = "wifi_pass"
SERVER_PORT = 80
UPLOAD_DIR = "/uploads"  

# 硬件引脚
UART_ID = 1
TX_PIN = Pin(4)   # GP4 -> ESP RX
RX_PIN = Pin(5)   # GP5 -> ESP TX
BAUD_RATE = 115200
RX_BUF_SIZE = 4096

# ========== HTML 网页模板（纯内存驻留） ==========
INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Pico 文件服务器</title>
    <style>
        body { font-family: sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background-color: #f0f2f5;}
        h1 { color: #333; }
        #drop-zone { border: 2px dashed #bbb; border-radius: 12px; padding: 40px; text-align: center; background: #fff; margin-bottom: 20px; transition: 0.3s; box-shadow: 0 2px 8px rgba(0,0,0,0.05);}
        #drop-zone.dragover { background: #e8f5e9; border-color: #4caf50; }
        button { padding: 10px 20px; font-size: 16px; cursor: pointer; border: none; border-radius: 6px; background-color: #1890ff; color: white; transition: 0.2s;}
        button:hover { background-color: #40a9ff; }
        #file-list { list-style: none; padding: 0; background: #fff; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); overflow: hidden;}
        #file-list li { display: flex; justify-content: space-between; padding: 15px 20px; border-bottom: 1px solid #eee; align-items: center;}
        #file-list li:last-child { border-bottom: none; }
        .file-name { font-weight: 500; color: #555;}
        .file-actions a { margin-left: 15px; text-decoration: none; font-size: 14px; font-weight: bold;}
        .btn-download { color: #1890ff; }
        .btn-delete { color: #f5222d; }
    </style>
</head>
<body>
    <h1>📁 Pico 极速文件网盘</h1>
    <div id="drop-zone">
        <p style="color: #666; font-size: 16px;">拖拽文件到这里上传</p>
        <p style="color: #999;">或</p>
        <input type="file" id="file-input" multiple style="display:none">
        <button id="upload-btn">选择文件</button>
    </div>
    <h2>文件列表</h2>
    <ul id="file-list">加载中...</ul>
    <script>
        const dropZone = document.getElementById('drop-zone');
        const fileInput = document.getElementById('file-input');
        const uploadBtn = document.getElementById('upload-btn');
        const fileList = document.getElementById('file-list');
        
        dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
        dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
        dropZone.addEventListener('drop', e => {
            e.preventDefault(); dropZone.classList.remove('dragover');
            handleFiles(e.dataTransfer.files);
        });
        uploadBtn.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', e => { handleFiles(e.target.files); fileInput.value = ''; });
        
        function handleFiles(files) {
            for (const file of files) {
                const fd = new FormData(); fd.append('file', file);
                fetch('/upload', {method: 'POST', body: fd})
                    .then(res => res.ok ? loadFileList() : alert('上传失败（可能文件过大）'))
                    .catch(() => alert('网络中断，上传出错'));
            }
        }
        
        function deleteFile(filename) {
            if (!confirm(`确定要删除 "${decodeURIComponent(filename)}" 吗？`)) return;
            fetch('/delete?file=' + filename, {method: 'DELETE'})
                .then(res => {
                    if (res.ok) loadFileList();
                    else alert('删除失败');
                }).catch(() => alert('网络错误'));
        }

        function loadFileList() {
            fetch('/list').then(r => r.json()).then(files => {
                fileList.innerHTML = files.length ? files.map(f => `<li>
                    <span class="file-name">📄 ${f.name} <span style="color:#aaa; font-size: 12px;">(${(f.size/1024).toFixed(2)} KB)</span></span>
                    <span class="file-actions">
                        <a href="/download?file=${encodeURIComponent(f.name)}" download class="btn-download">下载</a>
                        <a href="#" onclick="deleteFile('${encodeURIComponent(f.name)}'); return false;" class="btn-delete">删除</a>
                    </span>
                </li>`).join('') : '<li style="color:#999; justify-content: center;">网盘是空的，快去上传文件吧！</li>';
            });
        }
        loadFileList();
    </script>
</body>
</html>"""

# ========== 初始化 UART ==========
uart = UART(UART_ID, baudrate=BAUD_RATE, tx=TX_PIN, rx=RX_PIN, rxbuf=RX_BUF_SIZE)

def send_at(cmd, wait=1, silent=False):
    uart.write(cmd + '\r\n')
    time.sleep(wait)
    resp = ''
    while uart.any():
        chunk = uart.read()
        if chunk:
            try: resp += chunk.decode('utf-8', 'ignore')
            except: resp += str(chunk)
    if not silent and resp:
        print(f"[AT] {cmd}\n-> {resp.strip()}")
    return resp

def connect_wifi():
    print("--- Wi-Fi 连接检查 ---")
    resp = send_at('AT+CIPSTATUS', wait=1, silent=True)
    if 'STATUS:2' in resp or 'STATUS:3' in resp:
        print("ESP-01S 已连接到 Wi-Fi")
        send_at('AT+CIFSR', silent=False)
        return True
    print("未连接 Wi-Fi，开始连接...")
    if 'OK' not in send_at('AT'): return False
    send_at('AT+CWMODE=1')
    uart.write(f'AT+CWJAP="{WIFI_SSID}","{WIFI_PASSWORD}"\r\n')
    start = time.time()
    while time.time() - start < 15:
        if uart.any():
            chunk = uart.read()
            if chunk:
                try: decoded = chunk.decode('utf-8', 'ignore')
                except: decoded = str(chunk)
                if 'WIFI GOT IP' in decoded or 'OK' in decoded:
                    send_at('AT+CIFSR')
                    return True
        time.sleep(0.5)
    return False

def start_server():
    send_at('AT+CIPSERVER=0', wait=1, silent=True)
    time.sleep(1)
    if 'OK' not in send_at('AT+CIPMUX=1', wait=1): return False
    return 'OK' in send_at(f'AT+CIPSERVER=1,{SERVER_PORT}', wait=2)

def accept_client(timeout=30):
    """【字节级提速】去除 decode 负担，直接用 Byte 捕获请求"""
    start = time.time()
    while time.time() - start < timeout:
        if uart.any():
            line = uart.readline() 
            if line and b'+IPD,' in line:
                try:
                    ipd_start = line.find(b'+IPD,')
                    colon_idx = line.find(b':', ipd_start)
                    if colon_idx != -1:
                        header_part = line[ipd_start:colon_idx].decode('utf-8')
                        link_id = int(header_part.split(',')[1])
                        return link_id, line[colon_idx+1:]
                except: pass
        time.sleep(0.02)
    return None, b''

def clean_ipd(data):
    """剔除混入文件流中的 AT 杂质"""
    idx = 0
    while True:
        idx = data.find(b'+IPD,', idx)
        if idx == -1: break
        colon_idx = data.find(b':', idx)
        if colon_idx != -1 and (colon_idx - idx) < 20:
            start_cut = idx
            if idx >= 2 and data[idx-2:idx] == b'\r\n': start_cut = idx - 2
            data = data[:start_cut] + data[colon_idx+1:]
            idx = start_cut
        else:
            idx += 5
    return data

def recv_data(link_id, initial_data, timeout=3.0):
    """【绝杀级提速】精准嗅探请求体长度，0.01秒瞬间跳出死等循环"""
    data = bytearray(initial_data)
    start = time.time()
    expected_body_len = -1
    header_len = -1
    
    while time.time() - start < timeout:
        # 1. 嗅探 HTTP 报头
        if header_len == -1:
            idx = data.find(b'\r\n\r\n')
            if idx != -1:
                header_len = idx + 4
                headers = data[:idx].upper()
                
                # 核心：发现是 GET / DELETE 请求（无 Body），毫秒级极速退出！
                if b'GET ' in headers[:20] or b'DELETE ' in headers[:20] or b'OPTIONS ' in headers[:20]:
                    return clean_ipd(bytes(data))
                    
                # 核心：对于 POST (上传文件)，计算期望长度
                cl_idx = headers.find(b'CONTENT-LENGTH:')
                if cl_idx != -1:
                    cl_end = headers.find(b'\r\n', cl_idx)
                    if cl_end == -1: cl_end = len(headers)
                    try: expected_body_len = int(headers[cl_idx+15:cl_end].strip())
                    except: pass

        # 2. 检测 POST 主体是否接收完毕（上传文件完成瞬间跳出，无需傻等2秒超时！）
        if header_len != -1 and expected_body_len != -1:
            if len(data) >= header_len + expected_body_len:
                cleaned = clean_ipd(bytes(data))
                if len(cleaned) >= header_len + expected_body_len:
                    return cleaned

        if uart.any():
            chunk = uart.read()
            if chunk:
                data.extend(chunk)
                start = time.time()
        else:
            time.sleep(0.01)
            
    return clean_ipd(bytes(data))

def _send_data(link_id, data):
    """【AT发送大提速】增大管道至 1024 字节，去除解码损耗"""
    if not data: return True
    CHUNK_SIZE = 1024 
    for i in range(0, len(data), CHUNK_SIZE):
        chunk = data[i:i+CHUNK_SIZE]
        uart.write(f'AT+CIPSEND={link_id},{len(chunk)}\r\n')
        
        # 纯 Byte 等待，摆脱 CPU 解码阻塞
        got_prompt = False
        start = time.time()
        received = b''
        while time.time() - start < 3:
            if uart.any():
                received += uart.read()
                if b'>' in received: 
                    got_prompt = True; break
            else: time.sleep(0.005)
        if not got_prompt: return False
            
        # 丝滑发包，每次 128 字节，避免拥堵
        w_idx = 0
        while w_idx < len(chunk):
            sz = min(128, len(chunk) - w_idx)
            uart.write(chunk[w_idx:w_idx+sz])
            w_idx += sz
            time.sleep(0.01) 
            
        send_ok = False
        start = time.time()
        received_after = b''
        while time.time() - start < 5:
            if uart.any():
                received_after += uart.read()
                if b'SEND OK' in received_after: send_ok = True; break
                if b'ERROR' in received_after or b'CLOSED' in received_after: return False
            else: time.sleep(0.005)
        if not send_ok: return False
    return True

def send_response(link_id, content, content_type='text/html'):
    if isinstance(content, str): content = content.encode()
    header = (
        "HTTP/1.1 200 OK\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(content)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    _send_data(link_id, header + content)

def send_error(link_id, code, message):
    body = f"<h1>{code} {message}</h1>"
    header = f"HTTP/1.1 {code} {message}\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    _send_data(link_id, (header + body).encode())

def extract_http_body(raw_data):
    methods =[b'GET ', b'POST ', b'PUT ', b'DELETE ', b'OPTIONS ']
    for method in methods:
        idx = raw_data.find(method)
        if idx != -1: return raw_data[idx:]
    return raw_data

def close_client(link_id):
    """【秒级挂断】取消原代码长达 0.2s 的死等"""
    uart.write(f'AT+CIPCLOSE={link_id}\r\n')
    time.sleep(0.05)

def unquote(string):
    res = bytearray()
    i = 0
    while i < len(string):
        if string[i] == '%' and i + 2 < len(string):
            try:
                res.append(int(string[i+1:i+3], 16))
                i += 3; continue
            except: pass
        res.append(ord(string[i]))
        i += 1
    return res.decode('utf-8', 'ignore')

def handle_upload(link_id, http_data):
    header_end = http_data.find(b'\r\n\r\n')
    if header_end == -1: return send_error(link_id, 400, 'No Body')
    headers, body = http_data[:header_end], http_data[header_end+4:]
    
    # 【健壮性提速】纯 Bytes 级解析，不再拆分换行导致低效
    boundary = None
    idx = headers.find(b'boundary=')
    if idx != -1:
        b_end = headers.find(b'\r\n', idx)
        if b_end == -1: b_end = len(headers)
        boundary = headers[idx+9:b_end].strip()
        
    if not boundary: return send_error(link_id, 400, 'No Boundary')
    
    sections = body.split(b'--' + boundary)
    for section in sections:
        if b'form-data' not in section: continue
        sec_header_end = section.find(b'\r\n\r\n')
        if sec_header_end != -1:
            sec_headers, file_data = section[:sec_header_end], section[sec_header_end+4:]
            if file_data.endswith(b'\r\n'): file_data = file_data[:-2]
            
            filename = None
            fn_start = sec_headers.find(b'filename="')
            if fn_start != -1:
                fn_end = sec_headers.find(b'"', fn_start + 10)
                filename = sec_headers[fn_start+10:fn_end].decode('utf-8', 'ignore')
            
            if filename:
                try:
                    with open(UPLOAD_DIR + '/' + filename, 'wb') as f: f.write(file_data)
                    print(f"✅ 上传成功: {filename} ({len(file_data)} 字节)")
                    return send_response(link_id, '{"status":"ok"}', 'application/json')
                except Exception as e:
                    print("❌ 保存失败:", e)
                    return send_error(link_id, 500, 'Write Failed')
    send_error(link_id, 400, 'Upload Failed')

def handle_delete(link_id, path):
    query_start = path.find('?')
    if query_start == -1: return send_error(link_id, 400, 'Bad Request')
    query = path[query_start+1:]
    
    if query.startswith('file='):
        filename = unquote(query[5:])
        if '..' in filename or '/' in filename: return send_error(link_id, 403, 'Forbidden')
            
        try:
            os.remove(UPLOAD_DIR + '/' + filename)
            print(f"🗑️ 已删除: {filename}")
            send_response(link_id, '{"status":"ok"}', 'application/json')
        except OSError:
            send_error(link_id, 404, 'File Not Found')
    else: send_error(link_id, 400, 'Bad Request')

def handle_request(link_id, raw_data):
    http_data = extract_http_body(raw_data)
    header_end = http_data.find(b'\r\n\r\n')
    headers_bytes = http_data[:header_end] if header_end != -1 else http_data
    
    try: headers_str = headers_bytes.decode('utf-8', 'ignore')
    except: return send_error(link_id, 400, 'Bad Request')
        
    lines = headers_str.split('\r\n')
    if not lines: return
    
    parts = lines[0].split(' ')
    if len(parts) < 2: return send_error(link_id, 400, 'Bad Request')
    method, path = parts[0], parts[1]
    print(f"[{method}] {path}")

    try:
        if method == 'GET' and (path == '/' or path == '/index.html'): 
            send_response(link_id, INDEX_HTML, 'text/html')
        elif method == 'GET' and path == '/list': serve_file_list(link_id)
        elif method == 'GET' and path.startswith('/download'): serve_download(link_id, path)
        elif method == 'DELETE' and path.startswith('/delete'): handle_delete(link_id, path)
        elif method == 'POST' and path == '/upload': handle_upload(link_id, http_data)
        else: send_error(link_id, 404, 'Not Found')
    except Exception as e:
        print(f"内部错误: {e}")
        send_error(link_id, 500, 'Internal Server Error')

def serve_file_list(link_id):
    files =[]
    try:
        for entry in os.ilistdir(UPLOAD_DIR):
            name = entry[0]
            if entry[1] == 0x8000: # 快速探测，无视系统隐藏卷标
                size = entry[3] if len(entry) > 3 else 0
                files.append({'name': name, 'size': size})
    except: pass
    send_response(link_id, json.dumps(files), 'application/json')

def serve_download(link_id, path):
    query_start = path.find('?')
    if query_start == -1: return send_error(link_id, 400, 'Bad Request')
    query = path[query_start+1:]
    if query.startswith('file='):
        filename = unquote(query[5:])
        file_path = UPLOAD_DIR + '/' + filename
        try:
            # 【流式下载防护】再大的文件也不会把 Pico 撑爆！
            file_sz = os.stat(file_path)[6]
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/octet-stream\r\n"
                f"Content-Disposition: attachment; filename=\"{filename}\"\r\n"
                f"Content-Length: {file_sz}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            
            if not _send_data(link_id, header): return
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(1024)
                    if not chunk: break
                    if not _send_data(link_id, chunk): break
        except: send_error(link_id, 404, 'File Not Found')
    else: send_error(link_id, 400, 'Bad Request')

def init_filesystem():
    try: os.mkdir(UPLOAD_DIR)
    except: pass

def get_ip():
    resp = send_at('AT+CIFSR', silent=True)
    for line in resp.split('\r\n'):
        if line.startswith('+CIFSR:STAIP,'):
            return line.split(',')[1].strip('"')
    return "未知"

def main():
    print("\n===== Pico 极速文件服务器 =====")
    init_filesystem()
    if not connect_wifi(): return print("WiFi 连接失败")
    if not start_server(): return print("服务器启动失败")
    print(f"\n🚀 服务器已启动！请在浏览器访问: http://{get_ip()}:{SERVER_PORT}\n")
    
    while True:
        gc.collect() 
        link_id, initial_data = accept_client(timeout=3600)
        if link_id is not None:
            raw_data = recv_data(link_id, initial_data, timeout=3.0)
            if raw_data:
                handle_request(link_id, raw_data)
            close_client(link_id)
        time.sleep(0.05)

if __name__ == "__main__":
    main()
