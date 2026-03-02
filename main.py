import machine
from machine import Pin
import time
import struct
import network
import socket
import json
import ntptime
import DS1302
import os
import gc
import sys

# ================= 初始化系统目录 =================
def ensure_dir(path):
    try:
        os.mkdir(path)
    except OSError:
        pass

ensure_dir('res')
ensure_dir('res/conf')

# ================= 初始化 DS1302 =================
ds1302 = DS1302.DS1302(clk=Pin(14), dio=Pin(13), cs=Pin(15))

# ================= 计算机控制 GPIO 设置 =================
pc1_sw = Pin(26, Pin.OUT, value=0)
pc2_sw = Pin(27, Pin.OUT, value=0)
pc_sw_pins = {1: pc1_sw, 2: pc2_sw}
pc_pulse_end = {1: 0, 2: 0} 

pc1_led = Pin(32, Pin.IN, Pin.PULL_UP)
pc2_led = Pin(33, Pin.IN, Pin.PULL_UP)

# ================= 统一配置与日志管理 =================
CONFIG_FILE = 'res/conf/config.jsonl'
LOG_FILE = 'res/conf/logs.jsonl'       
HISTORY_FILE = 'res/conf/history.jsonl' 
HTML_FILE = 'res/index.html'
MODBUS_DIM_FILE = 'res/conf/ModbusDim.jsonl' 

app_config = {
    "wifi_list": [
        {"ssid": "WIFI_PRIMARY", "pass": "YOUR_PASS_1"},
        {"ssid": "WIFI_BACKUP", "pass": "YOUR_PASS_2"}
    ],
    "test_mode": "short",    
    "test_limit": 50,        
    "test_schedule": "none", 
    "test_day": 1,           
    "test_hour": 2,          
    "test_minute": 0,
    "buzzer_muted": True,
    "auto_start": 0,
    "log_limit": 50,
    "history_limit": 1440,
    "lang": "zh-cn",
    "sec_pwd": "",
    "ups_model_str": "--",
    "ups_version": "--",
    "ups_esn": "--"
}

def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            for line in f:
                if line.strip():
                    app_config.update(json.loads(line))
    except OSError:
        save_config()
    except ValueError:
        print("警告: 配置文件损坏，正在恢复默认设置...")
        save_config()
    except Exception as e:
        print("读取配置失败:", e)

def save_config():
    try:
        with open(CONFIG_FILE, 'w') as f:
            for k, v in app_config.items():
                f.write(json.dumps({k: v}) + '\n')
    except Exception as e:
        print("保存配置失败:", e)

def trim_file(filepath, limit, buffer_size=60):
    try:
        count = 0
        with open(filepath, 'r') as f:
            for _ in f: 
                count += 1
        if count > limit + buffer_size:
            with open(filepath, 'r') as f_in, open(filepath + '.tmp', 'w') as f_out:
                skip = count - limit
                for i, line in enumerate(f_in):
                    if i >= skip:
                        f_out.write(line)
            try: os.remove(filepath)
            except OSError: pass
            os.rename(filepath + '.tmp', filepath)
    except Exception: pass

last_log_type = ""
last_log_msg = ""
def add_log(l_type, msg):
    global last_log_type, last_log_msg
    if l_type == last_log_type and msg == last_log_msg:
        return
    last_log_type = l_type
    last_log_msg = msg
    
    lt = time.localtime()
    time_str = f"{lt[0]}-{lt[1]:02d}-{lt[2]:02d} {lt[3]:02d}:{lt[4]:02d}:{lt[5]:02d}"
    record = {"time": time_str, "type": l_type, "msg": msg}
    
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        pass
    
    limit = app_config.get("log_limit", 50)
    trim_file(LOG_FILE, limit, 10)

load_config()

# ================= 全局状态与数据字典 =================
auto_deep_test_active = False 
manual_deep_test_active = False
last_test_trigger_minute = -1 
last_bat_status = -1
last_history_min = -1

# 独立告警跟踪状态机
active_alarms = set()
last_alarm_check = 0

