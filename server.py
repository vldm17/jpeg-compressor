"""
JPEG Compressor Web Server & Remote Access
Предоставляет доступ к веб-приложению с телефона и ПК:
1. По локальной сети (Wi-Fi) с автогенерацией QR-кода в терминале.
2. Через глобальный интернет (Бесплатный Cloudflare Tunnel) для доступа из любой точки мира через 4G/5G.
"""

import os
import sys
import re
import socket
import threading
import subprocess
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, HTTPServer
import socketserver

# Windows UTF-8 console output
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False


def get_local_ips() -> list:
    """Определяет все активные локальные IPv4 адреса (Wi-Fi, Ethernet)."""
    ips = []
    # 1. Try ipconfig parsing on Windows
    try:
        out = subprocess.check_output('ipconfig', text=True, encoding='cp866', stderr=subprocess.DEVNULL)
        for block in out.split('\n\n'):
            ip_m = re.search(r'IPv4[^\r\n:]*:\s*([\d\.]+)', block)
            if ip_m:
                ip = ip_m.group(1).strip()
                if not ip.startswith('127.') and ip not in ips:
                    ips.append(ip)
    except Exception:
        pass

    # 2. Try socket UDP route
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        sock_ip = s.getsockname()[0]
        s.close()
        if sock_ip not in ips and not sock_ip.startswith('127.'):
            ips.insert(0, sock_ip)
    except Exception:
        pass

    # Prefer 192.168.x.x addresses first (common home Wi-Fi)
    ips.sort(key=lambda x: 0 if x.startswith('192.168.') else (1 if x.startswith('10.') else 2))
    return ips if ips else ['127.0.0.1']


def find_free_port(start_port: int = 8080) -> int:
    """Ищет свободный порт, начиная с start_port."""
    port = start_port
    while port < start_port + 50:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) != 0:
                return port
            port += 1
    return start_port


def print_qr_code(url: str):
    """Выводит QR-код прямо в текстовую консоль."""
    if not HAS_QRCODE:
        return
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception:
        pass


class CustomHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        # Serve web_app directory
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web_app')
        super().__init__(*args, directory=web_dir, **kwargs)

    def log_message(self, format, *args):
        # Silence routine 200 GET logs for cleaner terminal
        pass


def download_cloudflared(dest_path: str):
    """Скачивает официальный легковесный бинарник cloudflared для Windows."""
    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    print("\n[Cloudflare Tunnel] Первый запуск: скачивание бесплатного клиента Cloudflare...")
    print(f"Источник: {url}")
    try:
        urllib.request.urlretrieve(url, dest_path)
        print("[Cloudflare Tunnel] Скачивание завершено успешно!")
    except Exception as e:
        print(f"[Ошибка скачивания Cloudflare]: {e}")
        return False
    return True


def run_cloudflare_tunnel(port: int):
    """Запускает Cloudflare Quick Tunnel и выводит публичную HTTPS-ссылку."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cf_exe = os.path.join(base_dir, "cloudflared.exe")

    if not os.path.exists(cf_exe):
        success = download_cloudflared(cf_exe)
        if not success:
            print("Не удалось запустить онлайн-туннель. Используйте доступ по Wi-Fi.")
            return

    print("\n[Cloudflare Tunnel] Создание безопасного бесплатного HTTPS-туннеля...")
    cmd = [cf_exe, "tunnel", "--url", f"http://127.0.0.1:{port}"]

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )

        tunnel_url = None
        # Parse stdout/stderr to find the https://*.trycloudflare.com url
        for line in process.stdout:
            match = re.search(r'(https://[a-zA-Z0-9-]+\.trycloudflare\.com)', line)
            if match:
                tunnel_url = match.group(1)
                break

        if tunnel_url:
            print("\n" + "=" * 62)
            print("  🌐 ВАША ПУБЛИЧНАЯ ОНЛАЙН-ССЫЛКА (ДОСТУП ИЗ ИНТЕРНЕТА):")
            print(f"  👉 {tunnel_url}")
            print("=" * 62)
            print("Откройте эту ссылку на телефоне (работает через 4G/5G/Wi-Fi из любой точки мира):")
            print_qr_code(tunnel_url)
            print("Наведите камеру смартфона на QR-код выше для перехода.\n")
        else:
            print("Ожидание инициализации туннеля...")

        process.wait()
    except Exception as e:
        print(f"[Ошибка Cloudflare Tunnel]: {e}")


def main():
    online_mode = "--online" in sys.argv
    port = find_free_port(8080)

    # Start HTTP server in background thread
    server = HTTPServer(('0.0.0.0', port), CustomHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    local_ips = get_local_ips()
    primary_ip = local_ips[0]
    local_url = f"http://{primary_ip}:{port}"
    localhost_url = f"http://localhost:{port}"

    print("\n" + "=" * 62)
    print("      ⚡ JPEG COMPRESSOR 2.0 — СЕРВЕР ЗАПУЩЕН")
    print("=" * 62)
    print(f"  💻 Доступ с этого ПК:        {localhost_url}")
    print(f"  📱 Доступ с телефона (Wi-Fi): {local_url}")
    if len(local_ips) > 1:
        print(f"  Дополнительные IP адреса:   {', '.join(['http://' + ip + ':' + str(port) for ip in local_ips[1:]])}")
    print("=" * 62)

    if not online_mode:
        print("\n📱 СКАНЕР QR-КОДА ДЛЯ ТЕЛЕФОНА (ДОМАШНИЙ WI-FI):")
        print("Подключите телефон к тому же Wi-Fi и наведите камеру на QR-код:")
        print_qr_code(local_url)
        print(f"\nИли вручную введите в браузере смартфона: {local_url}")
        print("\nНажмите Ctrl+C для остановки сервера.")
    else:
        # Start online tunnel
        run_cloudflare_tunnel(port)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nОстановка сервера...")
        server.shutdown()


if __name__ == '__main__':
    main()
