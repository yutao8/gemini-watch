#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""Gemini / Antigravity 可用性监控。

用法:
    python gemini_watch.py            # 托盘常驻（托盘图标显示绿/黄/红）
    python gemini_watch.py --no-tray  # 纯后台，不显示托盘
    python gemini_watch.py --once     # 只检测一轮，适合先验证

判定规则（单个目标）:
    HTTP 2xx/3xx/401/403/404  -> 服务在线（401/403 只是未带凭证，网络是通的）
    HTTP 429                  -> 降级（限流 / 额度）
    HTTP 5xx                  -> 不可用（服务端故障，Antigravity 常见 503）
    超时 / DNS / TLS / 连接失败 -> 不可用（网络或代理问题）

连续 fail_threshold 次失败才提醒（默认 1 次即报，设为 2 可过滤抖动）；
连续 recover_threshold 次成功才算恢复；repeat_alert_minutes=0 表示一次故障只提醒一次，
大于 0 则表示持续异常每隔该分钟数再提醒。

状态面板: http://127.0.0.1:47131 （可用率 / 延迟曲线 / 故障时间线）

配置文件: Windows 为 %APPDATA%\GeminiWatch\config.json，
macOS 为 ~/Library/Application Support/GeminiWatch/config.json，
支持热加载（改完保存即生效，端口变化会自动重启面板）。
"""

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests


IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def _app_dir():
    """exe 运行时用 exe 所在目录（配置/日志可改），脚本运行时用脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _resource_dir():
    """PyInstaller onefile 解压目录，用于读取打包进去的 panel.html。"""
    return getattr(sys, "_MEIPASS", _app_dir())


def _data_dir():
    """运行数据统一放在系统用户目录，不污染程序所在目录。"""
    override = os.environ.get("GEMINI_WATCH_HOME")
    if override:
        return os.path.abspath(override)
    home = os.path.expanduser("~")
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or home
    elif IS_MAC:
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, "GeminiWatch")


BASE_DIR = _app_dir()
DATA_DIR = _data_dir()
RESOURCE_DIR = _resource_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOCK_PATH = os.path.join(DATA_DIR, "gemini_watch.lock")
PANEL_PATH = os.path.join(RESOURCE_DIR, "panel.html")
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_HISTORY_BYTES = 5 * 1024 * 1024
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

DEFAULT_CONFIG = {
    "interval_seconds": 60,
    "timeout_seconds": 5,
    "proxy": "",
    "fail_threshold": 1,
    "recover_threshold": 2,
    "repeat_alert_minutes": 0,
    "notify": {"enabled": True, "sound": True},
    "log_file": "gemini_watch.log",
    "history_file": "history.jsonl",
    "diagnose": {
        "enabled": True,
        "proxy_port": 7890,
        "processes": ["clash", "mihomo", "verge", "v2ray", "xray", "sing-box"],
    },
    "panel": {"enabled": True, "host": "127.0.0.1", "port": 47131},
    "tray": {"enabled": True},
    "path_check": {"enabled": True, "require_proxy": False, "proxy_port": 7890},
    "region_check": {
        "enabled": False,
        "api_key": "",
        "model": "gemini-2.5-flash",
        "interval_minutes": 10,
    },
    "targets": [
        {"name": "网络连通", "url": "http://www.gstatic.com/generate_204", "method": "GET", "expect": 204},
        {"name": "Antigravity 后端", "url": "https://cloudcode-pa.googleapis.com/", "method": "GET"},
        {"name": "Gemini API", "url": "https://generativelanguage.googleapis.com/v1beta/models", "method": "GET"},
        {"name": "Gemini 网页版", "url": "https://gemini.google.com/", "method": "GET"},
    ],
}

def ensure_baseline_target(cfg):
    """像 Clash Verge 的 delay 检测一样，保证有一个 generate_204 连通性基准目标。"""
    if not any(t.get("name") == "网络连通" for t in cfg.get("targets") or []):
        cfg.setdefault("targets", []).insert(0, {
            "name": "网络连通",
            "url": "http://www.gstatic.com/generate_204",
            "method": "GET",
            "expect": 204,
        })
    return cfg


SEVERITY = {"up": 0, "degraded": 1, "down": 2}
SEVERITY_TEXT = {"up": "可用", "degraded": "降级", "down": "不可用", "unknown": "检测中"}
SHORT_NAMES = {
    "网络连通": "网络",
    "Antigravity 后端": "Antigravity",
    "Gemini API": "Gemini API",
    "Gemini 网页版": "Gemini 网页",
}
SEVERITY_COLOR = {
    "up": (34, 197, 94),
    "degraded": (245, 158, 11),
    "down": (239, 68, 68),
    "unknown": (148, 163, 184),
}