ups_data = {
    "in_voltage": 0.0, "in_freq": 0.0, "in_system": 0,
    "bypass_voltage": 0.0, "bypass_freq": 0.0,
    "out_voltage": 0.0, "out_current": 0.0, "out_freq": 0.0, "out_system": 0,
    "out_active_power": 0.0, "out_apparent_power": 0.0, "load_ratio": 0.0,
    "bat_voltage": 0.0, "bat_status": 0, "bat_remain_cap": 0,
    "bat_remain_time": 0, "bat_cells": 0, "bat_cap_ah": 0,
    "temperature": 0.0, "power_mode": 0, "ups_status": 0, "power_status": 0,
    "buzzer_muted": app_config["buzzer_muted"], "sys_time": "",
    "pc1_state": 0, "pc2_state": 0,
    "ups_model_str": app_config["ups_model_str"], "ups_version": app_config["ups_version"], "ups_esn": app_config["ups_esn"],
    "energy_flow": 0, "alm_temp": 0, "alm_byp": 0, "alm_bat1": 0, "alm_bat2": 0, "alm_bat3": 0, "alm_out": 0, "alm_inv": 0, "alm_in": 0
}

uart = machine.UART(2, baudrate=9600, bits=8, parity=None, stop=1, timeout=100, rx=16, tx=17)

# ================= Modbus 动态字典加载 =================
read_tasks = []
write_regs = {}

def init_modbus_dim():
    global read_tasks, write_regs
    try:
        with open(MODBUS_DIM_FILE, 'r') as f:
            for line in f:
                if not line.strip(): continue
                item = json.loads(line)
                if item.get("type") == "read_task":
                    read_tasks.append(item)
                elif item.get("type") == "write_reg":
                    write_regs[item["key"]] = item["reg"]
        if not read_tasks or not write_regs:
            raise ValueError("File loaded but dictionaries are empty.")
        print(f"Modbus 映射加载成功! 读任务: {len(read_tasks)} 项, 写任务: {len(write_regs)} 项")
    except Exception as e:
        print(f"致命错误: 无法加载或解析 {MODBUS_DIM_FILE} ({e})")
        print("系统已挂起，请检查文件是否完整上传。")
        # 抛出异常中断执行，严格按照 Fail Fast 原则
        sys.exit(1) 

init_modbus_dim()

def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected(): return wlan.ifconfig()[0]
    wifis = app_config.get("wifi_list", [])
    if not wifis and "wifi_ssid" in app_config: wifis.append({"ssid": app_config["wifi_ssid"], "pass": app_config["wifi_pass"]})
    for w in wifis:
        if not w.get("ssid"): continue
        wlan.disconnect() 
        time.sleep(0.5)
        wlan.connect(w["ssid"], w["pass"])
        timeout = 10
        while not wlan.isconnected() and timeout > 0: time.sleep(1); timeout -= 1
        if wlan.isconnected(): return wlan.ifconfig()[0]
    return None

def sync_ntp_and_rtc():
    try:
        ntptime.host = "ntp.aliyun.com"
        ntptime.settime() 
        bj_timestamp = time.time() + 8 * 3600
        tm = time.localtime(bj_timestamp)
        machine.RTC().datetime((tm[0], tm[1], tm[2], tm[6], tm[3], tm[4], tm[5], 0))
        ds1302.DateTime([tm[0], tm[1], tm[2], tm[6], tm[3], tm[4], tm[5]])
        add_log('System', 'NTP Sync Success')
        return True
    except Exception: return False

def calc_crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8): crc = (crc >> 1) ^ 0xA001 if (crc & 1) else crc >> 1
    return struct.pack('<H', crc)

def build_modbus_frame(slave_id, func_code, register, value_or_count):
    frame = struct.pack('>BBHH', slave_id, func_code, register, value_or_count)
    return frame + calc_crc16(frame)

def read_modbus_register(reg_addr, count=1):
    uart.read() 
    uart.write(build_modbus_frame(0x01, 0x03, reg_addr, count))
    time.sleep_ms(80) 
    expected_len = 5 + count * 2
    resp = uart.read(expected_len)
    if resp and len(resp) == expected_len and resp[0:2] == b'\x01\x03':
        return struct.unpack('>H', resp[3:5])[0] if count == 1 else struct.unpack('>I', resp[3:7])[0]
    return None

def write_modbus_register(reg_addr, value):
    uart.write(build_modbus_frame(0x01, 0x06, reg_addr, value))
    time.sleep_ms(100)
    uart.read()

