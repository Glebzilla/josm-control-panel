# -*- coding: utf-8 -*-
"""
JOSM & Nginx Control Panel
==========================

Панель для работы с НСПД / ПКК: обновление JOSM, Nginx-прокси на
nspd.gov.ru (порт 8081), проверка Java и плагинов JOSM.

Автор: Gleb — https://github.com/Glebzilla
Код написан с помощью ИИ (DeepSeek через OpenCode), проверен и
протестирован автором на реальной машине.

Лицензия: MIT (см. LICENSE).
"""
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import urllib.request
import os
import shutil
import subprocess
import zipfile
import re
import json
import queue
import tempfile
import time
import webbrowser


# ============ Константы ============
URL_JOSM          = "https://josm.openstreetmap.de/josm-latest.jar"
URL_NGINX_PAGE    = "https://nginx.org/en/download.html"
NGINX_FALLBACK    = "1.31.6"
URL_TEMURIN       = "https://adoptium.net/temurin/releases"
URL_ADOPTIUM_INFO = "https://api.adoptium.net/v3/info/available_releases"
URL_ADOPTIUM_LATEST = ("https://api.adoptium.net/v3/assets/latest/{feature}"
                       "/hotspot?architecture=x64&image_type=jdk&os=windows")
URL_AUTHOR      = "https://github.com/Glebzilla"

HOME        = os.path.expanduser("~")
DOWNLOADS   = os.path.join(HOME, "Downloads")
JOSM_DEST   = os.path.join(DOWNLOADS, "josm-latest.jar")
JOSM_NEW    = JOSM_DEST + ".new"
JOSM_PLUGINS_DIR = os.path.join(os.environ.get("APPDATA", ""), "JOSM", "plugins")

# Плагины, без которых панель/НСПД-сценарий не имеет смысла.
# Ссылки — официальные (JOSM dist / GitLab / GitHub releases).
JOSM_PLUGINS = [
    {
        "file": "russia-address-helper.jar",
        "name": "Russia Address Helper",
        "note": "адреса из ЕГРН — нужен для работы с НСПД / ПКК",
        "url": ("https://github.com/micronull/JOSM-Russia-address-helper-"
                "plugin/releases/latest/download/russia-address-helper.jar"),
    },
    {
        "file": "utilsplugin2.jar",
        "name": "UtilsPlugin2",
        "note": "обязательные утилиты; без него не работает Address Helper",
        "url": ("https://josm.openstreetmap.de/osmsvn/applications/editors/"
                "josm/dist/utilsplugin2.jar"),
    },
    {
        "file": "FilletTools.jar",
        "name": "FilletTools",
        "note": "скругление углов и сглаживание линий",
        "url": ("https://gitlab.com/darkonus/josm-fillet-tools/-/releases/"
                "permalink/latest/downloads/FilletTools.jar"),
    },
]

# ============ Палитра ============
BG      = "#1e1e2e"
CARD    = "#252537"
BORDER  = "#313244"
TEXT    = "#cdd6f4"
SUBTEXT = "#7f849c"
BLUE    = "#89b4fa"
GREEN   = "#a6e3a1"
YELLOW  = "#f9e2af"
RED     = "#f38ba8"
FONT    = "Segoe UI"

# ============ Патч для nginx.conf ============
NGINX_PATCH = """
    server {
        listen 8081;
        server_name localhost;
        location / {
            proxy_pass              https://nspd.gov.ru/;
            proxy_ssl_verify        off;
        }
    }
"""


# ============ Утилиты ============
def _http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def _http_head_size(url, timeout=15):
    """Размер файла по HEAD-запросу или 0, если узнать не удалось."""
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.headers.get("Content-Length") or 0)
    except Exception:
        return 0


def parse_ver(s):
    """'1.31.6' -> (1, 31, 6). Хвосты вроде '+1' отбрасываются."""
    out = []
    for part in re.split(r"[._+\-]", str(s)):
        if part.isdigit():
            out.append(int(part))
        else:
            break
    return tuple(out)


def parse_java_version(text):
    """'openjdk version "25.0.4.1" ...' -> '25.0.4.1', '1.8.0_392' -> '8.0.392'."""
    m = re.search(r'version "([^"]+)"', text or "")
    if not m:
        return None
    v = m.group(1).strip()
    if v.startswith("1."):
        base, _, upd = v.partition("_")
        parts = base.split(".")
        v = ".".join([parts[1]] + parts[2:])
        if upd:
            v += "." + upd
    return v


def get_nginx_versions(timeout=6):
    """Возвращает (mainline, stable).

    mainline — самая свежая версия со страницы загрузок,
    stable   — самая свежая с чётным вторым числом (конвенция nginx).

    Источники опрашиваются параллельно: nginx.org периодически отдаёт
    таймаут то по https, то по http, и последовательный перебор
    подвешивал проверку на полминуты.
    """
    sources = [
        ("https://nginx.org/download/", r"nginx-(\d+\.\d+\.\d+)"),
        ("https://api.github.com/repos/nginx/nginx/tags?per_page=100",
         r"release-(\d+\.\d+\.\d+)"),
        ("http://nginx.org/en/download.html", r"nginx-(\d+\.\d+\.\d+)"),
        (URL_NGINX_PAGE, r"nginx-(\d+\.\d+\.\d+)"),
        ("http://nginx.org/download/", r"nginx-(\d+\.\d+\.\d+)"),
    ]
    q = queue.Queue()

    def fetch(url, pattern):
        try:
            q.put((_http_get(url, timeout), pattern))
        except Exception:
            pass

    for url, pattern in sources:
        threading.Thread(target=fetch, args=(url, pattern),
                         daemon=True).start()

    deadline = time.time() + timeout + 2
    while True:
        left = deadline - time.time()
        if left <= 0:
            break
        try:
            html, pattern = q.get(timeout=left)
        except queue.Empty:
            break
        found = sorted(set(re.findall(pattern, html)), key=parse_ver)
        if found:
            stable = [v for v in found if parse_ver(v)[1] % 2 == 0]
            return found[-1], (stable[-1] if stable else None)
    return None, None


def get_latest_nginx_version(timeout=6):
    """Последняя mainline-версия Nginx или NGINX_FALLBACK."""
    return get_nginx_versions(timeout)[0] or NGINX_FALLBACK


