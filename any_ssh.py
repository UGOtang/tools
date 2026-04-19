import serial
import socket
import time
import threading
import sys

# ========== 配置区域 ==========
WIFI_SSID = "your_wifi"
WIFI_PASSWORD = "your_pass"
SERIAL_PORT = "/dev/ttyS2"    
BAUD_RATE = 115200            
ESP_PORT = 2222               
LOCAL_SSH_PORT = 22           

class ESPProxy:
    def __init__(self):
        print(f"[Init] 正在打开串口 {SERIAL_PORT} @ {BAUD_RATE}")
        try:
            self.ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0)
        except Exception as e:
            print(f"串口打开失败: {e}")
            sys.exit(1)
            
        self.ssh_sock = None
        self.client_id = None
        self.running = True
        
        # 全局共享内存池与锁
        self.buffer = bytearray()
        self.buffer_lock = threading.Lock()
        self.ser_write_lock = threading.Lock()

    def send_at_init(self, cmd, wait_for='OK', timeout=2.0):
        """仅用于初始化阶段的发送"""
        self.ser.write((cmd + '\r\n').encode())
        start = time.time()
        resp = b''
        while time.time() - start < timeout:
            if self.ser.in_waiting:
                resp += self.ser.read(self.ser.in_waiting)
                if wait_for.encode() in resp:
                    return True, resp
            time.sleep(0.01)
        return False, resp

    def init_wifi(self):
        print("[WiFi] 正在初始化 ESP-01S...")
        self.send_at_init('ATE0', timeout=0.5) # 关闭回显
        self.send_at_init('AT+CWMODE=1')
        
        print(f"[WiFi] 连接热点 {WIFI_SSID}...")
        self.send_at_init(f'AT+CWJAP="{WIFI_SSID}","{WIFI_PASSWORD}"', wait_for='OK', timeout=15)
        time.sleep(1) 
        
        _, resp = self.send_at_init('AT+CIFSR', wait_for='OK', timeout=3)
        ip = "Unknown"
        for line in resp.decode('ascii', 'ignore').split('\n'):
            if 'STAIP' in line:
                try: ip = line.split('"')[1]
                except: pass
        
        print(f"[WiFi] 连接成功！IP 地址: {ip}")
        self.send_at_init('AT+CIPMUX=1')
        self.send_at_init(f'AT+CIPSERVER=1,{ESP_PORT}')
        print(f"\n🚀 [Server] 隧道建立成功！")
        print(f"👉 请在电脑上连接: ssh root@{ip} -p {ESP_PORT}\n")

    def find_safe(self, token):
        """
        【防注入核心算法】
        在内存池中查找 AT 标识符。但如果该标识符位于一个还没被解析的 +IPD 数据包内部
        （也就是 SSH 的二进制伪装乱码），则判定为无效，避免代理崩溃！
        """
        idx = self.buffer.find(token)
        if idx != -1:
            ipd_idx = self.buffer.find(b'+IPD,')
            # 只有在没有 IPD 包，或者标识符在 IPD 包前面时，才是安全的 AT 指令
            if ipd_idx == -1 or idx < ipd_idx:
                return idx
        return -1

    def send_to_esp(self, data):
        """将本地 SSH 系统的密文，安全地发回给电脑"""
        if self.client_id is None: return
        
        CHUNK_SIZE = 256 # 小步快跑，防止 ESP 缓冲区雪崩
        for i in range(0, len(data), CHUNK_SIZE):
            chunk = data[i:i+CHUNK_SIZE]
            
            with self.ser_write_lock:
                self.ser.write(f'AT+CIPSEND={self.client_id},{len(chunk)}\r\n'.encode())
            
            # 安全地从内存池等候 '>'
            got_prompt = False
            start = time.time()
            while time.time() - start < 2.0:
                with self.buffer_lock:
                    idx = self.find_safe(b'>')
                    if idx != -1:
                        del self.buffer[idx:idx+1]
                        got_prompt = True
                        break
                time.sleep(0.005)
                
            if got_prompt:
                with self.ser_write_lock:
                    self.ser.write(chunk)
                
                # 等待 SEND OK，确保发送稳妥
                start = time.time()
                while time.time() - start < 2.0:
                    with self.buffer_lock:
                        idx = self.find_safe(b'SEND OK')
                        if idx != -1:
                            del self.buffer[idx:idx+7]
                            break
                    time.sleep(0.005)
            else:
                print("\n[警告] 串口死锁，未收到 '>' 提示符")

    def ssh_to_esp_thread(self):
        """线程：抽血本地 SSH 服务，打向外网"""
        while self.running and self.ssh_sock:
            try:
                data = self.ssh_sock.recv(4096)
                if not data:
                    break
                self.send_to_esp(data)
            except socket.timeout:
                pass
            except:
                break
        self.close_connection()

    def close_connection(self):
        if self.ssh_sock:
            try: self.ssh_sock.close()
            except: pass
            self.ssh_sock = None
            
        if self.client_id is not None:
            print(f"[TCP] 断开客户端 {self.client_id}")
            with self.ser_write_lock:
                self.ser.write(f'AT+CIPCLOSE={self.client_id}\r\n'.encode())
            self.client_id = None

    def start_proxy(self):
        self.init_wifi()
        
        while self.running:
            # 【绝对核心】：只有主循环可以读取串口！彻底断绝数据争夺。
            if self.ser.in_waiting:
                data = self.ser.read(self.ser.in_waiting)
                with self.buffer_lock:
                    self.buffer.extend(data)
            
            with self.buffer_lock:
                if not self.buffer:
                    time.sleep(0.005)
                    continue
                
                # 1. 优先剥离并转发 SSH 加密包 (最高优先级)
                ipd_idx = self.buffer.find(b'+IPD,')
                if ipd_idx != -1:
                    colon_idx = self.buffer.find(b':', ipd_idx)
                    if colon_idx != -1 and (colon_idx - ipd_idx) < 20:
                        try:
                            header = self.buffer[ipd_idx:colon_idx].decode('ascii')
                            data_len = int(header.split(',')[2])
                            total_len = colon_idx + 1 + data_len
                            
                            # 必须等该加密包在内存池中完全收齐
                            if len(self.buffer) >= total_len:
                                payload = self.buffer[colon_idx+1 : total_len]
                                if self.ssh_sock:
                                    try: self.ssh_sock.sendall(payload)
                                    except: pass
                                # 包提取完成，彻底从内存池切割销毁
                                del self.buffer[ipd_idx:total_len]
                                continue
                        except:
                            del self.buffer[ipd_idx:ipd_idx+5] # 报头损毁
                            continue

                # 2. 检测新连接建立
                conn_idx = self.find_safe(b',CONNECT')
                if conn_idx > 0:
                    c_id = self.buffer[conn_idx-1] - 48
                    print(f"\n[TCP] 收到远程连接! 客户端 ID: {c_id}")
                    self.client_id = c_id
                    
                    try:
                        self.ssh_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        self.ssh_sock.connect(('127.0.0.1', LOCAL_SSH_PORT))
                        self.ssh_sock.settimeout(0.1)
                        print("[Proxy] 本地 22 端口桥接完毕，开始传输加密流量...")
                        threading.Thread(target=self.ssh_to_esp_thread, daemon=True).start()
                    except Exception as e:
                        print(f"[错误] SSH 本地桥接失败: {e}")
                        self.close_connection()
                    
                    del self.buffer[:conn_idx+8]
                    continue

                # 3. 检测连接断开
                close_idx = self.find_safe(b',CLOSED')
                if close_idx > 0:
                    print("\n[TCP] 电脑端主动断开连接")
                    self.close_connection()
                    del self.buffer[:close_idx+7]
                    continue

                # 4. 防溢出保护
                if len(self.buffer) > 16384 and b'+IPD,' not in self.buffer:
                    del self.buffer[:-2048]

            time.sleep(0.005)

if __name__ == '__main__':
    proxy = ESPProxy()
    try:
        proxy.start_proxy()
    except KeyboardInterrupt:
        proxy.running = False
        proxy.close_connection()
        print("\n退出代理")