def fetch_device_info():
    if app_config.get("ups_model_str", "--") != "--" and app_config.get("ups_esn", "--") != "--":
        return
        
    try:
        uart.read()
        uart.write(bytes([0x01, 0x2B, 0x0E, 0x03, 0x87, 0x31, 0x75]))
        time.sleep_ms(500) 
        resp = uart.read()
        if resp and b'1=' in resp:
            s = resp[resp.find(b'1='):].decode('ascii', 'ignore')
            info = {}
            for p in s.split(';'):
                if '=' in p:
                    k, v = p.split('=', 1)
                    info[k] = v
            updated = False
            if '1' in info: 
                app_config['ups_model_str'] = info['1']
                ups_data['ups_model_str'] = info['1']
                updated = True
            if '2' in info: 
                app_config['ups_version'] = info['2']
                ups_data['ups_version'] = info['2']
                updated = True
            if '4' in info: 
                app_config['ups_esn'] = info['4']
                ups_data['ups_esn'] = info['4']
                updated = True
            if updated:
                save_config()
    except Exception: pass

def init_ups_hardware():
    write_modbus_register(write_regs["buzzer_mute"], 1 if app_config.get("buzzer_muted", True) else 0)
    write_modbus_register(write_regs["auto_start"], 1 if app_config.get("auto_start", 0) else 0)

# ================= 系统初始化 =================
try:
    dt = ds1302.DateTime()
    if dt[0] >= 2024: machine.RTC().datetime((dt[0], dt[1], dt[2], dt[3], dt[4], dt[5], dt[6], 0))
except Exception: pass

ip_address = connect_wifi()
if ip_address: sync_ntp_and_rtc()

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('', 80))
s.listen(5)
s.setblocking(False)

fetch_device_info()
init_ups_hardware()

print(f"System Ready! Access: http://{ip_address}/")
print(f"Mobile Ready! Access: http://{ip_address}/mobile")
add_log('System', 'Backend Started')
gc.collect()

# ================= 主循环 =================
last_read_time = 0
READ_INTERVAL = 100
current_task_idx = 0
last_ntp_sync_ticks = time.ticks_ms()
NTP_SYNC_INTERVAL_MS = 12 * 60 * 60 * 1000  

boot_time = time.ticks_ms()
is_booting = True