def _sparkle_points(n):
    """Gemini 四角星轮廓（基于 24×24 的贝塞尔路径），缩放到 n×n。"""
    segs = [((12, 2), (12, 8), (16, 12), (22, 12)),
            ((22, 12), (16, 12), (12, 16), (12, 22)),
            ((12, 22), (12, 16), (8, 12), (2, 12)),
            ((2, 12), (8, 12), (12, 8), (12, 2))]
    k = n / 24.0
    pts = []
    for (x0, y0), (x1, y1), (x2, y2), (x3, y3) in segs:
        for i in range(40):
            t = i / 40.0
            u = 1 - t
            a, b, c, d = u ** 3, 3 * u * u * t, 3 * u * t * t, t ** 3
            pts.append(((a * x0 + b * x1 + c * x2 + d * x3) * k,
                        (a * y0 + b * y1 + c * y2 + d * y3) * k))
    return pts


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def abs_path(name):
    return name if os.path.isabs(name) else os.path.join(DATA_DIR, name)


def _open_path(path):
    """用系统默认程序打开文件/目录，跨平台。"""
    if IS_WINDOWS:
        os.startfile(path)
    elif IS_MAC:
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


def ensure_data_dir():
    """建好数据目录，并把旧版本散落在运行目录的文件搬过来。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    src = os.path.abspath(BASE_DIR)
    if src == os.path.abspath(DATA_DIR):
        return
    legacy = ("config.json", "gemini_watch.log", "gemini_watch.log.1",
              "history.jsonl", "history.jsonl.1")
    for name in legacy:
        old, new = os.path.join(src, name), os.path.join(DATA_DIR, name)
        if not os.path.exists(old) or os.path.exists(new):
            continue
        try:
            if name == "config.json" and not getattr(sys, "frozen", False):
                shutil.copy2(old, new)   # 开发模式保留仓库里的配置
            else:
                shutil.move(old, new)
        except Exception:
            pass


def log(level, message, log_file=None):
    line = f"[{now_str()}] {level:<5} {message}"
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass
    if not log_file:
        return
    try:
        path = abs_path(log_file)
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            backup = path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config():
    ensure_data_dir()
    cfg = dict(DEFAULT_CONFIG)
    if not os.path.exists(CONFIG_PATH):
        # 首次运行或配置被删除时，自动生成可编辑的默认配置
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            log("INFO", f"配置文件不存在，已生成默认配置：{CONFIG_PATH}",
                abs_path(DEFAULT_CONFIG.get("log_file") or "gemini_watch.log"))
        except Exception as e:
            log("ERROR", f"生成默认配置失败: {e}")
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log("WARN", f"读取 config.json 失败，使用默认配置: {e}")
    return cfg


def acquire_lock():
    """单实例锁：拿不到锁说明已有进程在跑。"""
    ensure_data_dir()
    try:
        f = open(LOCK_PATH, "a+")
        f.seek(0)
        f.write("0")
        f.flush()
        f.seek(0)
        if IS_WINDOWS:
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except Exception:
        return None


UNSUPPORTED_HINTS = (
    "isn't available in your country",
    "is not available in your country",
    "not available in your country",
    "isn't currently available in your country",
    "is not currently available in your country",
    "not yet available in your country",
    "在你所在的国家/地区不可用",
    "在你所在的国家或地区不可用",
    "尚未在你所在的国家/地区推出",
)


def probe(target, cfg):
    """返回 (severity, detail, latency_ms)。"""
    url = target["url"]
    timeout = float(cfg["timeout_seconds"])
    proxies = None
    if cfg.get("proxy"):
        proxies = {"http": cfg["proxy"], "https": cfg["proxy"]}
    headers = {"User-Agent": "gemini-watch/1.1"}
    headers.update(target.get("headers") or {})

    started = time.perf_counter()
    try:
        resp = requests.request(
            target.get("method", "GET"),
            url,
            timeout=timeout,
            proxies=proxies,
            headers=headers,
            allow_redirects=True,
            stream=True,
        )
        ms = int((time.perf_counter() - started) * 1000)
        code = resp.status_code
        # Gemini 网页版：像 Clash Verge 的解锁测试那样，直接看服务自己返回的内容，
        # 页面上出现“你所在国家/地区不可用”即判定不支持。
        if "gemini.google.com" in url and code < 400:
            chunks, size = [], 0
            for chunk in resp.iter_content(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size >= 300000:
                    break
            body = b"".join(chunks).decode("utf-8", "ignore").lower()
            resp.close()
            if any(h in body for h in UNSUPPORTED_HINTS):
                return "down", "不支持：页面提示该地区不可用", ms
            return "up", "支持（页面可正常打开）", ms
        resp.close()
        expect = target.get("expect")
        if expect is not None and code == int(expect):
            return "up", f"HTTP {code}（预期状态）", ms
        if code == 429:
            return "degraded", "HTTP 429 限流/额度", ms
        if code >= 500:
            return "down", f"HTTP {code} 服务端故障", ms
        if code >= 400:
            return "up", f"HTTP {code}（网络通，未鉴权）", ms
        return "up", f"HTTP {code}", ms
    except requests.exceptions.Timeout:
        return "down", f"超时 >{timeout:g}s（网络/代理）", None
    except requests.exceptions.SSLError:
        return "down", "TLS 握手失败（代理或证书异常）", None
    except requests.exceptions.ConnectionError:
        return "down", "连接失败（DNS/网络/代理）", None
    except Exception as e:
        return "down", f"{type(e).__name__}: {str(e)[:80]}", None


# ---------------------------------------------------------------- 故障诊断

def _process_dump():
    """导出当前进程名列表（小写），用于诊断代理进程是否在跑。"""
    try:
        if IS_WINDOWS:
            out = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh"],
                capture_output=True,
                timeout=6,
                creationflags=CREATE_NO_WINDOW,
            )
            # 中文 Windows 下 tasklist 输出为 GBK，不能直接用 utf-8 解码
            return _decode(out.stdout or b"").lower()
        out = subprocess.run(["ps", "-Ao", "comm="], capture_output=True, timeout=6)
        return (out.stdout or b"").decode("utf-8", errors="ignore").lower()
    except Exception:
        return ""


def _port_open(port, host="127.0.0.1"):
    try:
        with socket.socket() as s:
            s.settimeout(1.0)
            return s.connect_ex((host, int(port))) == 0
    except Exception:
        return False


def diagnose(cfg, host):
    """对目标域名做一次链路诊断，返回简短结论列表。"""
    dcfg = cfg.get("diagnose") or {}
    lines = []
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        lines.append(f"DNS 正常 → {ips[0]}" if ips else "DNS 无解析结果")
    except Exception as e:
        return [f"DNS 解析失败：{type(e).__name__}"]

    if not ips:
        return lines
    ip = ips[0]

    started = time.perf_counter()
    try:
        with socket.create_connection((ip, 443), timeout=3):
            lines.append(f"TCP 443 可连（{int((time.perf_counter() - started) * 1000)}ms）")
    except Exception as e:
        lines.append(f"TCP 443 失败：{type(e).__name__}")

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((ip, 443), timeout=4) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                lines.append(f"TLS 正常（{tls.version()}）")
    except Exception as e:
        lines.append(f"TLS 失败：{str(e)[:50]}")

    pport = dcfg.get("proxy_port") or 7890
    lines.append(f"代理端口 {pport} " + ("正常监听" if _port_open(pport) else "未监听"))

    names = dcfg.get("processes") or []
    if names:
        dump = _process_dump()
        alive = [n for n in names if n.lower() in dump]
        lines.append("代理进程：" + ("、".join(alive) if alive else "未运行"))
    return lines


# ---------------------------------------------------------------- 代理路径

TUN_KEYWORDS = ("clash", "mihomo", "meta", "tun", "tap", "wintun", "wireguard",
                "sing-box", "singbox", "v2ray", "xray")


def _decode(raw):
    encs = ("mbcs", "utf-8") if IS_WINDOWS else ("utf-8",)
    for enc in encs:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


def _system_proxy_enabled():
    if IS_MAC:
        try:
            out = subprocess.run(["scutil", "--proxy"], capture_output=True, timeout=5)
            text = (out.stdout or b"").decode("utf-8", errors="ignore")
            return bool(re.search(r"(HTTP|HTTPS|SOCKS)Enable\s*:\s*1", text))
        except Exception:
            return False
    if not IS_WINDOWS:
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            return bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
    except Exception:
        return False


def _tun_adapters():
    """列出已连接且带 IPv4 地址的 TUN/TAP 类虚拟网卡。"""
    if IS_MAC:
        try:
            out = subprocess.run(["ifconfig"], capture_output=True, timeout=5)
            text = (out.stdout or b"").decode("utf-8", errors="ignore")
        except Exception:
            return []
        names = []
        # macOS 自带的 utun0~3 只有 IPv6 链路地址，据此排除系统接口
        for block in re.split(r"\n(?=\S)", text):
            head = block.splitlines()[0] if block else ""
            m = re.match(r"^(utun\d+|tap\d+|tun\d+):", head)
            if not m or "UP" not in head:
                continue
            if re.search(r"\binet \d+\.\d+\.\d+\.\d+", block):
                names.append(m.group(1))
        return names
    if not IS_WINDOWS:
        return []
    try:
        out = subprocess.run(
            ["netsh", "interface", "show", "interface"],
            capture_output=True,
            timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
        names = []
        for line in _decode(out.stdout or b"").splitlines():
            low = line.lower()
            if "connected" not in low and "已连接" not in line:
                continue
            if any(kw in low for kw in TUN_KEYWORDS):
                parts = line.split()
                if parts:
                    names.append(parts[-1])
        return names
    except Exception:
        return []


def detect_path(cfg):
    """判断当前流量实际走的路径，返回 (path, detail)。"""
    pcfg = cfg.get("path_check") or {}
    if not pcfg.get("enabled", True):
        return "未知", ""
    proxy_on = _system_proxy_enabled()
    tuns = _tun_adapters()
    if proxy_on and tuns:
        return "系统代理 + TUN", "、".join(tuns)
    if proxy_on:
        return "系统代理", ""
    if tuns:
        return "TUN", "、".join(tuns)
    if _port_open(pcfg.get("proxy_port") or 7890):
        return "代理端口未接管", "端口在监听，但没有系统代理/TUN 引流"
    return "直连", "未检测到系统代理或 TUN"


# ---------------------------------------------------------------- 地区限制

REGION_CACHE = {"ts": 0.0, "data": None}


def _check_region_via_api(rcfg, timeout):
    """带 key 调一次最小请求，识别官方地区错误（最准）。"""
    key = (rcfg.get("api_key") or "").strip()
    if not key:
        return None
    model = rcfg.get("model") or "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": "hi"}]}],
        "generationConfig": {"maxOutputTokens": 1},
    }
    try:
        resp = requests.post(url, params={"key": key}, json=body, timeout=timeout)
    except Exception as e:
        return {"ok": None, "detail": f"地区探测请求失败：{type(e).__name__}"}

    text = (resp.text or "").lower()
    if resp.status_code == 200:
        return {"ok": True, "detail": "接口可用（含地区校验）"}
    if "location is not supported" in text or "failed_precondition" in text:
        return {"ok": False, "detail": "官方返回：出口 IP 地区不受支持"}
    if resp.status_code in (401, 403) and "api key" in text:
        return {"ok": None, "detail": "API key 无效，已回退 IP 判定"}
    if resp.status_code == 429:
        return {"ok": None, "detail": "配额限流，暂时无法判定地区"}
    return {"ok": None, "detail": f"HTTP {resp.status_code}，未识别为地区错误"}


def check_region(cfg, force=False):
    """只在配置了 api_key 时用官方接口判定地区；不做 IP 归属推断（不准）。"""
    rcfg = dict(DEFAULT_CONFIG["region_check"])
    rcfg.update(cfg.get("region_check") or {})
    if not rcfg.get("enabled", True) or not (rcfg.get("api_key") or "").strip():
        return None
    now = time.time()
    cached = REGION_CACHE.get("data")
    ttl = int(rcfg.get("interval_minutes", 10)) * 60
    if cached and not force and now - REGION_CACHE["ts"] < ttl:
        return cached

    verdict = _check_region_via_api(rcfg, float(cfg["timeout_seconds"]))
    result = {"ok": None, "via": "api", "detail": ""}
    if verdict:
        result["ok"] = verdict["ok"]
        result["detail"] = verdict["detail"]
    else:
        result["detail"] = "地区校验请求失败"

    REGION_CACHE.update(ts=now, data=result)
    return result


# ---------------------------------------------------------------- 通知

def _toast(title, message, sound=True):
    """跨平台桌面通知：Windows Toast / macOS 通知中心 / Linux notify-send。"""
    if IS_WINDOWS:
        from winotify import Notification, audio

        toast = Notification(app_id="Gemini 可用性监控", title=title, msg=message, duration="short")
        if sound:
            toast.set_audio(audio.Default, loop=False)
        toast.show()
        return
    if IS_MAC:
        def esc(text):
            return text.replace("\\", "\\\\").replace('"', '\\"')

        script = f'display notification "{esc(message)}" with title "{esc(title)}"'
        if sound:
            script += ' sound name "default"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=8, check=False)
        return
    subprocess.run(["notify-send", title, message], capture_output=True, timeout=8, check=False)


def notify(cfg, title, message, log_file=None):
    if not cfg["notify"].get("enabled", True):
        log("INFO", f"通知已禁用，跳过：{title}", log_file)
        return False
    try:
        _toast(title, message, bool(cfg["notify"].get("sound", True)))
        log("INFO", f"已发送桌面通知：{title}", log_file)
        return True
    except Exception as e:
        log("WARN", f"桌面通知发送失败: {e}", log_file)
        return False


# ---------------------------------------------------------------- 监控主体

class Monitor:
    def __init__(self, cfg):
        self.cfg = cfg
        ensure_baseline_target(cfg)
        self.log_file = cfg.get("log_file")
        self.history_file = abs_path(cfg.get("history_file") or "history.jsonl")
        self.states = {
            t["name"]: {"sev": "unknown", "fails": 0, "oks": 0} for t in cfg["targets"]
        }
        self.current = {}
        self.overall = "unknown"
        self.last_cycle = 0
        self.cycle_seq = 0
        self.last_alert = 0.0
        self.paused = False
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.tray = None
        self._cache = None
        self.path = "未知"
        self.path_detail = ""
        self.region = None
        self._region_thread = None
        self.panel_server = None
        self.panel_host = "127.0.0.1"
        self.panel_port = None
        self.running = False
        self._config_mtime = _file_mtime(CONFIG_PATH)

    # ---- 探测
    def _probe_all(self, targets):
        """探测加硬截止：DNS 卡死时也能按时给出不可用，不拖垮整轮。"""
        hard = float(self.cfg["timeout_seconds"]) + 2
        pool = ThreadPoolExecutor(max_workers=max(1, min(4, len(targets))))
        futures = {pool.submit(probe, t, self.cfg): t["name"] for t in targets}
        done, _pending = futures_wait(futures, timeout=hard)
        results = {}
        for fut, name in futures.items():
            if fut in done:
                try:
                    results[name] = fut.result()
                except Exception:
                    results[name] = ("down", "探测异常", None)
            else:
                results[name] = ("down", f"超时 >{hard:g}s（DNS/连接无响应）", None)
        pool.shutdown(wait=False, cancel_futures=True)
        return results

    def _refresh_region_async(self):
        """地区查询在后台跑，避免出口查询拖慢每轮探测。"""
        rcfg = dict(DEFAULT_CONFIG["region_check"])
        rcfg.update(self.cfg.get("region_check") or {})
        if not rcfg.get("enabled", True) or not (rcfg.get("api_key") or "").strip():
            return
        ttl = int(rcfg.get("interval_minutes", 10)) * 60
        if self.region and time.time() - REGION_CACHE["ts"] < ttl:
            return
        if self._region_thread and self._region_thread.is_alive():
            return
        self._region_thread = threading.Thread(target=self._region_worker, daemon=True)
        self._region_thread.start()

    def _region_worker(self):
        try:
            region = check_region(self.cfg, force=True)
            if region:
                with self.lock:
                    self.region = region
                    self._apply_region()
                    self._cache = None
                self._update_tray()
        except Exception:
            pass

    def run_cycle(self):
        ts = int(time.time())
        self.cycle_seq += 1
        path, path_detail = detect_path(self.cfg)
        self._refresh_region_async()
        results = self._probe_all(self.cfg["targets"])

        with self.lock:
            self.path, self.path_detail = path, path_detail
            for name, (sev, _detail, _ms) in results.items():
                st = self.states.setdefault(name, {"sev": "unknown", "fails": 0, "oks": 0})
                st["sev"] = sev
                if sev == "up":
                    st["fails"] = 0
                    st["oks"] += 1
                else:
                    st["fails"] += 1
                    st["oks"] = 0

            self.current = {
                name: {"sev": sev, "detail": detail, "ms": ms, "ts": ts}
                for name, (sev, detail, ms) in results.items()
            }
            self.overall = self._settle_overall(results)
            self._apply_region()
            self._apply_require_proxy()
            self._append_history(ts, results)
            self._log_cycle(results)
            self._maybe_alert(results)
            self._update_tray()
            self._cache = None
            self.last_cycle = time.time()

    def _apply_region(self):
        """仅当带 key 的官方接口明确返回地区不支持时才判不可用；
        基于 IP 的地区推断只作参考，不影响总状态。"""
        r = self.region or {}
        if r.get("ok") is False and r.get("via") == "api" and self.overall != "down":
            self.overall = "down"

    def _apply_require_proxy(self):
        """勾选“必须走代理”时，直连成功也判为降级。"""
        pcfg = self.cfg.get("path_check") or {}
        if pcfg.get("require_proxy") and self.overall == "up" and self.path in ("直连", "代理端口未接管"):
            self.overall = "degraded"

    def _settle_overall(self, results):
        worst = max(SEVERITY[r[0]] for r in results.values()) if results else 0
        if worst == SEVERITY["up"]:
            return "up"
        return "down" if worst == SEVERITY["down"] else "degraded"

    def severity_text(self, sev):
        return SEVERITY_TEXT.get(sev, sev)

    # ---- 历史与日志
    def _append_history(self, ts, results):
        try:
            if os.path.exists(self.history_file) and \
                    os.path.getsize(self.history_file) > MAX_HISTORY_BYTES:
                backup = self.history_file + ".1"
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(self.history_file, backup)
            with open(self.history_file, "a", encoding="utf-8") as f:
                for name, (sev, detail, ms) in results.items():
                    f.write(json.dumps(
                        {"ts": ts, "t": name, "s": sev, "d": detail, "ms": ms},
                        ensure_ascii=False,
                    ) + "\n")
        except Exception as e:
            log("WARN", f"写入历史失败: {e}", self.log_file)

    def clear_history(self):
        """清空检测历史（记录/图表/事件），当前状态保留。"""
        try:
            with self.lock:
                for path in (self.history_file, self.history_file + ".1"):
                    if os.path.exists(path):
                        os.remove(path)
                self._cache = None
            log("INFO", "检测记录已清空", self.log_file)
            return True
        except Exception as e:
            log("ERROR", f"清空检测记录失败: {e}", self.log_file)
            return False

    def _log_cycle(self, results):
        parts = []
        for name, (sev, detail, ms) in results.items():
            latency = f" {ms}ms" if ms is not None else ""
            parts.append(f"{name}: {self.severity_text(sev)}({detail}{latency})")
        log("INFO", f"路径={self.path} | 地区={self._region_text()} | "
                    f"总状态={self.severity_text(self.overall)} | " + "；".join(parts),
            self.log_file)

    def _region_text(self):
        r = self.region or {}
        if not r:
            return "未校验（未配置 api_key）"
        return {True: "官方校验正常", False: "官方校验受限"}.get(r.get("ok"), "未判定")

    # ---- 告警
    def _maybe_alert(self, results):
        fail_threshold = int(self.cfg["fail_threshold"])
        repeat_seconds = int(self.cfg["repeat_alert_minutes"]) * 60
        now = time.time()

        if self.overall == "up":
            # 恢复通知仍要满足连续成功次数，避免抖动反复弹窗；面板状态则实时反映
            min_oks = min(st["oks"] for st in self.states.values()) if self.states else 0
            if self.last_alert and min_oks >= int(self.cfg["recover_threshold"]):
                notify(self.cfg, "✅ Gemini 已恢复", "全部检测项恢复正常", self.log_file)
                self.last_alert = 0.0
            return

        problem = [name for name, (sev, _, _) in results.items() if sev != "up"]
        if not problem:
            return
        if any(self.states[name]["fails"] < fail_threshold for name in problem):
            return
        if self.last_alert:
            if repeat_seconds <= 0:
                return  # repeat_alert_minutes=0：本次故障只提醒一次
            if now - self.last_alert < repeat_seconds:
                return

        names = "、".join(SHORT_NAMES.get(n, n) for n in problem[:3])
        if len(problem) > 3:
            names += " 等"
        message = f"{names} 不可用"
        if self.path in ("直连", "代理端口未接管"):
            message += "（未走代理）"

        diag = self._diagnose_if_needed(results)
        if diag:
            log("INFO", "故障诊断：" + " | ".join(diag), self.log_file)

        notify(self.cfg, f"⚠️ Gemini {self.severity_text(self.overall)}", message, self.log_file)
        self.last_alert = now

    def _diagnose_if_needed(self, results):
        if not (self.cfg.get("diagnose") or {}).get("enabled", True):
            return []
        network_issue = any(
            any(k in detail for k in ("超时", "连接失败", "TLS", "DNS"))
            for sev, detail, _ in results.values() if sev == "down"
        )
        if not network_issue:
            return []
        for target in self.cfg["targets"]:
            result = results.get(target["name"])
            if result and result[0] == "down":
                try:
                    lines = diagnose(self.cfg, urlparse(target["url"]).hostname)
                except Exception as e:
                    lines = [f"诊断失败：{e}"]
                log("INFO", "链路诊断：" + " | ".join(lines), self.log_file)
                return lines
        return []

    # ---- 托盘联动
    def _update_tray(self):
        if self.tray is None:
            return
        try:
            self.tray.update(self.overall, self.severity_text(self.overall))
        except Exception:
            pass

    # ---- 循环
    def run_loop(self):
        self._config_mtime = _file_mtime(CONFIG_PATH)
        while not self.stop.is_set():
            if self.paused:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            try:
                self.running = True
                self.run_cycle()
            except Exception as e:
                log("ERROR", f"检测异常: {e}", self.log_file)
            finally:
                self.running = False
            self._watch_config()
            self.wake.wait(float(self.cfg["interval_seconds"]))
            self.wake.clear()

    # ---- 配置热加载
    def _watch_config(self):
        mtime = _file_mtime(CONFIG_PATH)
        if mtime is None:
            if self._config_mtime is not None:
                log("WARN", "配置文件被删除，正在重新生成默认配置…", self.log_file)
                self._config_mtime = None
                self.reload_config()
            return
        if mtime != self._config_mtime:
            self.reload_config()

    def reload_config(self):
        """重新读取 config.json；端口等面板配置变化会自动重启面板。"""
        try:
            cfg = load_config()
        except Exception as e:
            log("ERROR", f"重新加载配置失败: {e}", self.log_file)
            return False
        ensure_baseline_target(cfg)
        old_panel = dict(self.cfg.get("panel") or {})
        with self.lock:
            self.cfg = cfg
            self.log_file = cfg.get("log_file")
            names = {t["name"] for t in (cfg.get("targets") or [])}
            self.states = {n: s for n, s in self.states.items() if n in names}
            for n in names:
                self.states.setdefault(n, {"sev": "unknown", "fails": 0, "oks": 0})
            self._config_mtime = _file_mtime(CONFIG_PATH)
            self._cache = None
        new_panel = dict(cfg.get("panel") or {})
        if (old_panel.get("enabled", True), old_panel.get("host"), old_panel.get("port")) != \
                (new_panel.get("enabled", True), new_panel.get("host"), new_panel.get("port")):
            self._restart_panel()
        log("INFO", f"配置已重新加载：间隔 {cfg['interval_seconds']}s，超时 {cfg['timeout_seconds']}s，"
                    f"目标 {len(cfg['targets'])} 个，面板 {new_panel.get('host', '127.0.0.1')}:"
                    f"{new_panel.get('port')}", self.log_file)
        self._update_tray()
        return True

    def _restart_panel(self):
        srv = getattr(self, "panel_server", None)
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        self.panel_server = start_panel(self)

    def trigger(self):
        self.paused = False
        self.wake.set()

    # ---- 面板数据
    def build_summary(self, hours=24):
        now = time.time()
        if self._cache and self._cache[0] == hours and now - self._cache[1] < 1.5:
            return self._cache[2]

        cutoff = now - hours * 3600
        records = self._load_history(cutoff)
        targets = []

        for target in self.cfg["targets"]:
            name = target["name"]
            rows = [r for r in records if r.get("t") == name]
            cur = self.current.get(name, {"sev": "unknown", "detail": "等待首次检测", "ms": None})
            latencies = [r["ms"] for r in rows if r.get("ms")]
            score = sum({"up": 1.0, "degraded": 0.5}.get(r.get("s"), 0.0) for r in rows)
            avail = round(score / len(rows) * 100, 2) if rows else None
            targets.append({
                "name": name,
                "sev": cur["sev"],
                "detail": cur["detail"],
                "ms": cur["ms"],
                "avail": avail,
                "avg_ms": int(sum(latencies) / len(latencies)) if latencies else None,
                "p95_ms": _percentile(latencies, 95),
                "samples": len(rows),
                "series": _bucket_series(rows, cutoff, hours * 3600, 180),
            })

        summary = {
            "now": int(now),
            "overall": self.overall,
            "cycle_ts": int(self.last_cycle * 1000),
            "seq": self.cycle_seq,
            "running": self.running,
            "paused": self.paused,
            "interval": self.cfg["interval_seconds"],
            "hours": hours,
            "path": self.path,
            "path_detail": self.path_detail,
            "region": self.region,
            "targets": targets,
            "records": _recent_cycles(records, 12),
            "events": _events(records)[-40:][::-1],
        }
        self._cache = (hours, now, summary)
        return summary

    def _load_history(self, cutoff):
        records = []
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("ts", 0) >= cutoff:
                        records.append(rec)
        except FileNotFoundError:
            pass
        except Exception as e:
            log("WARN", f"读取历史失败: {e}", self.log_file)
        return records


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def _bucket_series(rows, cutoff, span, buckets):
    slots = [[] for _ in range(buckets)]
    step = span / buckets
    for row in rows:
        ms = row.get("ms")
        if not ms:
            continue
        idx = int((row["ts"] - cutoff) / step)
        if 0 <= idx < buckets:
            slots[idx].append(ms)
    series = []
    for idx, values in enumerate(slots):
        ts = int(cutoff + (idx + 0.5) * step)
        series.append([ts, int(sum(values) / len(values)) if values else None])
    return series


def _recent_cycles(records, limit=12):
    """按轮次聚合历史，返回最近 limit 轮，最新在前。"""
    cycles = {}
    for rec in records:
        cycles.setdefault(rec.get("ts", 0), {})[rec.get("t", "")] = {
            "s": rec.get("s"), "ms": rec.get("ms"),
        }
    out = []
    for ts in sorted(cycles)[-limit:][::-1]:
        tgts = cycles[ts]
        worst = max((SEVERITY.get(v["s"], 0) for v in tgts.values()), default=0)
        overall = "down" if worst == SEVERITY["down"] else ("degraded" if worst else "up")
        out.append({"ts": ts, "overall": overall, "targets": tgts})
    return out


def _events(records):
    last = {}
    events = []
    for row in records:
        name, sev = row.get("t"), row.get("s")
        prev = last.get(name)
        if prev is not None and sev != prev and (sev != "up" or prev != "up"):
            events.append({
                "ts": row["ts"],
                "name": name,
                "sev": sev,
                "detail": row.get("d", ""),
            })
        last[name] = sev
    return events


def _notify_always(title, message):
    """不受 notify.enabled 影响的系统提示（端口切换等运维事件）。"""
    try:
        _toast(title, message, True)
    except Exception:
        pass


def _find_free_port(host, start, tries=30):
    for p in range(start + 1, start + tries + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return None


# ---------------------------------------------------------------- 状态面板

def start_panel(monitor):
    panel_cfg = monitor.cfg.get("panel") or {}
    if not panel_cfg.get("enabled", True):
        return None
    host = panel_cfg.get("host", "127.0.0.1")
    port = int(panel_cfg.get("port", 47131))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                try:
                    with open(PANEL_PATH, "r", encoding="utf-8") as f:
                        body = f.read().encode("utf-8")
                    self._reply(200, "text/html; charset=utf-8", body)
                except Exception:
                    self._reply(500, "text/plain; charset=utf-8", b"panel.html missing")
                return
            if parsed.path == "/api/summary":
                try:
                    hours = float(parse_qs(parsed.query).get("hours", ["24"])[0])
                    body = json.dumps(monitor.build_summary(hours), ensure_ascii=False).encode("utf-8")
                    self._reply(200, "application/json; charset=utf-8", body)
                except Exception as e:
                    self._reply(500, "application/json", json.dumps({"error": str(e)}).encode("utf-8"))
                return
            self._reply(404, "text/plain; charset=utf-8", b"not found")

        def do_POST(self):
            if urlparse(self.path).path == "/api/refresh":
                seq = monitor.cycle_seq
                monitor.wake.set()
                monitor._cache = None
                body = json.dumps({"ok": True, "seq": seq, "cycle_ts": int(monitor.last_cycle * 1000)}).encode("utf-8")
                self._reply(200, "application/json; charset=utf-8", body)
                return
            if urlparse(self.path).path == "/api/clear":
                ok = monitor.clear_history()
                body = json.dumps({"ok": ok}).encode("utf-8")
                self._reply(200, "application/json; charset=utf-8", body)
                return
            self._reply(404, "text/plain; charset=utf-8", b"not found")

        def _reply(self, code, content_type, body):
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass

        def log_message(self, *args):
            pass

    orig = port
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        alt = _find_free_port(host, port)
        if alt is None:
            log("ERROR", f"面板端口 {orig} 被占用，且 {orig + 1}-{orig + 30} 均不可用: {e}", monitor.log_file)
            _notify_always("Gemini 监控：面板未启动", f"端口 {orig} 被占用，且未找到可用端口，请修改 config.json 的 panel.port")
            return None
        log("WARN", f"面板端口 {orig} 被占用（{e}），已自动切换到 {alt}", monitor.log_file)
        server = ThreadingHTTPServer((host, alt), Handler)
        port = alt
        _notify_always("Gemini 监控：端口已切换",
                       f"端口 {orig} 被占用，面板已改用 http://{host}:{alt}")
    monitor.panel_host, monitor.panel_port = host, port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("INFO", f"状态面板已启动: http://{host}:{port}", monitor.log_file)
    return server


# ---------------------------------------------------------------- 托盘

class Tray:
    def __init__(self, monitor):
        from pystray import Icon, Menu, MenuItem

        self.monitor = monitor
        menu = Menu(
            MenuItem(lambda item: f"状态：{monitor.severity_text(monitor.overall)}", None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("打开状态面板", self._open_panel, default=True),
            MenuItem("立即检测", lambda item: monitor.trigger()),
            MenuItem(lambda item: "恢复监控" if monitor.paused else "暂停监控", self._toggle_pause),
            MenuItem("查看日志", self._open_log),
            MenuItem("打开数据目录", self._open_data_dir),
            MenuItem("编辑配置", self._open_config),
            MenuItem("重新加载配置", lambda item: self.monitor.reload_config()),
            Menu.SEPARATOR,
            MenuItem("退出", self._quit),
        )
        self.icon = Icon("gemini-watch", self._image(monitor.overall), "Gemini 监控", menu=menu)

    @staticmethod
    def _image(sev):
        """Gemini 四角星图标，按状态着色。"""
        from PIL import Image, ImageDraw

        color = SEVERITY_COLOR.get(sev, SEVERITY_COLOR["unknown"])
        n = 256
        pts = _sparkle_points(n)

        img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        ImageDraw.Draw(img).polygon(pts, fill=color)

        shine = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        ImageDraw.Draw(shine).polygon(
            [(n / 2 + (x - n / 2) * 0.45, n / 2 + (y - n / 2) * 0.45) for x, y in pts],
            fill=(255, 255, 255, 70))
        size = 44 if IS_MAC else 64
        return Image.alpha_composite(img, shine).resize((size, size), Image.LANCZOS)

    def update(self, sev, text):
        self.icon.icon = self._image(sev)
        self.icon.title = f"Gemini 监控：{text}"

    def _open_panel(self, _item=None):
        # 单击/双击都会触发同一个默认项，做去抖避免双击开两个标签页
        now = time.time()
        if now - getattr(self, "_last_open", 0) < 0.8:
            return
        self._last_open = now
        host = self.monitor.panel_host or "127.0.0.1"
        port = self.monitor.panel_port or (self.monitor.cfg.get("panel") or {}).get("port", 47131)
        webbrowser.open(f"http://{host}:{port}")

    def _toggle_pause(self, _item=None):
        self.monitor.paused = not self.monitor.paused
        self.monitor._cache = None
        self.icon.title = "Gemini 监控：已暂停" if self.monitor.paused else "Gemini 监控：运行中"

    def _open_log(self, _item=None):
        try:
            _open_path(abs_path(self.monitor.cfg.get("log_file") or "gemini_watch.log"))
        except Exception:
            pass

    def _open_data_dir(self, _item=None):
        try:
            _open_path(DATA_DIR)
        except Exception:
            pass

    def _open_config(self, _item=None):
        try:
            _open_path(CONFIG_PATH)
        except Exception:
            pass

    def _quit(self, _item=None):
        self.monitor.stop.set()
        self.icon.stop()

    def run(self):
        self.icon.run()


# ---------------------------------------------------------------- 入口

def run_once(cfg, log_file=None):
    results = Monitor(cfg)._probe_all(cfg["targets"])
    for name, (sev, detail, ms) in results.items():
        latency = f" {ms}ms" if ms is not None else ""
        log("INFO", f"{name}: {SEVERITY_TEXT.get(sev, sev)}（{detail}{latency}）", log_file)
    return results


def main():
    cfg = load_config()
    log_file = cfg.get("log_file")

    if "--once" in sys.argv:
        run_once(cfg, log_file)
        return

    lock = acquire_lock()
    if lock is None:
        log("WARN", "已有监控进程在运行，本次退出。", log_file)
        notify(cfg, "Gemini 监控已在运行", "检测到重复启动，已忽略本次。", log_file)
        return

    monitor = Monitor(cfg)
    monitor.panel_server = start_panel(monitor)
    tray_cfg = cfg.get("tray") or {}
    use_tray = tray_cfg.get("enabled", True) and "--no-tray" not in sys.argv

    log("INFO", f"监控启动：间隔 {cfg['interval_seconds']}s，超时 {cfg['timeout_seconds']}s，"
                f"目标 {len(cfg['targets'])} 个，托盘={'开' if use_tray else '关'}", log_file)

    worker = threading.Thread(target=monitor.run_loop, daemon=True)
    worker.start()

    if use_tray:
        try:
            tray = Tray(monitor)
            monitor.tray = tray
            tray.run()  # 阻塞在主线程
        except Exception as e:
            log("WARN", f"托盘启动失败，改为无托盘模式: {e}", log_file)
            worker.join()
    else:
        try:
            worker.join()
        except KeyboardInterrupt:
            monitor.stop.set()

    log("INFO", "监控已停止", log_file)


if __name__ == "__main__":
    main()