def find_nginx_install():
    """Ищет Nginx в ~/Downloads и возвращает самую новую копию.

    Возвращает (dir, exe, conf, version) либо (None, None, None, None).
    """
    if not os.path.isdir(DOWNLOADS):
        return None, None, None, None

    best = None
    for name in os.listdir(DOWNLOADS):
        m = re.fullmatch(r"nginx-(\d+\.\d+\.\d+)", name)
        if not m:
            continue
        full = os.path.join(DOWNLOADS, name)
        exe = os.path.join(full, "nginx.exe")
        if not (os.path.isdir(full) and os.path.exists(exe)):
            continue
        ver = m.group(1)
        if best is None or parse_ver(ver) > parse_ver(best[3]):
            best = (full, exe, os.path.join(full, "conf", "nginx.conf"), ver)
    return best if best else (None, None, None, None)


# ---------- Java ----------
JAVA_ROOTS = [
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "Eclipse Adoptium"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "Java"),
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 "Eclipse Adoptium"),
]


def find_java_install():
    """Ищет установленную JDK в стандартных папках.

    Возвращает (java.exe, версия) или (None, None). Нужен потому, что PATH
    текущего процесса не обновляется после установки из панели.
    """
    best_exe, best_ver = None, ""
    for root in JAVA_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            exe = os.path.join(root, name, "bin", "java.exe")
            if not os.path.exists(exe):
                continue
            m = re.search(r"(\d+(?:\.\d+)*)", name)
            ver = m.group(1) if m else ""
            if best_exe is None or parse_ver(ver) > parse_ver(best_ver):
                best_exe, best_ver = exe, ver
    return best_exe, (best_ver or None)


def refresh_path_env():
    """Перечитывает PATH из реестра (после установки Java в текущем процессе).

    Без этого только что установленная JDK не видна: os.environ хранит
    окружение, полученное при запуске панели.
    """
    if os.name != "nt":
        return
    try:
        import winreg
    except ImportError:
        return

    def read(root, sub, name):
        try:
            with winreg.OpenKey(root, sub) as k:
                value, _ = winreg.QueryValueEx(k, name)
                return str(value)
        except OSError:
            return ""

    parts = [
        read(winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
             "Path"),
        read(winreg.HKEY_CURRENT_USER, "Environment", "Path"),
        os.environ.get("PATH", ""),
    ]
    merged = []
    for chunk in parts:
        for part in chunk.split(";"):
            part = os.path.expandvars(part.strip())
            if part and part not in merged:
                merged.append(part)
    os.environ["PATH"] = ";".join(merged)


def get_latest_java(installed_version=None, timeout=15):
    """Последняя сборка Temurin для нужной ветки.

    Если установленная ветка старее последней LTS — предлагаем LTS,
    иначе остаёмся в своей ветке (25.0.x -> 25.0.y, а не 27-ea).

    Возвращает dict: feature, version, link, size, lts.
    """
    info = json.loads(_http_get(URL_ADOPTIUM_INFO, timeout))
    lts = info.get("most_recent_lts")
    feature = None
    if installed_version:
        parts = parse_ver(installed_version)
        if parts:
            feature = parts[0]
    target = feature or lts
    if lts and (feature is None or feature < lts):
        target = lts
    if not target:
        return None

    data = json.loads(_http_get(URL_ADOPTIUM_LATEST.format(feature=target),
                                timeout))
    if not data:
        return None
    item = data[0]
    pkg = item.get("binary", {}).get("package", {})
    ver = (item.get("version_data", {}).get("openjdk_version")
           or item.get("release_name", ""))
    ver = str(ver).replace("jdk-", "").split("+")[0]

    link = pkg.get("link") or ""
    size = pkg.get("size") or 0
    if link.lower().endswith(".zip"):
        # API отдаёт ссылку на zip, но в том же релизе лежит MSI —
        # для тихой установки нужен именно он
        link = link[:-4] + ".msi"
        head = _http_head_size(link, timeout)
        if head:
            size = head
    if not link:
        return None
    return {
        "feature": target,
        "version": ver,
        "link": link,
        "size": size,
        "lts": target in (info.get("available_lts_releases") or []),
    }


# ---------- Плагины JOSM ----------
def _jar_plugin_version(path):
    """Версия плагина из META-INF/MANIFEST.MF (None, если не прочитать)."""
    try:
        with zipfile.ZipFile(path) as z:
            manifest = z.read("META-INF/MANIFEST.MF").decode("utf-8", "ignore")
    except Exception:
        return None
    for line in manifest.splitlines():
        if line.startswith("Plugin-Version:"):
            return line.split(":", 1)[1].strip() or None
    return None


def scan_josm_plugins():
    """Состояние нужных плагинов в профиле JOSM."""
    result = []
    for item in JOSM_PLUGINS:
        path = os.path.join(JOSM_PLUGINS_DIR, item["file"])
        installed = os.path.isfile(path)
        result.append(dict(item, path=path, installed=installed,
                           version=_jar_plugin_version(path)
                           if installed else None))
    return result


# ---------- Процессы / Nginx ----------
def run_silent(args, cwd=None, timeout=15):
    """Запуск без пайпов.

    nginx порождает дочерние процессы, которые наследуют stdout/stderr,
    поэтому capture_output приводит к ложному таймауту: команда давно
    отработала, а Python ждёт закрытия унаследованных пайпов.
    """
    kwargs = dict(cwd=cwd, stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  timeout=timeout)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(args, **kwargs)


def process_pids(image_name):
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}",
             "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if os.name == "nt" else 0)).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        cols = [c.strip().strip('"') for c in line.split('","')]
        if len(cols) >= 2 and cols[0].lower() == image_name.lower():
            try:
                pids.append(int(cols[1]))
            except ValueError:
                pass
    return pids