while True:
    current_time = time.ticks_ms()
    if is_booting and time.ticks_diff(current_time, boot_time) >= 30000: is_booting = False

    for pc_id, end_time in pc_pulse_end.items():
        if end_time > 0 and current_time >= end_time:
            pc_sw_pins[pc_id].value(0); pc_pulse_end[pc_id] = 0
    
    ups_data["pc1_state"] = 1 if pc1_led.value() == 0 else 0
    ups_data["pc2_state"] = 1 if pc2_led.value() == 0 else 0

    # ================= 核心：告警跟踪状态机 =================
    if not is_booting and time.ticks_diff(current_time, last_alarm_check) > 2000:
        last_alarm_check = current_time
        d_in = int(ups_data.get("alm_in", 65535))
        is_ac_fail = (d_in >> 8) & 1 if d_in != 65535 else 0
        
        alarms_to_check = [
            ("alm_temp", 3, "内部过温"), ("alm_in", 8, "市电中断"),
            ("alm_byp", 1, "旁路电压异常"), ("alm_byp", 2, "旁路频率异常"),
            ("alm_bat1", 3, "电池过压"), ("alm_bat2", 1, "电池需维护"),
            ("alm_bat2", 3, "电池低压"), ("alm_bat3", 4, "电池未接"),
            ("alm_out", 5, "输出过载"),
            ("alm_inv", 5, "逆变器异常"), ("alm_inv", 6, "逆变器异常"), ("alm_inv", 7, "逆变器异常")
        ]
        
        for key, bit, name in alarms_to_check:
            val = int(ups_data.get(key, 65535))
            if val == 65535: continue
            
            is_active = (val >> bit) & 1
            if "旁路" in name and is_ac_fail: is_active = 0
                
            alarm_id = f"{key}_{bit}"
            if is_active and alarm_id not in active_alarms:
                active_alarms.add(alarm_id)
                add_log('System', f'警告发生: {name}')
            elif not is_active and alarm_id in active_alarms:
                active_alarms.remove(alarm_id)
                add_log('System', f'警告恢复: {name}')

    lt = time.localtime()
    current_min = lt[4]
    ups_data["sys_time"] = f"{lt[0]}-{lt[1]:02d}-{lt[2]:02d} {lt[3]:02d}:{lt[4]:02d}:{lt[5]:02d}"
    
    if current_min != last_history_min and not is_booting:
        last_history_min = current_min
        record = [
            f"{lt[3]:02d}:{lt[4]:02d}", round(ups_data.get("temperature", 0), 1), round(ups_data.get("bat_voltage", 0), 1),
            int(ups_data.get("bat_remain_cap", 0)), round(ups_data.get("in_voltage", 0), 1), round(ups_data.get("out_voltage", 0), 1),
            round(ups_data.get("out_current", 0), 1), round(ups_data.get("out_active_power", 0), 2), round(ups_data.get("out_apparent_power", 0), 2), round(ups_data.get("load_ratio", 0), 1)
        ]
        try:
            with open(HISTORY_FILE, 'a') as f: f.write(json.dumps(record) + '\n')
        except Exception: pass
        trim_file(HISTORY_FILE, app_config.get("history_limit", 1440), 60); gc.collect()
    
    if time.ticks_diff(current_time, last_ntp_sync_ticks) > NTP_SYNC_INTERVAL_MS:
        if network.WLAN(network.STA_IF).isconnected(): sync_ntp_and_rtc()
        last_ntp_sync_ticks = current_time

    if current_min != last_test_trigger_minute:
        should_run = False
        if app_config["test_schedule"] == "weekly" and (lt[6] + 1) == app_config["test_day"]: 
            if lt[3] == app_config["test_hour"] and lt[4] == app_config["test_minute"]: should_run = True
        elif app_config["test_schedule"] == "monthly" and lt[2] == app_config["test_day"]:
            if lt[3] == app_config["test_hour"] and lt[4] == app_config["test_minute"]: should_run = True
        if should_run:
            last_test_trigger_minute = current_min
            if app_config["test_mode"] == "short": write_modbus_register(write_regs["test_short"], 1); add_log('Control', 'Auto Test: Short')
            elif app_config["test_mode"] == "deep": write_modbus_register(write_regs["test_deep"], 1); auto_deep_test_active = True; add_log('Control', 'Auto Test: Deep')

    # ---------------- 2. 响应 HTTP 请求 ----------------
    try:
        conn, addr = s.accept()
        conn.settimeout(3.0) 
        request = conn.recv(1024)
        if request:
            req_parts = request.decode('utf-8', 'ignore').split(' ')
            if len(req_parts) > 1:
                req_str = req_parts[1]
                
                if req_str == '/':
                    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n\r\n')
                    try:
                        with open(HTML_FILE, 'rb') as f:
                            while True:
                                chunk = f.read(1024)
                                if not chunk: break
                                conn.sendall(chunk)
                    except OSError: conn.sendall(b"Error: index.html not found.")

                elif req_str == '/m' or req_str == '/mobile':
                    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n\r\n')
                    try:
                        with open('res/mobile.html', 'rb') as f:
                            while True:
                                chunk = f.read(1024)
                                if not chunk: break
                                conn.sendall(chunk)
                    except OSError: conn.sendall(b"Error: mobile.html not found. Please upload to res/")

                elif req_str.startswith('/res/'):
                    filepath = req_str[1:]  
                    content_type = 'application/octet-stream'
                    if filepath.endswith('.css'): content_type = 'text/css'
                    elif filepath.endswith('.woff2'): content_type = 'font/woff2'
                    elif filepath.endswith('.json') or filepath.endswith('.jsonl'): content_type = 'application/json; charset=utf-8'
                    elif filepath.endswith('.svg'): content_type = 'image/svg+xml'
                    elif filepath.endswith('.js'): content_type = 'application/javascript'
                    try:
                        with open(filepath, 'rb') as f:
                            conn.sendall(f'HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nConnection: close\r\n\r\n'.encode('utf-8'))
                            while True:
                                chunk = f.read(1024)
                                if not chunk: break
                                conn.sendall(chunk)
                    except OSError: conn.sendall(b'HTTP/1.1 404 Not Found\r\n\r\n')

                elif req_str == '/api/status':
                    free_ram = gc.mem_free(); total_ram = free_ram + gc.mem_alloc()
                    try: st = os.statvfs('/'); flash_total = st[0] * st[2]; flash_free = st[0] * st[3]
                    except Exception: flash_total, flash_free = 0, 0
                    
                    ups_data["sys_ram_free"] = free_ram; ups_data["sys_ram_total"] = total_ram
                    ups_data["sys_flash_free"] = flash_free; ups_data["sys_flash_total"] = flash_total
                    ups_data["auto_deep_test_active"] = auto_deep_test_active; ups_data["manual_deep_test_active"] = manual_deep_test_active
                    ups_data["test_limit"] = app_config.get("test_limit", 50); ups_data["buzzer_muted"] = app_config.get("buzzer_muted", True) 
                    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nConnection: close\r\n\r\n')
                    conn.sendall(json.dumps(ups_data).encode('utf-8'))
                    
                elif req_str == '/api/history':
                    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nConnection: close\r\n\r\n'); conn.sendall(b'[')
                    try:
                        with open(HISTORY_FILE, 'r') as f:
                            first = True
                            for line in f:
                                line = line.strip()
                                if line:
                                    if not first: conn.sendall(b',')
                                    conn.sendall(line.encode('utf-8')); first = False
                    except OSError: pass
                    conn.sendall(b']')
                    
                elif req_str == '/api/get_config':
                    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n')
                    config_copy = app_config.copy()
                    if 'sec_pwd' in config_copy: config_copy['sec_pwd'] = "" if not app_config['sec_pwd'] else "******" 
                    conn.sendall(json.dumps(config_copy).encode('utf-8'))
                    
                elif req_str == '/api/logs':
                    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n')
                    lines = []
                    try:
                        with open(LOG_FILE, 'r') as f:
                            for line in f:
                                if line.strip(): lines.append(line.strip())
                    except OSError: pass
                    conn.sendall(b'['); 
                    for i, line in enumerate(reversed(lines)):
                        conn.sendall(line.encode('utf-8')); 
                        if i < len(lines) - 1: conn.sendall(b',')
                    conn.sendall(b']')
                    
                elif req_str == '/api/logs_clear':
                    try: os.remove(LOG_FILE)
                    except OSError: pass
                    last_log_type, last_log_msg = "", ""; add_log('System', 'Logs cleared by user')
                    conn.sendall(b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nOK')
                    
                elif req_str.startswith('/api/set_config?'):
                    params_str = req_str.split('?')[1].split(' ')[0]
                    params = dict([p.split('=') for p in params_str.split('&') if '=' in p])
                    
                    if 'set_pwd' in params:
                        pwd_in = params['set_pwd'].replace('%20', '').strip()
                        if pwd_in == "******": pass
                        elif pwd_in == "" or pwd_in.isalnum(): app_config['sec_pwd'] = pwd_in

                    if 'mode' in params: app_config['test_mode'] = params['mode']
                    if 'limit' in params: app_config['test_limit'] = int(params['limit'])
                    if 'log_limit' in params: app_config['log_limit'] = int(params['log_limit'])
                    if 'hist_limit' in params: app_config['history_limit'] = int(params['hist_limit'])
                    if 'lang' in params: app_config['lang'] = params['lang']
                    if 'buzzer' in params:
                        app_config['buzzer_muted'] = True if params['buzzer'] == '1' else False
                        write_modbus_register(write_regs["buzzer_mute"], 1 if app_config['buzzer_muted'] else 0)
                    if 'autostart' in params:
                        app_config['auto_start'] = int(params['autostart'])
                        write_modbus_register(write_regs["auto_start"], app_config['auto_start'])
                    if 'sch' in params: app_config['test_schedule'] = params['sch']
                    if 'day' in params: app_config['test_day'] = int(params['day'])
                    if 'h' in params: app_config['test_hour'] = int(params['h'])
                    if 'm' in params: app_config['test_minute'] = int(params['m'])
                    
                    save_config(); add_log('Control', 'Settings updated')
                    trim_file(LOG_FILE, app_config['log_limit'], 0); trim_file(HISTORY_FILE, app_config['history_limit'], 0)
                    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nOK')
                    
                elif req_str.startswith('/api/pc_ctrl?'):
                    params_str = req_str.split('?')[1].split(' ')[0]
                    params = dict([p.split('=') for p in params_str.split('&') if '=' in p])
                    sec_pwd = app_config.get('sec_pwd', '')
                    if sec_pwd and params.get('pwd', '').lower() != sec_pwd.lower(): conn.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
                    else:
                        pc_id = int(params.get('pc', 1)); action = params.get('action', 'power')
                        if pc_id in pc_sw_pins:
                            pc_sw_pins[pc_id].value(1)
                            if action == 'power': pc_pulse_end[pc_id] = current_time + 500; add_log('Control', f'PC{pc_id} Power Pulse')
                            elif action == 'force': pc_pulse_end[pc_id] = current_time + 10000; add_log('Control', f'PC{pc_id} Force Off')
                        conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nOK')
                    
                elif req_str.startswith('/api/ctrl?action='):
                    params_str = req_str.split('?')[1].split(' ')[0]
                    params = dict([p.split('=') for p in params_str.split('&') if '=' in p])
                    action = params.get('action', ''); sec_pwd = app_config.get('sec_pwd', '')
                    
                    if sec_pwd and params.get('pwd', '').lower() != sec_pwd.lower(): conn.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
                    else:
                        if action == 'power_on': write_modbus_register(write_regs["power_on"], 1); add_log('Control', 'UPS Turn ON')
                        elif action == 'power_off': write_modbus_register(write_regs["power_off"], 1); add_log('Control', 'UPS Turn OFF')
                        elif action == 'test_short': write_modbus_register(write_regs["test_short"], 1); add_log('Control', 'Short Test Started')
                        elif action == 'test_deep': write_modbus_register(write_regs["test_deep"], 1); manual_deep_test_active = True; auto_deep_test_active = False; add_log('Control', 'Deep Test Started')
                        elif action == 'test_stop': write_modbus_register(write_regs["test_stop"], 1); auto_deep_test_active = False; manual_deep_test_active = False; add_log('Control', 'Test Stopped')
                        conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nOK')
                    
                elif req_str.startswith('/api/debug?'):
                    params_str = req_str.split('?')[1].split(' ')[0]
                    params = dict([p.split('=') for p in params_str.split('&') if '=' in p])
                    try:
                        fc = int(params['fc']); reg = int(params['reg'], 0); val = int(params['val'], 0); add_log('Control', f'Modbus Debug FC:{fc} REG:{hex(reg)}')
                        uart.read(); frame = build_modbus_frame(0x01, fc, reg, val); uart.write(frame); time.sleep_ms(150); resp = uart.read(); data = list(resp) if resp else []
                        conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n'); conn.sendall(json.dumps({"req": list(frame), "res": data}).encode('utf-8'))
                    except Exception: conn.sendall(b'HTTP/1.1 400 Bad Request\r\n\r\n')
                    
                elif req_str.startswith('/api/hex_debug?'):
                    params_str = req_str.split('?')[1].split(' ')[0]
                    params = dict([p.split('=') for p in params_str.split('&') if '=' in p])
                    hex_str = params.get('data', '').replace('%20', '').replace('+', '').replace(' ', '').strip()
                    try:
                        frame = bytes([int(hex_str[i:i+2], 16) for i in range(0, len(hex_str), 2)]); add_log('Control', f'Hex Raw: {hex_str}')
                        uart.read(); uart.write(frame); time.sleep_ms(200); resp = uart.read(); data = list(resp) if resp else []
                        conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n'); conn.sendall(json.dumps({"req": hex_str, "res": data}).encode('utf-8'))
                    except Exception: conn.sendall(b'HTTP/1.1 400 Bad Request\r\n\r\n')

    except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass
        gc.collect()

    # ---------------- 4. 状态机轮询读取 ----------------
    if time.ticks_diff(current_time, last_read_time) >= READ_INTERVAL:
        last_read_time = current_time
        if read_tasks:
            task = read_tasks[current_task_idx]
            try:
                raw_value = read_modbus_register(task["reg"], task["len"])
                if raw_value is not None:
                    ups_data[task["key"]] = raw_value / task["gain"]
                    if task["key"] == "bat_status":
                        status_val = int(ups_data["bat_status"])
                        if last_bat_status != -1 and status_val != last_bat_status:
                            if status_val == 5: add_log('Discharge', f'Discharging (V:{ups_data["bat_voltage"]}V, Cap:{ups_data["bat_remain_cap"]}%)')
                            elif last_bat_status == 5: add_log('Discharge', f'Discharge ended. State: {status_val}')
                        last_bat_status = status_val
                    elif task["key"] == "bat_remain_cap":
                        current_percent = int(ups_data["bat_remain_cap"])
                        if auto_deep_test_active and current_percent <= app_config.get("test_limit", 50):
                            write_modbus_register(write_regs["test_stop"], 1); auto_deep_test_active = False; add_log('System', f'Deep test limit {app_config.get("test_limit", 50)}% reached, stopped.')
            except Exception: pass
            current_task_idx = (current_task_idx + 1) % len(read_tasks)
        
    time.sleep_ms(5)