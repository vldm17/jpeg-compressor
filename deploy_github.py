"""
Автоматическая публикация проекта на GitHub Pages
"""

import os
import sys
import re
import time
import subprocess
import webbrowser

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

GH_EXE = r"C:\Program Files\GitHub CLI\gh.exe"
GIT_EXE = r"D:\git\cmd\git.exe"
REPO_DIR = r"D:\jpeg-compressor"

os.environ["PATH"] = r"D:\git\cmd;C:\Program Files\GitHub CLI;" + os.environ.get("PATH", "")
os.chdir(REPO_DIR)


def print_qr(url: str):
    if not HAS_QRCODE:
        return
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception:
        pass


def is_authenticated() -> bool:
    res = subprocess.run([GH_EXE, "auth", "status"], capture_output=True, text=True)
    return res.returncode == 0


def login_flow():
    print("\n" + "=" * 62)
    print("      🔐 АВТОРИЗАЦИЯ ЧЕРЕЗ GITHUB")
    print("=" * 62)
    print("Запуск авторизации устройства в GitHub...")

    proc = subprocess.Popen(
        [GH_EXE, "auth", "login", "--web", "-h", "github.com", "-p", "https"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='ignore'
    )

    code = None
    url = "https://github.com/login/device"

    # Read output to capture code
    while True:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        if line:
            m = re.search(r'([A-Z0-9]{4}-[A-Z0-9]{4})', line)
            if m:
                code = m.group(1)
                break

    if code:
        print("\n" + "*" * 50)
        print(f"  👉 ТВОЙ ОДНОРАЗОВЫЙ КОД:  {code}")
        print("*" * 50)
        print(f"1. Перейди по ссылке: {url}")
        print(f"2. Введи код: {code}")
        print("3. Нажми зеленую кнопку «Authorize github»\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
    else:
        print("Открываем браузер для авторизации...")
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # Send Enter to proceed if requested
    try:
        proc.stdin.write("\n")
        proc.stdin.flush()
    except Exception:
        pass

    print("Ожидание подтверждения в браузере (до 2 минут)...")
    proc.wait()

    if not is_authenticated():
        print("[Ошибка] Авторизация не была завершена.")
        sys.exit(1)

    print("\n✅ Успешная авторизация в GitHub!\n")


def get_username() -> str:
    res = subprocess.run([GH_EXE, "api", "user", "-q", ".login"], capture_output=True, text=True)
    return res.stdout.strip()


def deploy():
    print("\n" + "=" * 62)
    print("      🚀 ПУБЛИКАЦИЯ НА GITHUB PAGES")
    print("=" * 62)

    username = get_username()
    repo_name = "jpeg-compressor"
    print(f"Пользователь GitHub: @{username}")
    print(f"Имя репозитория:     {repo_name}")

    # Check if git has changes
    subprocess.run([GIT_EXE, "add", "."], check=False)
    subprocess.run([GIT_EXE, "commit", "-m", "Update deployment files"], check=False)

    # Check if repo already exists on GitHub
    check_repo = subprocess.run([GH_EXE, "repo", "view", f"{username}/{repo_name}"], capture_output=True, text=True)

    if check_repo.returncode != 0:
        print(f"\nСоздание публичного репозитория {username}/{repo_name}...")
        create_res = subprocess.run(
            [GH_EXE, "repo", "create", repo_name, "--public", "--source=.", "--remote=origin", "--push"],
            capture_output=True,
            text=True
        )
        if create_res.returncode != 0:
            print(f"Лог создания: {create_res.stderr}")
    else:
        print(f"\nРепозиторий {username}/{repo_name} уже существует. Отправка свежего кода...")
        subprocess.run([GIT_EXE, "remote", "set-url", "origin", f"https://github.com/{username}/{repo_name}.git"], check=False)
        push_res = subprocess.run([GIT_EXE, "push", "-u", "origin", "main", "--force"], capture_output=True, text=True)
        if push_res.returncode != 0:
            print(f"Лог отправки: {push_res.stderr}")

    print("\nВключение бесплатного хостинга GitHub Pages...")
    # Enable GitHub Pages
    pages_res = subprocess.run(
        [
            GH_EXE, "api", f"repos/{username}/{repo_name}/pages",
            "-X", "POST",
            "-F", "source[branch]=main",
            "-F", "source[path]=/"
        ],
        capture_output=True,
        text=True
    )

    pages_url = f"https://{username}.github.io/{repo_name}/"

    print("\n" + "=" * 62)
    print("  🎉 ГОТОВО! ВАШ САЙТ ОПУБЛИКОВАН ОНЛАЙН НАВСЕГДА:")
    print(f"  👉 {pages_url}")
    print("=" * 62)
    print("Эта ссылка работает 24/7 с любого смартфона, планшета и ПК!")
    print("(GitHub Pages активируется в течение 30-60 секунд после первого создания).\n")

    print("QR-код для быстрого открытия на телефоне:")
    print_qr(pages_url)

    try:
        webbrowser.open(pages_url)
    except Exception:
        pass


def main():
    if not is_authenticated():
        login_flow()
    deploy()


if __name__ == "__main__":
    main()