def wait_process_exit(image_name, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_pids(image_name):
            return True
        time.sleep(0.25)
    return not process_pids(image_name)


def stop_nginx_process(exe, timeout=15):
    """Мягко останавливает nginx и ждёт завершения процессов."""
    if not exe or not os.path.exists(exe):
        return False
    try:
        run_silent([exe, "-s", "stop"], cwd=os.path.dirname(exe),
                   timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return False
    return wait_process_exit("nginx.exe", 10)


# ============ Кнопка с hover ============
class FlatButton(tk.Frame):
    def __init__(self, parent, text, command, color, fg="#1e1e2e",
                 icon="", font_size=10):
        super().__init__(parent, bg=color, cursor="hand2")
        self.color = color
        self.command = command
        self.label = tk.Label(self, text=f"{icon}  {text}" if icon else text,
                              bg=color, fg=fg,
                              font=(FONT, font_size, "bold"),
                              padx=12, pady=6)
        self.label.pack(fill="both", expand=True)
        for w in (self, self.label):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.bind("<Button-1>", self._on_click)

    def _shade(self, hex_color, factor=0.88):
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
        r, g, b = int(r * factor), int(g * factor), int(b * factor)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _on_enter(self, _):
        if str(self["cursor"]) == "hand2":
            self._set_color(self._shade(self.color))

    def _on_leave(self, _):
        self._set_color(self.color)

    def _on_click(self, _):
        if str(self["cursor"]) == "hand2":
            self.command()

    def _set_color(self, c):
        self.config(bg=c)
        self.label.config(bg=c)

    def set_state(self, enabled):
        if enabled:
            self.config(cursor="hand2")
            self._set_color(self.color)
            self.label.config(fg="#1e1e2e")
        else:
            self.config(cursor="arrow")
            self._set_color(BORDER)
            self.label.config(fg=SUBTEXT)

    def set_text(self, text):
        self.label.config(text=text)


# ============ Приложение ============
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("JOSM & Nginx Control Panel")
        self.geometry("540x620")
        self.resizable(False, False)
        self.configure(bg=BG)

        # Кэш путей Nginx
        self.nginx_dir = None
        self.nginx_exe = None
        self.nginx_conf = None
        self.nginx_version = NGINX_FALLBACK
        self.nginx_latest = None

        # Java
        self.java_version = None
        self.java_latest = None

        # Плагины JOSM
        self.plugins_state = []
        self.plugins_link = None
        self._plugins_dlg = None
        self._plugins_msg = None
        self._plugins_btn = None
        self._plugins_installing = False

        # --- Нижняя панель (закреплена за низом окна) ---
        bottom = tk.Frame(self, bg=BG)
        bottom.pack(side="bottom", fill="x", padx=22, pady=(2, 14))
        credit = tk.Label(bottom, text="Сгенерировано ИИ · github.com/Glebzilla",
                          bg=BG, fg=SUBTEXT, font=(FONT, 8, "underline"),
                          cursor="hand2")
        credit.pack(side="left")
        credit.bind("<Button-1>", lambda e: webbrowser.open(URL_AUTHOR))
        tk.Label(bottom, text="· Ctrl+Q — выход", bg=BG, fg=SUBTEXT,
                 font=(FONT, 8)).pack(side="left", padx=(6, 0))
        FlatButton(bottom, "Закрыть", self.close_app, RED,
                   icon="✕", font_size=9).pack(side="right")

        # --- Заголовок ---
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=22, pady=(10, 4))
        tk.Label(header, text="Панель управления", bg=BG, fg=TEXT,
                 font=(FONT, 16, "bold")).pack(anchor="w")
        tk.Label(header, text="JOSM · Nginx · Java", bg=BG, fg=SUBTEXT,
                 font=(FONT, 10)).pack(anchor="w", pady=(2, 0))

        # --- JOSM ---
        card = tk.Frame(self, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", padx=22, pady=(10, 6))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=16, pady=9)

        tk.Label(inner, text="JOSM", bg=CARD, fg=TEXT,
                 font=(FONT, 11, "bold")).pack(anchor="w")

        row = tk.Frame(inner, bg=CARD); row.pack(anchor="w", pady=(4, 0))
        self.josm_dot = tk.Label(row, text="●", bg=CARD, fg=YELLOW,
                                 font=(FONT, 10))
        self.josm_dot.pack(side="left")
        self.josm_status = tk.Label(row, text="Проверка...", bg=CARD,
                                    fg=SUBTEXT, font=(FONT, 9),
                                    wraplength=420, justify="left")
        self.josm_status.pack(side="left", padx=(6, 0))

        self.plugins_link = tk.Label(
            inner, text="Плагины: проверка...", bg=CARD, fg=SUBTEXT,
            font=(FONT, 9, "underline"), cursor="hand2")
        self.plugins_link.pack(anchor="w", pady=(2, 0))
        self.plugins_link.bind("<Button-1>",
                               lambda e: self.show_plugins_dialog())

        style = ttk.Style(self)
        try: style.theme_use("clam")
        except tk.TclError: pass
        style.configure("Flat.Horizontal.TProgressbar",
                        troughcolor=BORDER, background=BLUE,
                        bordercolor=CARD, lightcolor=BLUE, darkcolor=BLUE,
                        thickness=6)

        self.josm_progress = ttk.Progressbar(
            inner, length=440, mode="determinate",
            style="Flat.Horizontal.TProgressbar")

        self.btn_josm_update = FlatButton(inner, "Проверка...",
                                          self.start_update_josm, BLUE,
                                          icon="⟳")
        self.btn_josm_update.pack(fill="x", pady=(0, 5))
        self.btn_josm_update.set_state(False)

        self.btn_josm_start = FlatButton(inner, "Запустить",
                                         self.start_josm, GREEN, icon="▶")
        self.btn_josm_start.pack(fill="x")

        # --- Nginx ---
        card = tk.Frame(self, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", padx=22, pady=(0, 6))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=16, pady=9)

        tk.Label(inner, text="Nginx", bg=CARD, fg=TEXT,
                 font=(FONT, 11, "bold")).pack(anchor="w")
        tk.Label(inner, text="Прокси для работы с НСПД / ПКК · порт 8081",
                 bg=CARD, fg=SUBTEXT,
                 font=(FONT, 9)).pack(anchor="w", pady=(2, 0))

        row = tk.Frame(inner, bg=CARD); row.pack(anchor="w", pady=(4, 8))
        self.nginx_dot = tk.Label(row, text="●", bg=CARD, fg=YELLOW,
                                  font=(FONT, 10))
        self.nginx_dot.pack(side="left")
        self.nginx_status = tk.Label(row, text="Проверка...", bg=CARD,
                                     fg=SUBTEXT, font=(FONT, 9),
                                     wraplength=420, justify="left")
        self.nginx_status.pack(side="left", padx=(6, 0))

        self.btn_nginx_install = FlatButton(inner, "Установить",
                                            self.install_nginx, YELLOW,
                                            icon="⬇")
        self.btn_nginx_install.pack(fill="x", pady=(0, 5))

        nginx_run_row = tk.Frame(inner, bg=CARD)
        nginx_run_row.pack(fill="x")
        self.btn_nginx_start = FlatButton(nginx_run_row, "Запустить",
                                          self.start_nginx, GREEN, icon="▶")
        self.btn_nginx_start.pack(side="left", fill="x", expand=True,
                                  padx=(0, 3))
        self.btn_nginx_stop = FlatButton(nginx_run_row, "Остановить",
                                         self.stop_nginx, RED, icon="■")
        self.btn_nginx_stop.pack(side="right", fill="x", expand=True,
                                 padx=(3, 0))

        # --- Java ---
        card = tk.Frame(self, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", padx=22, pady=(0, 6))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=16, pady=9)

        tk.Label(inner, text="Java", bg=CARD, fg=TEXT,
                 font=(FONT, 11, "bold")).pack(anchor="w")

        row = tk.Frame(inner, bg=CARD); row.pack(anchor="w", pady=(4, 8))
        self.java_dot = tk.Label(row, text="●", bg=CARD, fg=YELLOW,
                                 font=(FONT, 10))
        self.java_dot.pack(side="left")
        self.java_status = tk.Label(row, text="Проверка...", bg=CARD,
                                    fg=SUBTEXT, font=(FONT, 9),
                                    wraplength=420, justify="left")
        self.java_status.pack(side="left", padx=(6, 0))

        link = tk.Label(inner, text="Скачать Temurin (OpenJDK) ↗",
                        bg=CARD, fg=BLUE, font=(FONT, 9, "underline"),
                        cursor="hand2")
        link.pack(anchor="w")
        link.bind("<Button-1>", lambda e: webbrowser.open(URL_TEMURIN))

        self.btn_java_update = FlatButton(inner, "Проверка...",
                                          self.update_java, BLUE, icon="⟳")
        self.btn_java_update.pack(fill="x", pady=(8, 0))
        self.btn_java_update.set_state(False)

        self.bind("<Control-q>", lambda e: self.close_app())

        # Высоту подгоняем под экран: на 1280x720 окно должно влезать
        self._fit_window()

        # Автопроверки
        self.after(150, self.check_all)

    def _fit_window(self):
        self.update_idletasks()
        need = self.winfo_reqheight()
        avail = self.winfo_screenheight() - 80
        self.geometry(f"540x{min(need, avail)}")

    # =================================================
    #  Общие утилиты
    # =================================================
    def _set_status(self, label, dot, text, color):
        label.config(text=text)
        dot.config(fg=color)

    def _resolve_nginx_version(self):
        """Целевая версия установки: не ниже уже установленной."""
        return (self.nginx_latest or self.nginx_version
                or get_latest_nginx_version() or NGINX_FALLBACK)

    def is_nginx_running(self):
        return bool(process_pids("nginx.exe"))

    def _ensure_nginx_paths(self):
        """Подхватывает установленный Nginx, если автопроверка ещё не успела."""
        if self.nginx_exe and os.path.exists(self.nginx_exe):
            return True
        nd, exe, conf, ver = find_nginx_install()
        if not exe:
            return False
        self.nginx_dir, self.nginx_exe, self.nginx_conf = nd, exe, conf
        self.nginx_version = ver
        return True

    # =================================================
    #  JOSM
    # =================================================
    def check_josm(self):
        self._set_status(self.josm_status, self.josm_dot,
                         "Проверка...", SUBTEXT)
        threading.Thread(target=self._check_josm_worker,
                         daemon=True).start()

    def _check_josm_worker(self):
        try:
            req = urllib.request.Request(URL_JOSM, method="HEAD")
            with urllib.request.urlopen(req, timeout=15) as r:
                remote_size = int(r.headers.get("Content-Length", 0) or 0)

            if not os.path.exists(JOSM_DEST):
                result = ("missing", remote_size)
            elif remote_size and os.path.getsize(JOSM_DEST) != remote_size:
                result = ("outdated", remote_size)
            elif remote_size == 0:
                result = ("unknown", None)
            else:
                result = ("current", remote_size)
        except Exception as e:
            result = ("error", str(e))
        self.after(0, self._on_josm_check_done, result)

    def _on_josm_check_done(self, result):
        kind, info = result
        if kind == "current":
            self._set_status(self.josm_status, self.josm_dot,
                             "Актуальная версия", GREEN)
            self.btn_josm_update.set_text("Переустановить")
        elif kind == "outdated":
            self._set_status(self.josm_status, self.josm_dot,
                             "Доступно обновление", YELLOW)
            self.btn_josm_update.set_text("Обновить")
        elif kind == "missing":
            self._set_status(self.josm_status, self.josm_dot,
                             "JOSM не найден", YELLOW)
            self.btn_josm_update.set_text("Скачать")
        elif kind == "unknown":
            self._set_status(self.josm_status, self.josm_dot,
                             "Готово к обновлению", SUBTEXT)
            self.btn_josm_update.set_text("Обновить")
        else:
            self._set_status(self.josm_status, self.josm_dot,
                             f"Ошибка: {info}", RED)
            self.btn_josm_update.set_text("Обновить")
        self.btn_josm_update.set_state(True)

    # ---------- Плагины JOSM ----------
    def check_plugins(self):
        """Проверяет наличие нужных плагинов в профиле JOSM."""
        self.plugins_state = scan_josm_plugins()
        missing = [p for p in self.plugins_state if not p["installed"]]
        total = len(self.plugins_state)
        if self.plugins_link is None:
            return
        if missing:
            self.plugins_link.config(
                text=f"Плагины: {total - len(missing)} из {total} · "
                     "нажмите, чтобы установить", fg=YELLOW)
        else:
            self.plugins_link.config(
                text=f"Плагины: все {total} установлены", fg=GREEN)

    def show_plugins_dialog(self):
        self.check_plugins()
        dlg = self._plugins_dlg
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            dlg.focus_force()
            return

        win = tk.Toplevel(self)
        win.title("Плагины JOSM")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self)
        win.geometry("520x430")
        self._plugins_dlg = win

        tk.Label(win, text="Плагины JOSM", bg=BG, fg=TEXT,
                 font=(FONT, 14, "bold")).pack(anchor="w", padx=22,
                                               pady=(20, 2))
        tk.Label(win, text="Нужны для работы с НСПД / ПКК. "
                           "Ставятся в профиль JOSM.",
                 bg=BG, fg=SUBTEXT,
                 font=(FONT, 9)).pack(anchor="w", padx=22)

        box = tk.Frame(win, bg=CARD, highlightbackground=BORDER,
                       highlightthickness=1)
        box.pack(fill="x", padx=22, pady=14)
        inner = tk.Frame(box, bg=CARD)
        inner.pack(fill="x", padx=16, pady=14)

        for p in self.plugins_state:
            ok = p["installed"]
            row = tk.Frame(inner, bg=CARD)
            row.pack(fill="x", pady=(0, 10))
            head = tk.Frame(row, bg=CARD)
            head.pack(fill="x")
            tk.Label(head, text=f"{'✓' if ok else '✕'}  {p['name']}",
                     bg=CARD, fg=GREEN if ok else RED,
                     font=(FONT, 10, "bold")).pack(side="left")
            ver = (f"v{p['version']}" if p.get("version")
                   else ("не установлен" if not ok else ""))
            tk.Label(head, text=ver, bg=CARD, fg=SUBTEXT,
                     font=(FONT, 9)).pack(side="right")
            tk.Label(row, text=p["note"], bg=CARD, fg=SUBTEXT,
                     font=(FONT, 9), wraplength=440,
                     justify="left").pack(anchor="w", padx=(18, 0))

        self._plugins_msg = tk.Label(
            win, text=f"Папка: {JOSM_PLUGINS_DIR}", bg=BG, fg=SUBTEXT,
            font=(FONT, 8), wraplength=460, justify="left")
        self._plugins_msg.pack(anchor="w", padx=22)

        missing = [p for p in self.plugins_state if not p["installed"]]
        label = (f"Установить недостающие ({len(missing)})" if missing
                 else "Переустановить все")
        mode = "missing" if missing else "all"
        self._plugins_btn = FlatButton(win, label,
                                       lambda: self.install_plugins(mode),
                                       BLUE, icon="⬇")
        self._plugins_btn.pack(fill="x", padx=22, pady=(12, 6))
        self._plugins_btn.set_state(not self._plugins_installing)

        FlatButton(win, "Закрыть", win.destroy, RED,
                   icon="✕", font_size=9).pack(fill="x", padx=22,
                                               pady=(0, 18))

    def _plugin_dialog_update(self, text, color=SUBTEXT):
        dlg = self._plugins_dlg
        if dlg is not None and dlg.winfo_exists() and \
                self._plugins_msg is not None:
            self._plugins_msg.config(text=text, fg=color)

    def install_plugins(self, mode="missing"):
        if self._plugins_installing:
            return
        targets = [p for p in scan_josm_plugins()
                   if mode == "all" or not p["installed"]]
        if not targets:
            self._plugin_dialog_update("Все плагины уже установлены.", GREEN)
            return
        self._plugins_installing = True
        if self._plugins_btn is not None and \
                self._plugins_btn.winfo_exists():
            self._plugins_btn.set_state(False)
            self._plugins_btn.set_text("Установка...")
        self._plugin_dialog_update("Подготовка...", YELLOW)
        threading.Thread(target=self._install_plugins_worker,
                         args=(targets,), daemon=True).start()

    def _install_plugins_worker(self, targets):
        errors = []
        try:
            os.makedirs(JOSM_PLUGINS_DIR, exist_ok=True)
        except Exception as e:
            self.after(0, self._on_plugins_done, [f"папка плагинов: {e}"])
            return

        for i, p in enumerate(targets, 1):
            self.after(0, self._plugin_dialog_update,
                       f"Скачивание {p['name']} ({i}/{len(targets)})...",
                       YELLOW)
            tmp = p["path"] + ".part"
            try:
                urllib.request.urlretrieve(p["url"], tmp)
                os.replace(tmp, p["path"])
            except PermissionError:
                errors.append(f"{p['name']}: файл занят — закройте JOSM "
                              "и повторите")
            except Exception as e:
                errors.append(f"{p['name']}: {e}")
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        self.after(0, self._on_plugins_done, errors)

    def _on_plugins_done(self, errors):
        self._plugins_installing = False
        self.check_plugins()
        btn = self._plugins_btn
        if btn is not None and btn.winfo_exists():
            btn.set_state(True)
            missing = [p for p in self.plugins_state if not p["installed"]]
            btn.set_text(f"Установить недостающие ({len(missing)})"
                         if missing else "Переустановить все")
        if errors:
            self._plugin_dialog_update("Не всё удалось установить", RED)
            messagebox.showerror(
                "Плагины JOSM",
                "Не удалось установить:\n\n" + "\n".join(errors) +
                "\n\nЕсли JOSM запущен — закройте его и повторите.")
        else:
            self._plugin_dialog_update("Готово — перезапустите JOSM", GREEN)
            messagebox.showinfo(
                "Плагины JOSM",
                "Плагины установлены.\n\n"
                "Перезапустите JOSM, чтобы они загрузились.")

    def start_update_josm(self):
        self.btn_josm_update.set_state(False)
        self.btn_josm_update.set_text("Обновление...")
        self.josm_progress["value"] = 0
        self.josm_progress.pack(fill="x", pady=(6, 8),
                                before=self.btn_josm_update)
        self._fit_window()
        self._set_status(self.josm_status, self.josm_dot,
                         "Скачивание...", BLUE)
        threading.Thread(target=self._update_josm_worker,
                         daemon=True).start()

    def _update_josm_worker(self):
        try:
            os.makedirs(DOWNLOADS, exist_ok=True)

            def report(block_num, block_size, total_size):
                if total_size > 0:
                    p = min(100, block_num * block_size * 100 / total_size)
                    self.after(0, self._set_josm_progress, p)

            urllib.request.urlretrieve(URL_JOSM, JOSM_NEW,
                                       reporthook=report)
            self.after(0, self._set_status, self.josm_status,
                       self.josm_dot, "Установка...", BLUE)

            if os.path.exists(JOSM_DEST):
                backup = JOSM_DEST + ".bak"
                try:
                    if os.path.exists(backup):
                        os.remove(backup)
                    os.replace(JOSM_DEST, backup)
                except OSError:
                    pass
            shutil.move(JOSM_NEW, JOSM_DEST)
            self.after(0, self._on_josm_update_done, None)
        except Exception as e:
            self.after(0, self._on_josm_update_done, str(e))

    def _set_josm_progress(self, v):
        self.josm_progress["value"] = v
        self._set_status(self.josm_status, self.josm_dot,
                         f"Скачивание... {v:.0f}%", BLUE)

    def _on_josm_update_done(self, error):
        if error:
            self._set_status(self.josm_status, self.josm_dot,
                             "Ошибка!", RED)
            self.btn_josm_update.set_text("Повторить")
            messagebox.showerror("Ошибка JOSM",
                                 f"Не удалось обновить:\n{error}")
        else:
            self.josm_progress["value"] = 100
            self._set_status(self.josm_status, self.josm_dot,
                             "Обновление завершено", GREEN)
            self.btn_josm_update.set_text("Обновить снова")
        self.btn_josm_update.set_state(True)
        self.josm_progress.pack_forget()
        self._fit_window()

    def start_josm(self):
        if not os.path.exists(JOSM_DEST):
            self._set_status(self.josm_status, self.josm_dot,
                             "Файл JOSM не найден — сначала обновите", RED)
            messagebox.showwarning(
                "JOSM не найден",
                f"Файл не существует:\n{JOSM_DEST}\n\n"
                "Нажмите «Скачать», чтобы загрузить его.")
            return
        try:
            flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
            subprocess.Popen(["java", "-jar", JOSM_DEST],
                             creationflags=flags, close_fds=True)
            self._set_status(self.josm_status, self.josm_dot,
                             "JOSM запускается...", GREEN)
        except FileNotFoundError:
            self._set_status(self.josm_status, self.josm_dot,
                             "Java не найдена в PATH", RED)
            messagebox.showerror(
                "Java не найдена",
                "Не удалось запустить java.\n\n"
                "Установите Java (Temurin) и добавьте её в PATH.")
        except Exception as e:
            self._set_status(self.josm_status, self.josm_dot,
                             f"Ошибка: {e}", RED)

    # =================================================
    #  Nginx
    # =================================================
    def check_nginx(self):
        self._set_status(self.nginx_status, self.nginx_dot,
                         "Проверка...", SUBTEXT)
        threading.Thread(target=self._check_nginx_worker,
                         daemon=True).start()

    def _check_nginx_worker(self):
        nd, exe, conf, ver = find_nginx_install()
        latest, stable = get_nginx_versions()
        self.after(0, self._on_nginx_check_done,
                   (nd, exe, conf, ver, latest, stable))

    def _on_nginx_check_done(self, result):
        nd, exe, conf, ver, latest, stable = result
        self.nginx_latest = latest

        if exe:
            self.nginx_dir, self.nginx_exe, self.nginx_conf = nd, exe, conf
            self.nginx_version = ver

            patched = False
            try:
                with open(conf, "r", encoding="utf-8", errors="ignore") as f:
                    patched = "nspd.gov.ru" in f.read()
            except Exception:
                pass
            cfg = "конфиг настроен" if patched else "конфиг НЕ пропатчен"

            if latest and parse_ver(latest) > parse_ver(ver):
                self._set_status(self.nginx_status, self.nginx_dot,
                                 f"{ver}, {cfg}. Доступно обновление "
                                 f"до {latest}", YELLOW)
                self.btn_nginx_install.set_text(f"Обновить до {latest}")
            elif latest:
                self._set_status(self.nginx_status, self.nginx_dot,
                                 f"{ver}, {cfg}. Версия актуальная", GREEN)
                self.btn_nginx_install.set_text("Переустановить")
            else:
                self._set_status(self.nginx_status, self.nginx_dot,
                                 f"{ver}, {cfg}. Не удалось проверить "
                                 "последнюю версию", YELLOW)
                self.btn_nginx_install.set_text("Переустановить")
        else:
            self.nginx_dir = self.nginx_exe = self.nginx_conf = None
            if latest:
                self._set_status(self.nginx_status, self.nginx_dot,
                                 f"Nginx не найден. Доступна версия "
                                 f"{latest}", YELLOW)
                self.btn_nginx_install.set_text(f"Установить {latest}")
            else:
                self._set_status(self.nginx_status, self.nginx_dot,
                                 "Nginx не найден, версия не определена",
                                 YELLOW)
                self.btn_nginx_install.set_text("Установить")
        self.btn_nginx_install.set_state(True)

    def install_nginx(self):
        target = self.nginx_latest
        if target and self.nginx_version and \
                parse_ver(target) <= parse_ver(self.nginx_version):
            target = None  # уже актуальная — просто переустановить
        self.btn_nginx_install.set_state(False)
        self.btn_nginx_install.set_text("Установка...")
        self._set_status(self.nginx_status, self.nginx_dot,
                         "Определение версии...", YELLOW)
        threading.Thread(target=self._install_nginx_worker,
                         args=(target,), daemon=True).start()

    def _install_nginx_worker(self, target_version=None):
        try:
            os.makedirs(DOWNLOADS, exist_ok=True)

            version = target_version or self._resolve_nginx_version()
            urls = [f"https://nginx.org/download/nginx-{version}.zip",
                    f"http://nginx.org/download/nginx-{version}.zip"]
            nginx_dir = os.path.join(DOWNLOADS, f"nginx-{version}")
            nginx_exe = os.path.join(nginx_dir, "nginx.exe")
            nginx_conf = os.path.join(nginx_dir, "conf", "nginx.conf")
            zip_path = os.path.join(DOWNLOADS, f"nginx-{version}.zip")

            # рабочий конфиг запоминаем до удаления старой папки
            old_conf_bytes = None
            if self.nginx_conf and os.path.exists(self.nginx_conf):
                try:
                    with open(self.nginx_conf, "rb") as f:
                        old_conf_bytes = f.read()
                except OSError:
                    pass

            # работающий nginx держит файлы — останавливаем
            if self.is_nginx_running() and self.nginx_exe and \
                    os.path.exists(self.nginx_exe):
                self.after(0, self._set_status, self.nginx_status,
                           self.nginx_dot, "Остановка Nginx...", YELLOW)
                stop_nginx_process(self.nginx_exe)

            self.after(0, self._set_status, self.nginx_status,
                       self.nginx_dot,
                       f"Скачивание Nginx {version}...", YELLOW)

            def report(block_num, block_size, total_size):
                if total_size > 0:
                    p = min(100, block_num * block_size * 100 / total_size)
                    self.after(0, self._set_status, self.nginx_status,
                               self.nginx_dot,
                               f"Скачивание... {p:.0f}%", YELLOW)

            last_error = None
            for url_zip in urls:
                try:
                    urllib.request.urlretrieve(url_zip, zip_path,
                                               reporthook=report)
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    try:
                        os.remove(zip_path)
                    except OSError:
                        pass
            if last_error:
                raise last_error

            self.after(0, self._set_status, self.nginx_status,
                       self.nginx_dot, "Распаковка...", YELLOW)
            if os.path.exists(nginx_dir):
                shutil.rmtree(nginx_dir, ignore_errors=True)
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(DOWNLOADS)

            if not os.path.exists(nginx_exe):
                raise FileNotFoundError(
                    f"После распаковки nginx.exe не найден:\n{nginx_exe}")

            # возвращаем рабочий конфиг на место
            if old_conf_bytes:
                self.after(0, self._set_status, self.nginx_status,
                           self.nginx_dot, "Перенос конфига...", YELLOW)
                try:
                    with open(nginx_conf, "wb") as f:
                        f.write(old_conf_bytes)
                except OSError:
                    pass

            self.after(0, self._set_status, self.nginx_status,
                       self.nginx_dot, "Патч конфига...", YELLOW)
            self._patch_nginx_conf(nginx_conf)

            self.nginx_dir = nginx_dir
            self.nginx_exe = nginx_exe
            self.nginx_conf = nginx_conf
            self.nginx_version = version

            # старые версии больше не нужны
            for name in os.listdir(DOWNLOADS):
                m = re.fullmatch(r"nginx-(\d+\.\d+\.\d+)", name)
                if m and m.group(1) != version:
                    shutil.rmtree(os.path.join(DOWNLOADS, name),
                                  ignore_errors=True)

            self.after(0, self._on_nginx_install_done, None)
        except Exception as e:
            self.after(0, self._on_nginx_install_done, str(e))

    def _patch_nginx_conf(self, conf_path):
        if not os.path.exists(conf_path):
            raise FileNotFoundError(f"Конфиг не найден: {conf_path}")

        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        if "nspd.gov.ru" in content:
            return

        last_brace = content.rfind("}")
        if last_brace == -1:
            raise ValueError("Не найден закрывающий '}' в nginx.conf")

        patched = content[:last_brace] + NGINX_PATCH + "\n" + content[last_brace:]

        backup = conf_path + ".bak"
        if not os.path.exists(backup):
            shutil.copy2(conf_path, backup)

        with open(conf_path, "w", encoding="utf-8") as f:
            f.write(patched)

    def _on_nginx_install_done(self, error):
        self.btn_nginx_install.set_state(True)
        if error:
            self._set_status(self.nginx_status, self.nginx_dot,
                             "Ошибка установки", RED)
            self.btn_nginx_install.set_text("Повторить")
            messagebox.showerror("Ошибка Nginx",
                                 f"Не удалось установить:\n{error}")
            return

        messagebox.showinfo(
            "Nginx установлен",
            f"Nginx {self.nginx_version} установлен.\n\n"
            f"Папка: {self.nginx_dir}\n"
            "Рабочий конфиг сохранён, прокси на nspd.gov.ru — порт 8081.\n"
            "Если Nginx был запущен, он остановлен — запустите заново.")
        self.check_nginx()

    def start_nginx(self):
        if not self._ensure_nginx_paths():
            messagebox.showwarning("Nginx", "Nginx не установлен.")
            return
        if self.is_nginx_running():
            self._set_status(self.nginx_status, self.nginx_dot,
                             "Nginx уже запущен", GREEN)
            return
        try:
            nginx_dir = os.path.dirname(self.nginx_exe)
            flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
            subprocess.Popen([self.nginx_exe], cwd=nginx_dir,
                             creationflags=flags, close_fds=True)
            self._set_status(self.nginx_status, self.nginx_dot,
                             "Nginx запущен (порт 80 и 8081)", GREEN)
        except Exception as e:
            self._set_status(self.nginx_status, self.nginx_dot,
                             f"Ошибка: {e}", RED)

    def stop_nginx(self):
        if not self._ensure_nginx_paths():
            messagebox.showwarning("Nginx", "Nginx не установлен.")
            return
        if not self.is_nginx_running():
            self._set_status(self.nginx_status, self.nginx_dot,
                             "Nginx не запущен", SUBTEXT)
            return
        self._set_status(self.nginx_status, self.nginx_dot,
                         "Остановка Nginx...", YELLOW)
        self.btn_nginx_stop.set_state(False)
        threading.Thread(target=self._stop_nginx_worker,
                         daemon=True).start()

    def _stop_nginx_worker(self):
        ok = stop_nginx_process(self.nginx_exe)
        self.after(0, self._on_nginx_stop_done, ok)

    def _on_nginx_stop_done(self, ok):
        self.btn_nginx_stop.set_state(True)
        if ok:
            self._set_status(self.nginx_status, self.nginx_dot,
                             "Nginx остановлен", SUBTEXT)
        else:
            self._set_status(self.nginx_status, self.nginx_dot,
                             "Не удалось остановить Nginx", RED)
            messagebox.showwarning(
                "Nginx",
                "Процесс nginx.exe не завершился.\n\n"
                "Если Nginx запущен от имени администратора, остановите "
                "его вручную — или закройте и запустите панель от админа.")

    # =================================================
    #  Java
    # =================================================
    def check_java(self):
        self._set_status(self.java_status, self.java_dot,
                         "Проверка...", SUBTEXT)
        self.btn_java_update.set_state(False)
        threading.Thread(target=self._check_java_worker,
                         daemon=True).start()

    def _check_java_worker(self):
        installed = None
        raw = ""
        try:
            r = subprocess.run(["java", "-version"],
                               capture_output=True, text=True, timeout=10)
            raw = (r.stderr or "") + (r.stdout or "")
            installed = parse_java_version(raw)
        except FileNotFoundError:
            pass
        except Exception as e:
            raw = str(e)

        exe = None
        if not installed:
            exe, _dir_ver = find_java_install()
            if exe:
                try:
                    r = subprocess.run([exe, "-version"],
                                       capture_output=True, text=True,
                                       timeout=15)
                    installed = parse_java_version(
                        (r.stderr or "") + (r.stdout or ""))
                except Exception:
                    installed = None

        latest = None
        error = None
        if installed:
            try:
                latest = get_latest_java(installed)
            except Exception as e:
                error = str(e)
        result = (installed, latest, raw.strip(), exe, error)
        self.after(0, self._on_java_check_done, result)

    def _on_java_check_done(self, result):
        installed, latest, raw, exe, error = result
        self.java_latest = latest

        if not installed:
            self.btn_java_update.set_state(True)
            if latest:
                self._set_status(self.java_status, self.java_dot,
                                 f"Java не найдена в PATH. Доступна "
                                 f"{latest['version']} "
                                 f"(LTS {latest['feature']})", RED)
                self.btn_java_update.set_text(
                    f"Установить {latest['version']}")
            else:
                self._set_status(self.java_status, self.java_dot,
                                 "Java не найдена в PATH. Установите "
                                 "Temurin по ссылке ниже.", RED)
                self.btn_java_update.set_text("Установить")
            return

        lts_tag = " LTS" if latest and latest.get("lts") else ""
        if latest and parse_ver(latest["version"]) > parse_ver(installed):
            self._set_status(self.java_status, self.java_dot,
                             f"Установлена {installed}. Доступно "
                             f"обновление до {latest['version']}{lts_tag}",
                             YELLOW)
            self.btn_java_update.set_text(f"Обновить до {latest['version']}")
        elif latest:
            self._set_status(self.java_status, self.java_dot,
                             f"Java {installed}{lts_tag}. Версия актуальная",
                             GREEN)
            self.btn_java_update.set_text("Переустановить")
        else:
            self._set_status(self.java_status, self.java_dot,
                             f"Java {installed}. Не удалось проверить "
                             f"актуальную версию" +
                             (f": {error}" if error else ""), YELLOW)
            self.btn_java_update.set_text("Переустановить")
        self.btn_java_update.set_state(True)

    def update_java(self):
        if not self.java_latest:
            messagebox.showwarning("Java",
                                   "Не удалось определить последнюю версию "
                                   "Java.\nПроверьте подключение к интернету.")
            return
        target = self.java_latest
        size = ""
        if target.get("size"):
            size = f"\nРазмер: {target['size'] / 1024 / 1024:.0f} МБ"
        if not messagebox.askyesno(
                "Обновление Java",
                f"Скачать и установить Temurin "
                f"{target['version']} (JDK {target['feature']})?\n\n"
                f"Файл будет сохранён в папку Downloads.{size}\n"
                "Появится запрос прав администратора (UAC)."):
            return
        self.btn_java_update.set_state(False)
        self.btn_java_update.set_text("Установка...")
        self._set_status(self.java_status, self.java_dot,
                         "Скачивание Java...", YELLOW)
        threading.Thread(target=self._update_java_worker,
                         args=(dict(target),), daemon=True).start()

    def _update_java_worker(self, target):
        try:
            if not target.get("link"):
                raise ValueError("в ответе Adoptium нет ссылки на MSI")
            os.makedirs(DOWNLOADS, exist_ok=True)
            fname = target["link"].split("?")[0].rsplit("/", 1)[-1]
            msi = os.path.join(DOWNLOADS, fname or
                               f"temurin-{target['feature']}.msi")

            def report(block_num, block_size, total_size):
                if total_size > 0:
                    p = min(100, block_num * block_size * 100 / total_size)
                    self.after(0, self._set_status, self.java_status,
                               self.java_dot,
                               f"Скачивание Java... {p:.0f}%", YELLOW)

            urllib.request.urlretrieve(target["link"], msi, reporthook=report)

            self.after(0, self._set_status, self.java_status, self.java_dot,
                       "Установка Java (UAC)...", YELLOW)

            script = os.path.join(tempfile.gettempdir(), "temurin_install.ps1")
            args = ",".join(["FeatureMain", "FeatureEnvironment",
                             "FeatureJarFileRunWith", "FeatureJavaHome"])
            with open(script, "w", encoding="ascii", newline="\r\n") as f:
                f.write("$ErrorActionPreference = 'Stop'\n")
                f.write("$p = Start-Process -FilePath 'msiexec.exe' "
                        "-ArgumentList @('/i', '\"%s\"', '/qn', '/norestart', "
                        "'ADDLOCAL=%s') -Verb RunAs -Wait -PassThru\n"
                        % (msi.replace("'", "''"), args))
                f.write("exit $p.ExitCode\n")

            r = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", script],
                capture_output=True, text=True, timeout=1800)
            if r.returncode not in (0, 3010):
                raise RuntimeError(
                    f"msiexec вернул код {r.returncode}. "
                    f"{(r.stderr or r.stdout or '').strip()[:300]}")

            try:
                os.remove(script)
            except OSError:
                pass

            refresh_path_env()
            self.after(0, self._on_java_update_done, None)
        except Exception as e:
            self.after(0, self._on_java_update_done, str(e))

    def _on_java_update_done(self, error):
        self.btn_java_update.set_state(True)
        if error:
            self._set_status(self.java_status, self.java_dot,
                             "Ошибка установки Java", RED)
            self.btn_java_update.set_text("Повторить")
            messagebox.showerror("Ошибка Java",
                                 f"Не удалось обновить Java:\n{error}")
            return
        messagebox.showinfo(
            "Java обновлена",
            "Установка завершена.\n\n"
            "Новая версия подхватится после перезапуска панели и JOSM — "
            "переменная PATH обновится в новых процессах.")
        self.check_java()

    # =================================================
    #  Прочее
    # =================================================
    def check_all(self):
        self.check_josm()
        self.check_nginx()
        self.check_java()
        self.check_plugins()

    def close_app(self):
        self.destroy()


if __name__ == "__main__":
    App().mainloop()