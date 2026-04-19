import serial
import socket
import time
import threading
import sys

# ========== 配置区域 ==========
WIFI_SSID = "your_wifi"
WIFI_PASSWORD = "your_pass"
SERIAL_PORT = "/dev/ttyS2"    
INIT_BAUD = 115200            
HIGH_BAUD = 921600            # 究极提速：8倍速波特率！

ESP_PORT = 2222               
LOCAL_SSH_PORT = 22           

class ESPProxy:
    def __init__(self):
        print(f"[Init] 正在以 {INIT_BAUD} 波特率打开串口...")
        try:
            self.ser = serial.Serial(SERIAL_PORT, INIT_BAUD, timeout=0)
        except Exception as e:
            print(f"串口打开失败: {e}")
            sys.exit(1)
            
        self.ssh_sock = None
        self.client_id = None
        self.running = True
        
        self.buffer = bytearray()
        self.buffer_lock = threading.Lock()
        self.ser_write_lock = threading.Lock()

    def send_at_init(self, cmd, wait_for='OK', timeout=2.0):
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

    def speed_up_uart(self):
        """🚀 动态换挡：将 ESP-01S 提速至 921600，解决 top 卡顿"""
        print(f"[SpeedUp] 尝试将通信速率提升至 {HIGH_BAUD}...")
        # 使用 AT+UART_CUR 只改变本次运行的波特率，断电后恢复115200，绝对安全
        self.send_at_init(f'AT+UART_CUR={HIGH_BAUD},8,1,0,0', timeout=0.5)
        time.sleep(0.2)
        
        # 切换本地 Linux 的串口波特率
        self.ser.close()
        self.ser = serial.Serial(SERIAL_PORT, HIGH_BAUD, timeout=0)
        
        # 测试新波特率是否握手成功
        ok, _ = self.send_at_init('AT', timeout=1)
        if ok:
            print(f"⚡ [SpeedUp] 提速成功！当前速率: {HIGH_BAUD} (8倍速享受)")
        else:
            print("⚠️ [SpeedUp] 提速失败，回退至 115200...")
            self.ser.close()
            self.ser = serial.Serial(SERIAL_PORT, INIT_BAUD, timeout=0)

    def init_wifi(self):
        print("[WiFi] 正在初始化 ESP-01S...")
        self.send_at_init('ATE0', timeout=0.5) 
        self.send_at_init('AT+CWMODE=1')
        
        print(f"[WiFi] 连接热点 {WIFI_SSID}...")
        self.send_at_init(f'AT+CWJAP="{WIFI_SSID}","{WIFI_PASSWORD}"', wait_for='OK', timeout=15)
        time.sleep(1) 
        
        # 换挡提速！
        self.speed_up_uart()
        
        _, resp = self.send_at_init('AT+CIFSR', wait_for='OK', timeout=3)
        ip = "Unknown"
        for line in resp.decode('ascii', 'ignore').split('\n'):
            if 'STAIP' in line:
                try: ip = line.split('"')[1]
                except: pass
        
        print(f"[WiFi] 连接成功！IP 地址: {ip}")
        self.send_at_init('AT+CIPMUX=1')
        self.send_at_init(f'AT+CIPSERVER=1,{ESP_PORT}')
        print(f"\n🚀 [Server] 高速隧道建立成功！")
        print(f"👉 请在电脑上连接: ssh root@{ip} -p {ESP_PORT}\n")

    def find_safe(self, token):
        idx = self.buffer.find(token)
        if idx != -1:
            ipd_idx = self.buffer.find(b'+IPD,')
            if ipd_idx == -1 or idx < ipd_idx:
                return idx
        return -1

    def send_to_esp(self, data):
        """【提速重构版】大容量流式发送，大幅削减等待时间"""
        if self.client_id is None: return
        
        # 将包体积提升4倍，大幅减少 AT 指令交互产生的延迟
        CHUNK_SIZE = 1024 
        for i in range(0, len(data), CHUNK_SIZE):
            chunk = data[i:i+CHUNK_SIZE]
            
            with self.ser_write_lock:
                self.ser.write(f'AT+CIPSEND={self.client_id},{len(chunk)}\r\n'.encode())
            
            got_prompt = False
            start = time.time()
            while time.time() - start < 2.0:
                with self.buffer_lock:
                    idx = self.find_safe(b'>')
                    if idx != -1:
                        del self.buffer[idx:idx+1]
                        got_prompt = True
                        break
                time.sleep(0.002)
                
            if got_prompt:
                # 配合高波特率，将 1024 字节切割为 256 字节的微块连发，防硬件缓存溢出
                with self.ser_write_lock:
                    for j in range(0, len(chunk), 256):
                        self.ser.write(chunk[j:j+256])
                        time.sleep(0.001)
                
                # 快速等待 SEND OK
                start = time.time()
                while time.time() - start < 1.5:
                    with self.buffer_lock:
                        idx = self.find_safe(b'SEND OK')
                        if idx != -1:
                            del self.buffer[idx:idx+7]
                            break
                    time.sleep(0.002)

    def ssh_to_esp_thread(self):
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
            if self.ser.in_waiting:
                data = self.ser.read(self.ser.in_waiting)
                with self.buffer_lock:
                    self.buffer.extend(data)
            
            with self.buffer_lock:
                if not self.buffer:
                    time.sleep(0.005)
                    continue
                
                ipd_idx = self.buffer.find(b'+IPD,')
                if ipd_idx != -1:
                    colon_idx = self.buffer.find(b':', ipd_idx)
                    if colon_idx != -1 and (colon_idx - ipd_idx) < 20:
                        try:
                            header = self.buffer[ipd_idx:colon_idx].decode('ascii')
                            data_len = int(header.split(',')[2])
                            total_len = colon_idx + 1 + data_len
                            
                            if len(self.buffer) >= total_len:
                                payload = self.buffer[colon_idx+1 : total_len]
                                if self.ssh_sock:
                                    try: self.ssh_sock.sendall(payload)
                                    except: pass
                                del self.buffer[ipd_idx:total_len]
                                continue
                        except:
                            del self.buffer[ipd_idx:ipd_idx+5] 
                            continue

                conn_idx = self.find_safe(b',CONNECT')
                if conn_idx > 0:
                    c_id = self.buffer[conn_idx-1] - 48
                    print(f"\n[TCP] 收到远程连接! 客户端 ID: {c_id}")
                    self.client_id = c_id
                    
                    try:
                        self.ssh_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        self.ssh_sock.connect(('127.0.0.1', LOCAL_SSH_PORT))
                        self.ssh_sock.settimeout(0.1)
                        print("[Proxy] 本地 22 端口桥接完毕，开始高速传输...")
                        threading.Thread(target=self.ssh_to_esp_thread, daemon=True).start()
                    except Exception as e:
                        print(f"[错误] SSH 本地桥接失败: {e}")
                        self.close_connection()
                    
                    del self.buffer[:conn_idx+8]
                    continue

                close_idx = self.find_safe(b',CLOSED')
                if close_idx > 0:
                    print("\n[TCP] 电脑端主动断开连接")
                    self.close_connection()
                    del self.buffer[:close_idx+7]
                    continue

                if len(self.buffer) > 16384 and b'+IPD,' not in self.buffer:
                    del self.buffer[:-2048]

            time.sleep(0.002)

if __name__ == '__main__':
    proxy = ESPProxy()
    try:
        proxy.start_proxy()
    except KeyboardInterrupt:
        proxy.running = False
        proxy.close_connection()
