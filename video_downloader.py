#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VideoDownloader - Windows 视频下载器 GUI
基于 yt-dlp，支持抖音 / YouTube 等站点，支持 SOCKS 代理与浏览器 cookies。

踩坑与修复（经验固化，开箱即用）：
  - 网络层：YouTube 直连不通、且本机环境代理（如 WorkBuddy 127.0.0.1:56459）对 YouTube
    返回 502。→ 代理留空时自动探测本机可用 SOCKS（VPN/Clash/V2Ray），已验证可访问 YouTube。
  - 认证层：YouTube 频繁要求 "Sign in to confirm you're not a bot"。→ 支持「浏览器已登录
    cookies」与「选择 cookies.txt」两种方式，并在日志中给出中文引导。
  - 签名层：YouTube 的 n 参数挑战需 Node.js 解开。WorkBuddy 注入的 node-language-shim 会开启
    Node 权限模型，使 yt-dlp 调 node 解挑战时被 "Access to this API has been restricted" 拦截。
    → 仅剥离该 shim 的 --require 注入并追加 --allow-fs-read=*，保留用户其它 NODE_OPTIONS。
  - 抖音链接：各类分享页（jingxuan?modal_id= / user?modal_id= / aweme_id= 等）统一归一化为
    video/<ID>，原仅被 start 调用，现独立为纯函数 normalize_url()。
  - ffmpeg：抖音为分离音视频流，必须经 ffmpeg 合并；本地无则提供 PATH 回退与版本自检。

代码架构（重构后）：
  - DownloadOptions：把散落的全局变量/设置收敛为显式配置对象。
  - build_command()：纯函数，由参数接收二进制路径与选项，不再依赖全局变量。
  - DownloadEngine：把子进程管理、进度解析、日志线程从 UI 类里剥离，UI 只负责展示。
  - build_node_env()：仅在检测到 shim 时中和 Node 权限模型，避免误伤用户配置。

用法：
  1. 将本程序与 yt-dlp.exe、ffmpeg.exe 放在同一文件夹（cookies.txt 可选）。
  2. 双击 VideoDownloader.exe（或运行本脚本）即可。
  3. 命令行自检：VideoDownloader.exe --selftest  （无 GUI，打印环境诊断）。
"""
import os
import sys
import json
import locale
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import time
from dataclasses import dataclass
from typing import List, Optional

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox

# ---------- 资源定位 ----------
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

YTDLP = os.path.join(APP_DIR, "yt-dlp.exe")
FFMPEG = os.path.join(APP_DIR, "ffmpeg.exe")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
DEFAULT_OUTPUT = os.path.join(os.path.expanduser("~"), "Downloads", "VideoDownloader_Output")


# ---------- Node.js 定位（YouTube n 参数挑战求解依赖） ----------
def find_node() -> Optional[str]:
    """定位 Node.js 可执行文件，用于让 yt-dlp 解开 YouTube 的 n 签名挑战。

    查找顺序：
      1) 程序同目录的 node.exe（若将来打包进去）
      2) 系统常见安装位置（C:\\Program Files\\nodejs）
      3) WorkBuddy 内置 node（环境变量 CODEBUDDY_NODE_BIN / WORKBUDDY_NODE_BIN）
      4) PATH 中可执行的 node
    找不到返回 None（yt-dlp 会回退到自身探测）。
    """
    candidates: List[str] = []
    candidates.append(os.path.join(APP_DIR, "node.exe"))
    candidates.append(r"C:\Program Files\nodejs\node.exe")
    candidates.append(r"C:\Program Files (x86)\nodejs\node.exe")
    wb = os.environ.get("CODEBUDDY_NODE_BIN") or os.environ.get("WORKBUDDY_NODE_BIN")
    if wb:
        candidates.append(wb)
    for name in ("node.exe", "node"):
        p = shutil.which(name)
        if p:
            candidates.append(p)
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


# 启动即探测一次，全局复用
NODE_PATH = find_node()
# WorkBuddy 注入的 node-language-shim 会开启 Node 权限模型，导致 yt-dlp 调 node 解
# n 挑战时被 "Access to this API has been restricted" 拦住；此标记用于中和该限制。
NODE_SHIM_PRESENT = "node-language" in os.environ.get("NODE_OPTIONS", "")


def sanitize_node_options(opts: str) -> str:
    """中和 WorkBuddy 的 node-language-shim 对 Node 权限模型的干扰。

    做法：拆词后丢弃其 --require=<shim> 注入与 --permission，再确保带上
    --allow-fs-read=*（放开文件读取，避免 yt-dlp 调 node 解 n 挑战被拦），最后保留
    用户可能设置的其它 NODE_OPTIONS（如 --max-old-space-size）。若 shim 不在选项中则原样返回。
    """
    if "node-language" not in opts:
        return opts
    tokens = opts.split()
    kept = [
        t for t in tokens
        if ("node-language-shim" not in t
            and not t.startswith("--require=")
            and t != "--permission")
    ]
    if "--allow-fs-read=*" not in kept:
        kept.append("--allow-fs-read=*")
    return " ".join(kept)


def build_node_env(base_env: dict) -> dict:
    """返回一份子进程环境；仅当存在 node-language-shim 时中和 NODE_OPTIONS。"""
    env = dict(base_env)
    cur = env.get("NODE_OPTIONS", "")
    if "node-language" in cur:
        env["NODE_OPTIONS"] = sanitize_node_options(cur)
    return env


# ---------- ffmpeg 定位（抖音分离音视频流需合并） ----------
def find_ffmpeg() -> Optional[str]:
    """优先用同目录 ffmpeg.exe，否则回退到 PATH 中的 ffmpeg。"""
    if os.path.isfile(FFMPEG):
        return FFMPEG
    return shutil.which("ffmpeg")


def ffmpeg_version(path: str) -> Optional[str]:
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True,
                             timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        m = re.search(r"version\s+([\d.]+)", out.stdout or "")
        return m.group(1) if m else "未知"
    except Exception:
        return None


# ---------- cookies 自动定位 ----------
# 用户习惯把 cookies.txt 直接丢在程序根目录，此前 GUI 只在手动「选择 cookies.txt」后才传
# --cookies，导致根目录的 cookies.txt 形同虚设、YouTube 仍被 bot 检测拦下。
# 这里自动识别同目录下的 cookies.txt 并默认启用。
COOKIES_FILE_DEFAULT = os.path.join(APP_DIR, "cookies.txt")


def find_cookies() -> Optional[str]:
    """自动定位程序目录下的 cookies.txt（Netscape 格式），没有则 None。"""
    for p in (COOKIES_FILE_DEFAULT, os.path.join(APP_DIR, "cookies", "cookies.txt")):
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        except OSError:
            continue
    return None


def cookies_summary(path: Optional[str]) -> str:
    """简要统计 cookies.txt 里的域名与条数，便于诊断是否覆盖目标站点。"""
    if not path or not os.path.isfile(path):
        return "未找到"
    try:
        domains, n = {}, 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                parts = ln.split("\t")
                if len(parts) >= 6:
                    n += 1
                    domains[parts[0].lstrip(".")] = domains.get(parts[0].lstrip("."), 0) + 1
        top = "，".join(f"{d}({c})" for d, c in sorted(domains.items(), key=lambda x: -x[1])[:5])
        return f"{n} 条 · {top}"
    except Exception:
        return "读取失败"


# YouTube 登录态关键 cookie。只有 __Secure-3P* 这类「三方」cookie 而缺 SID /
# __Secure-1PSID 等「第一方」会话 cookie 时，YouTube 仍判定为未登录 → 照样人机验证。
STRONG_LOGIN_COOKIES = ("SID", "HSID", "SSID", "APISID", "SAPISID",
                        "__Secure-1PSID", "__Secure-1PAPISID")
WEAK_LOGIN_COOKIES = ("LOGIN_INFO", "__Secure-3PSID", "__Secure-3PAPISID")


def cookies_login_report(path: Optional[str]):
    """检查 cookies 是否含 YouTube 登录态，返回 (等级, 说明)，等级：none/weak/ok。"""
    if not path or not os.path.isfile(path):
        return "none", "未找到 cookies 文件"
    names = set()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                if ln.startswith("#") or not ln.strip():
                    continue
                parts = ln.split("\t")
                if len(parts) >= 6:
                    names.add(parts[5])
    except Exception as e:
        return "none", f"读取失败：{e}"
    strong = [c for c in STRONG_LOGIN_COOKIES if c in names]
    weak = [c for c in WEAK_LOGIN_COOKIES if c in names]
    if strong:
        return "ok", "含登录态：" + "、".join(strong)
    if weak:
        return "weak", ("仅含三方 cookie（" + "、".join(weak) + "），缺少 SID/__Secure-1PSID 等第一方会话 "
                        "cookie，YouTube 仍判为未登录并触发人机验证")
    return "none", "未发现任何 YouTube 登录 cookie"


def prepare_cookies_temp(path: Optional[str]) -> Optional[str]:
    """yt-dlp 会**回写** --cookies 指定的文件；一旦触发人机验证，YouTube 下发失效 cookie，
    yt-dlp 就会把仍有价值的条目剪掉，导致原始 cookies.txt 被逐步破坏。
    这里每次运行都先复制到临时文件再传给 yt-dlp，保护用户原始文件。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        tmpdir = tempfile.mkdtemp(prefix="vdcookie_")
        dst = os.path.join(tmpdir, "cookies.txt")
        shutil.copy2(path, dst)
        return dst
    except Exception:
        return path


# ---------- 本地 SOCKS 代理自动探测 ----------
# YouTube 在此环境下直连不通、且环境代理不放行，需走用户本机的 SOCKS 代理（VPN/Clash/V2Ray 等）。
# 这里逐个探测常见端口，用 SOCKS5 握手验证「能否经该代理连到 www.youtube.com:443」。
COMMON_SOCKS_PORTS = [10808, 7890, 7891, 1080, 10809, 1087, 33210, 10811]


def socks5_can_reach(proxy_host: str = "127.0.0.1", proxy_port: int = 10808,
                     target_host: str = "www.youtube.com", target_port: int = 443,
                     timeout: float = 1.5) -> bool:
    """经 SOCKS5 代理做一次 TCP 连接探测，验证该代理能否访问目标主机。"""
    try:
        s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
        s.settimeout(timeout)
        # 握手：VERSION=5, 1 个方法, 无认证(0x00)
        s.sendall(b"\x05\x01\x00")
        if s.recv(2) != b"\x05\x00":
            s.close()
            return False
        # 连接请求：VER, CMD=connect(1), RSV, ATYP=domain(3), 域名长度, 域名, 端口(2)
        msg = b"\x05\x01\x00\x03" + bytes([len(target_host)]) + target_host.encode() + target_port.to_bytes(2, "big")
        s.sendall(msg)
        rep = s.recv(10)
        s.close()
        # rep[1] == 0x00 表示代理连接成功
        return len(rep) >= 2 and rep[1] == 0
    except Exception:
        return False


def detect_socks_proxy() -> Optional[str]:
    """并发探测本机常见 SOCKS 端口，返回优先级最高的可用代理地址，没有则 None。

    串行扫描 8 个端口 × 1.5s 超时最坏要 12 秒，曾导致 GUI 主线程卡死、被系统标记「未响应」；
    改为并发后最坏 ~2 秒。
    """
    hits = {}
    lock = threading.Lock()

    def probe(p):
        ok = socks5_can_reach(proxy_port=p, timeout=1.5)
        if ok:
            with lock:
                hits[p] = True

    threads = [threading.Thread(target=probe, args=(p,), daemon=True) for p in COMMON_SOCKS_PORTS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3.0)
    # 按 COMMON_SOCKS_PORTS 的优先级顺序返回，保证结果稳定
    for p in COMMON_SOCKS_PORTS:
        if hits.get(p):
            return f"socks5://127.0.0.1:{p}"
    return None


# 探测结果缓存：一次启动内复用，避免每次点按钮都重新扫端口（这也是卡顿来源之一）
_SOCKS_CACHE = {"proxy": None, "checked": False, "info": None}


def get_socks_proxy(force: bool = False) -> Optional[str]:
    if _SOCKS_CACHE["checked"] and not force:
        return _SOCKS_CACHE["proxy"]
    _SOCKS_CACHE["proxy"] = detect_socks_proxy()
    _SOCKS_CACHE["checked"] = True
    return _SOCKS_CACHE["proxy"]


def get_exit_ip(proxy: str, force: bool = False):
    """带缓存的出口 IP 查询。"""
    if not proxy:
        return None
    if not force and _SOCKS_CACHE["info"] is not None:
        return _SOCKS_CACHE["info"]
    info = probe_exit_ip(proxy)
    _SOCKS_CACHE["info"] = info
    return info


# ---------- 出口 IP / 风控画像 ----------
# YouTube 对「数据中心出口 IP」极不友好：AWS / GCP / Azure / Vultr 等云主机 IP，
# 即使带完整登录态也频繁弹 "Sign in to confirm you're not a bot"。
# 这里把出口 IP 的归属查出来并预警，直接告诉用户「该换节点了」。
CLOUD_ISP_PATTERNS = ("amazon", "aws", "google", "microsoft", "azure", "digitalocean",
                      "vultr", "hetzner", "linode", "akamai", "oracle", "ovh",
                      "alibaba", "tencent", "huawei", "contabo", "scaleway", "choopa",
                      "cloud", "data center", "datacenter", "hosting", "-server")


def socks5_connect(proxy_host: str, proxy_port: int, target_host: str, target_port: int,
                   timeout: float = 3.0):
    """经 SOCKS5 建立到目标的 TCP 连接，返回已连接 socket（调用方负责关闭）；失败返回 None。"""
    try:
        s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")
        if s.recv(2) != b"\x05\x00":
            s.close()
            return None
        msg = (b"\x05\x01\x00\x03" + bytes([len(target_host)]) +
               target_host.encode() + target_port.to_bytes(2, "big"))
        s.sendall(msg)
        rep = s.recv(10)
        if len(rep) < 2 or rep[1] != 0:
            s.close()
            return None
        return s
    except Exception:
        return None


def probe_exit_ip(proxy_url: str, timeout: float = 8.0):
    """查询经代理访问外网时的出口 IP 归属。

    纯标准库实现：经 SOCKS5 发一个明文 HTTP 请求到 ip-api.com（无需 TLS / 第三方库）。
    返回 dict(ip/country/city/isp/org)，失败返回 None。
    """
    m = re.match(r"socks5://([^:]+):(\d+)", proxy_url or "")
    if not m:
        return None
    host, port = m.group(1), int(m.group(2))
    s = socks5_connect(host, port, "ip-api.com", 80, timeout=timeout)
    if not s:
        return None
    buf = b""
    try:
        s.sendall(b"GET /json HTTP/1.1\r\nHost: ip-api.com\r\n"
                  b"User-Agent: curl/8.0\r\nConnection: close\r\n\r\n")
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 65536:
                break
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass
    try:
        body = buf.split(b"\r\n\r\n", 1)[1]
        data = json.loads(body.decode("utf-8", "replace"))
        return {"ip": data.get("query"), "country": data.get("country"),
                "city": data.get("city"), "isp": data.get("isp"), "org": data.get("org")}
    except Exception:
        return None


def is_datacenter_ip(info) -> bool:
    """出口 IP 是否属于云机房/数据中心。"""
    if not info:
        return False
    blob = f"{info.get('isp', '')} {info.get('org', '')}".lower()
    return any(p in blob for p in CLOUD_ISP_PATTERNS)


# ---------- 正则：进度与失败模式识别 ----------
PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
ETA_RE = re.compile(r"ETA\s+(\d+:\d+)", re.I)
SPEED_RE = re.compile(r"at\s+([\d.]+\s*(?:KiB|MiB|GiB|B|KB|MB|GB)/s)", re.I)
ITEM_RE = re.compile(r"Downloading item\s+(\d+)\s+of\s+(\d+)", re.I)
DEST_RE = re.compile(r"Destination:\s*(.+)", re.I)

# yt-dlp 常见失败模式（用于在日志中给出中文引导）
LOGIN_REQUIRED_RE = re.compile(r"(sign in to (view|confirm)|please log in|login required|not a bot|需要登录|请先登录)", re.I)
COOKIE_LOCK_RE = re.compile(r"could not copy .*cookie database|cookie database is locked", re.I)
UNSUPPORTED_RE = re.compile(r"unsupported url", re.I)
ERROR_RE = re.compile(r"\bERROR\b", re.I)

# ---------- HLS(m3u8) 分片 403：识别 + 快速失败 ----------
# YouTube 在缺少有效 PO Token（播放凭证）时，会让整条 HLS 流的**每个**分片都返回 403。
# yt-dlp 默认对每个分片重试 10 次：一条 292 分片的视频会「逐个分片 × 10 次重试」慢慢磨，
# 最终仍然必然失败。识别出该模式后立刻中止下载并给出可执行的诊断，而不是白等。
FRAGMENT_403_RE = re.compile(r"HTTP Error 403[^\n]*fragment", re.I)
FRAGMENT_SKIP_RE = re.compile(r"fragment\s+not\s+found", re.I)
HLS_MARKER_RE = re.compile(r"\[(?:hlsnative|hls|m3u8)\]|\bm3u8\b", re.I)
# 连续出现这么多次「分片 403」即判定为不可恢复（远小于默认的 10 次/分片）
FRAGMENT_403_ABORT_AT = 4

# ---------- 人机验证兜底：扩展 player_client 列表 ----------
# yt-dlp 2026.10 的默认 player_client 只有 tv_samsung / visionos / web 三个，
# 只要这几个都返回 LOGIN_REQUIRED 就直接放弃，不再尝试其余客户端。
# 本兜底的做法是：检测到人机验证后，**多跑一次** yt-dlp，这一次显式传入下面这个
# 更完整的客户端列表，让 yt-dlp 在同一次提取里依次探测这些客户端，
# 有机会从 mweb / tv / web_embedded 等拿到可用的 m3u8 或初始数据而绕过部分 bot 校验。
# 注意：这不是「逐个客户端单独重试」，只是额外跑一轮完整列表，成本极低（多打几个 API）。
# 列表中的每个名字都必须是当前 yt-dlp 支持的客户端名，否则会被
# 「Skipping unsupported client」静默丢弃。
# 2026.10.26 实测（yt-dlp --extractor-args 传一堆候选名，看谁被报 unsupported）：
#   不支持：tv_embedded（2026.10.21 起即失效）、tv_embedded_2
#   支持  ：default / web / mweb / tv / tv_samsung / visionos / ios / android /
#           android_vr / web_safari / web_embedded / web_creator
# 据此在原有 8 个基础上补入 android（历史上可给出 DASH 且不强依赖 PO Token）
# 与 web_creator，提高拿到非 HLS 直链的概率。
YT_FALLBACK_PLAYER_CLIENTS = (
    "default,mweb,web,tv,android,ios,web_safari,android_vr,web_embedded,web_creator"
)

# ---------- 格式选择：优先 DASH，绕开 HLS 分片 403 ----------
# 「数据中心出口 IP + 无第一方登录 cookie」时 YouTube 常常只给 HLS(m3u8) 视频流，
# 而 HLS 分片几乎必然被 403（缺 PO Token）。因此首选纯 DASH 组合；
# 完全没有 DASH 时才回退到含 HLS 的常规选择——此时由上面的分片 403 快速失败兜底。
NOT_HLS_FILTER = "[protocol!=m3u8_native][protocol!=m3u8_entry]"

# ---------- yt-dlp 管道输出编码 ----------
# 实测 yt-dlp.exe（PyInstaller 版）在 Windows 下把管道输出按 **GBK(ANSI 代码页)** 编码，
# 且 PYTHONIOENCODING=utf-8 / PYTHONUTF8=1 对它都无效，只有 --encoding 能改。
# 按 UTF-8 硬解 GBK 字节会把中文标题读成乱码（日志里显示成一串 ?），
# 但**落盘文件名始终是正确的**——纯日志显示问题。
YTDLP_OUTPUT_ENCODING = "UTF-8"


def build_format_selector(mode: str, resolution: str = "1080") -> str:
    """按下载模式生成 yt-dlp 的 -f 表达式：先纯 DASH，再回退常规（可含 HLS）。

    Args:
        mode: DownloadOptions.mode，仅 "video_res" 会带上高度上限，其余按最佳画质。
        resolution: 分辨率上限字符串（video_res 模式下生效），非法值回退 1080。

    Returns:
        可直接作为 yt-dlp -f 参数的格式表达式。
    """
    if mode == "video_res":
        try:
            height = max(1, int(str(resolution).strip() or 1080))
        except (TypeError, ValueError):
            height = 1080
        return (f"bv*[height<={height}]{NOT_HLS_FILTER}+ba{NOT_HLS_FILTER}"
                f"/bv*[height<={height}]+ba/best[height<={height}]")
    return f"bv*{NOT_HLS_FILTER}+ba{NOT_HLS_FILTER}/bv*+ba/best"


def _build_stream_encodings() -> tuple:
    """子进程输出解码候选顺序：UTF-8 → 本机 ANSI(GBK) → GBK（去重、去空）。"""
    ordered = []
    for enc in ("utf-8", (locale.getpreferredencoding(False) or ""), "gbk"):
        key = (enc or "").strip().lower().replace("_", "-")
        if key and key != "utf8" and key not in ordered:
            ordered.append(key)
    return tuple(ordered) or ("utf-8",)


_STREAM_ENCODINGS = _build_stream_encodings()


def decode_stream_line(raw) -> str:
    """把子进程输出的一行（bytes）解码为 str：UTF-8 优先，失败回退本机 ANSI(GBK)。

    这样即使 --encoding 不被目标 yt-dlp 支持、或用户换用了别的 yt-dlp 版本，
    中文日志也不会再解码成一串问号。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in _STREAM_ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


# 按 **ytdlp_path** 为 key 缓存（OBS-01）：早先用单例缓存，先探一个老版本(False)
# 再探新版本会错误地继续返回 False。
_ENCODING_SUPPORT = {}


def ytdlp_supports_encoding(ytdlp_path: str = YTDLP) -> bool:
    """探测指定 yt-dlp 是否支持 --encoding（按路径缓存，避免重复起子进程）。

    老版本 yt-dlp 没有该开关，硬传会让整条命令因「no such option」失败，
    因此只在确认支持时才注入；不支持时退回到 decode_stream_line 的编码回退。

    Args:
        ytdlp_path: yt-dlp 可执行文件路径，结果按该路径的绝对路径分别缓存。

    Returns:
        True 表示该 yt-dlp 的帮助里含 --encoding，可安全注入。
    """
    try:
        key = os.path.abspath(ytdlp_path) if ytdlp_path else ""
    except Exception:
        key = str(ytdlp_path or "")
    if key in _ENCODING_SUPPORT:
        return bool(_ENCODING_SUPPORT[key])
    supported = False
    try:
        proc = subprocess.run(
            [ytdlp_path, "--help"], capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        supported = b"--encoding" in (proc.stdout or b"")
    except Exception:
        supported = False
    _ENCODING_SUPPORT[key] = supported
    return bool(supported)


DEFAULT_SETTINGS = {
    "output_dir": DEFAULT_OUTPUT,
    "mode": "video_best",        # video_best | video_res | audio
    "resolution": "1080",
    "proxy": "",
    "use_browser_cookies": False,
    "browser": "chrome",
    "cookies_file": "",
    "node_path": "",
    "include_playlist": False,
    # YouTube 的 GVS PO Token（格式 <客户端>+<token>，如 web+xxxxx），
    # 用于解开 HLS(m3u8) 分片的 403；留空则不注入。
    "po_token": "",
    # 代理是否被用户手动填写过（True 才持久化；否则每次启动重新自动探测）
    "proxy_manual": False,
}


# ---------- 设置持久化 ----------
def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s.update(json.load(f))
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------- 链接归一化（纯函数） ----------
def normalize_url(url: str) -> str:
    """把抖音各种形态的链接统一成 yt-dlp 可识别的 video/<ID> 格式。

    支持：
      - jingxuan?modal_id=ID  /  user?modal_id=ID  /  ?aweme_id=ID  /  ?video_id=ID
      - 分享页、精选页、个人主页里的视频参数
    其它站点（YouTube 等）或已是标准 video/<ID>、短链 v.douyin.com 原样返回。
    """
    u = (url or "").strip()
    if not u:
        return u
    m = re.search(r"(?:modal_id|aweme_id|video_id)=(\d+)", u)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    return u


# ---------- 下载配置（解耦全局变量） ----------
# --cookies-from-browser 要求浏览器**完全退出**：浏览器进程还在，其 cookie 数据库必然被锁
# （"Could not copy Chrome cookie database"）。提前探测可避免白跑一整轮下载。
BROWSER_PROCESS = {
    "chrome": "chrome.exe",
    "edge": "msedge.exe",
    "firefox": "firefox.exe",
    "brave": "brave.exe",
}


def browser_process_running(browser: str) -> bool:
    """判断指定浏览器是否仍在运行（Windows tasklist 实现，失败时保守返回 False）。"""
    exe = BROWSER_PROCESS.get((browser or "").lower())
    if not exe:
        return False
    try:
        # 中文 Windows 的 tasklist 输出是 GBK，若按 UTF-8 解码会抛 UnicodeDecodeError
        # 并丢掉 stdout，导致「浏览器明明开着却检测不到」。改为直接比对字节，彻底规避编码。
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}"],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return exe.lower().encode() in (r.stdout or b"").lower()
    except Exception:
        return False


# ---------- Chrome cookie 只读体检 ----------
# 只做「复制到临时目录后读 SQLite」，绝不启动浏览器、绝不写回任何配置档。
CHROME_USER_DATA = os.path.join(os.path.expanduser("~"), "AppData", "Local",
                                "Google", "Chrome", "User Data")
LOGIN_COOKIE_NAMES = {"SID", "__Secure-1PSID", "LOGIN_INFO", "HSID", "SSID"}


def chrome_cookie_diagnostics() -> dict:
    """只读扫描 Chrome 各配置档，报告 YouTube 登录态位置与 cookie 加密方式。

    返回 {"abe": bool, "profiles": [ {profile,total,youtube,login,enc} ... ]}
    enc 为 "v20" 时代表 Chrome 应用绑定加密：yt-dlp 的 --cookies-from-browser 必然失败。
    """
    out = {"abe": False, "profiles": []}
    if not os.path.isdir(CHROME_USER_DATA):
        return out
    try:
        with open(os.path.join(CHROME_USER_DATA, "Local State"), encoding="utf-8") as fh:
            j = json.load(fh)
        out["abe"] = "app_bound_encrypted_key" in (j.get("os_crypt") or {})
    except Exception:
        pass
    try:
        names = sorted(os.listdir(CHROME_USER_DATA))
    except Exception:
        return out
    for name in names:
        db = os.path.join(CHROME_USER_DATA, name, "Network", "Cookies")
        if not os.path.isfile(db):
            continue
        info = {"profile": name, "total": 0, "youtube": 0, "login": False, "enc": ""}
        try:
            tmp = os.path.join(tempfile.mkdtemp(prefix="vdscan_"), "c.db")
            shutil.copy2(db, tmp)
            con = sqlite3.connect(tmp)
            cur = con.cursor()
            cur.execute("SELECT count(*) FROM cookies")
            info["total"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM cookies WHERE host_key LIKE '%youtube%'")
            info["youtube"] = cur.fetchone()[0]
            cur.execute("SELECT name FROM cookies WHERE host_key LIKE '%youtube.com'")
            got = {r[0] for r in cur.fetchall()}
            info["login"] = bool(got & LOGIN_COOKIE_NAMES)
            try:
                cur.execute("SELECT hex(substr(encrypted_value,1,3)) FROM cookies "
                            "WHERE length(encrypted_value) > 0 LIMIT 1")
                row = cur.fetchone()
                if row and row[0]:
                    info["enc"] = bytes.fromhex(row[0]).decode("latin-1")
            except Exception:
                pass
            con.close()
        except Exception:
            pass
        out["profiles"].append(info)
    return out


_ABE_CACHE = {"value": None}


def chrome_abe_enabled() -> bool:
    """Chrome cookie 是否为 v20 应用绑定加密（结果缓存，避免重复扫盘）。"""
    if _ABE_CACHE["value"] is None:
        try:
            d = chrome_cookie_diagnostics()
            _ABE_CACHE["value"] = bool(d["abe"]) or any(p["enc"] == "v20" for p in d["profiles"])
        except Exception:
            _ABE_CACHE["value"] = False
    return bool(_ABE_CACHE["value"])


@dataclass
class DownloadOptions:
    ytdlp_path: str = YTDLP
    ffmpeg_path: Optional[str] = None
    node_path: Optional[str] = None
    mode: str = "video_best"
    resolution: str = "1080"
    output_dir: str = DEFAULT_OUTPUT
    proxy: str = ""
    use_browser_cookies: bool = False
    browser: str = "chrome"
    cookies_file: str = ""
    include_playlist: bool = False
    # 留空 = 用 yt-dlp 默认客户端；非空则在人机验证兜底时轮换更完整的客户端列表
    player_clients: str = ""
    # YouTube GVS PO Token，格式 "<客户端>+<token>"（如 web+xxxxx）；
    # 多个用逗号分隔。用于解开 HLS(m3u8) 分片的 403。
    po_token: str = ""


def build_command(opts: DownloadOptions, urls: List[str]) -> List[str]:
    """由显式选项与链接列表构造 yt-dlp 命令行，不依赖任何全局变量。"""
    cmd = [opts.ytdlp_path, "--no-warnings", "--newline", "--ignore-errors"]

    # 把管道输出固定为 UTF-8，避免中文标题在日志里被解码成一串问号。
    # 仅当该 yt-dlp 支持 --encoding 时才注入（老版本硬传会直接报 no such option）。
    if YTDLP_OUTPUT_ENCODING and ytdlp_supports_encoding(opts.ytdlp_path):
        cmd += ["--encoding", YTDLP_OUTPUT_ENCODING]

    if opts.mode == "audio":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        # -f 首选纯 DASH 组合：HLS 分片在缺 PO Token 时会整条 403
        cmd += ["-f", build_format_selector(opts.mode, opts.resolution),
                "--merge-output-format", "mp4"]

    # 默认不下载整个播放列表，避免误贴列表链接触发几十个下载；勾选后可包含
    if not opts.include_playlist:
        cmd += ["--no-playlist"]

    out = opts.output_dir.strip()
    if out:
        os.makedirs(out, exist_ok=True)
        cmd += ["-P", out]
    cmd += ["-o", "%(title)s [%(id)s].%(ext)s"]

    if opts.proxy:
        cmd += ["--proxy", opts.proxy]
    else:
        # 用户未填代理时，强制直连，避免 yt-dlp 回退到环境变量里的（如 WorkBuddy 本地）代理导致 YouTube 被标记/失败
        cmd += ["--proxy", ""]

    if opts.cookies_file:
        cmd += ["--cookies", opts.cookies_file]
    elif opts.use_browser_cookies:
        cmd += ["--cookies-from-browser", opts.browser]

    # Windows 下标题常含 ":" / "?" 等非法字符，显式按 Windows 规则清洗文件名
    cmd += ["--windows-filenames"]

    if opts.ffmpeg_path and os.path.isfile(opts.ffmpeg_path):
        cmd += ["--ffmpeg-location", os.path.dirname(opts.ffmpeg_path)]

    # 显式指定 Node.js 运行时，确保 yt-dlp 能解开 YouTube 的 n 签名挑战
    # （yt-dlp 在某些环境下对 PATH 中的 node 探测会失败，显式指定最稳）。
    if opts.node_path and os.path.isfile(opts.node_path):
        cmd += ["--js-runtimes", f"node:{opts.node_path}"]

    # 人机验证兜底：轮换更完整的 player_client 列表
    # PO Token：解开 HLS 分片 403 的关键，格式 "<客户端>+<token>"
    # 实测（yt-dlp 2026.10.26）：
    #   - 同一 extractor 的多个参数用 ";" 分隔；
    #   - 重复传 --extractor-args 时**后者整体覆盖前者**，因此必须合并成一条。
    yt_args = []
    if opts.player_clients:
        yt_args.append(f"player_client={opts.player_clients}")
    po_token = (opts.po_token or "").strip()
    if po_token:
        yt_args.append(f"po_token={po_token}")
    if yt_args:
        cmd += ["--extractor-args", "youtube:" + ";".join(yt_args)]

    cmd += list(urls)
    return cmd


# ---------- 进程树精确终止（绝不盲目 taskkill /T） ----------
# yt-dlp.exe 是 PyInstaller --onefile 打包，运行时是「父 bootloader + 子真实进程」两级：
#     PID A  Parent=我们            yt-dlp.exe   ← 父 bootloader（我们 Popen 出来的）
#     PID B  Parent=A               yt-dlp.exe   ← 真正的 yt-dlp（干活的）
# Popen.terminate() 只能杀掉父进程 A；子进程 B 会**带着 stdout 管道句柄**继续跑完
# 292 分片 × 10 次重试，同时管道永远不 EOF → _reader 不退出 → on_finished 永不回调
# → 界面一直卡在「下载中」、开始按钮保持禁用、用户点「停止」也失效。
#
# 但直接用 taskkill /F /T /PID 是「按 PID 杀整棵树」：如果目标进程已经退出、
# 该 PID 被系统重新分配给别的进程，就会把那个**无关进程的整棵树**一起杀掉。
# 因此这里改成精确实现：
#   1) 用 CIM/WMI 递归查出 ParentProcessId 链上的真实后代；
#   2) kill 前逐个校验映像名 == yt-dlp.exe，不符就跳过，绝不杀；
#   3) 只有 CIM 查询不可用时才回退 taskkill /F /T，且回退前同样校验映像名；
#   4) 最后仍保留 Popen.terminate() 兜底。
YTDLP_EXE_NAME = "yt-dlp.exe"
_POWERSHELL_EXE = "powershell.exe"
_PS_TIMEOUT = 20.0

# ---- 首选实现：Job Object（进程内 Win32 API，约 0ms）----
# 相比 taskkill /F /T 或其它「按 PID 找进程」的做法，Job Object 的成员是**按句柄**
# 归属的：Job 里只可能有我们显式加入的进程及其后代，终止时不经过任何 PID 查找，
# 因此**根本不存在 PID 被系统复用导致误杀无关进程**的可能。
# 代价：只有在加入 Job **之后**才创建的后代才会被纳入，所以必须在 Popen 之后
# 立刻挂载（见 DownloadEngine.start）；另外由 stop() 起一个看门狗兜底。
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = -1
_MAX_PATH = 260

_kernel32 = None
if sys.platform == "win32":
    try:
        import ctypes

        class _PROCESSENTRY32W(ctypes.Structure):
            """PROCESSENTRY32W：ToolHelp32 的进程快照条目。

            字段必须与 Win32 定义**逐一对齐**：中间漏掉 th32ModuleID 一个字段，
            后面每个字段都会错位 4 字节，th32ParentProcessID 就会读到垃圾值
            （父子关系全乱 → 树查询失效）。这个坑踩过一次，勿删。
            """

            _fields_ = [
                ("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t),   # ULONG_PTR
                ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_int32),
                ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_wchar * _MAX_PATH),
            ]

        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # ---- Job Object：成员按句柄归属，终止时不经过任何 PID 查找 ----
        _kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        _kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        _kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _kernel32.TerminateJobObject.restype = ctypes.c_int
        _kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        # ---- 打开进程 / 查映像路径 / 终止（精确到句柄） ----
        _kernel32.OpenProcess.restype = ctypes.c_void_p
        _kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        _kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
        _kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32)]
        _kernel32.TerminateProcess.restype = ctypes.c_int
        _kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        # ---- ToolHelp32 进程快照 ----
        _kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        _kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        _kernel32.Process32FirstW.restype = ctypes.c_int
        _kernel32.Process32FirstW.argtypes = [ctypes.c_void_p,
                                              ctypes.POINTER(_PROCESSENTRY32W)]
        _kernel32.Process32NextW.restype = ctypes.c_int
        _kernel32.Process32NextW.argtypes = [ctypes.c_void_p,
                                             ctypes.POINTER(_PROCESSENTRY32W)]
        _kernel32.CloseHandle.restype = ctypes.c_int
        _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    except Exception:
        _kernel32 = None

# 递归列出后代：输出 "PID\t映像名"
_PS_TREE_SCRIPT = """
$ErrorActionPreference = 'SilentlyContinue'
$root = __ROOT__
$seen = @{}
$seen[$root] = $true
$q = [System.Collections.Queue]::new()
$q.Enqueue($root)
while ($q.Count -gt 0) {
  $cur = $q.Dequeue()
  $kids = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $cur")
  foreach ($k in $kids) {
    $kid = [int]$k.ProcessId
    if (-not $seen.ContainsKey($kid)) {
      $seen[$kid] = $true
      Write-Output ($kid.ToString() + "`t" + [string]$k.Name)
      $q.Enqueue($kid)
    }
  }
}
"""

# 按 PID 精确终止：先取回进程对象、校验映像名，再对**同一个 CIM 对象**调用 Terminate
_PS_KILL_SCRIPT = """
$ErrorActionPreference = 'SilentlyContinue'
$want = '__NAME__'
foreach ($p in @(__PIDS__)) {
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $p"
  if ($null -eq $proc) { Write-Output ([string]$p + "`tnotfound"); continue }
  if ($proc.Name -ieq $want) {
    $null = $proc | Invoke-CimMethod -MethodName Terminate
    Write-Output ([string]$p + "`tkilled")
  } else {
    Write-Output ([string]$p + "`tskipped:" + [string]$proc.Name)
  }
}
"""

_PS_NAME_SCRIPT = """
$ErrorActionPreference = 'SilentlyContinue'
$proc = Get-CimInstance Win32_Process -Filter "ProcessId = __PID__"
if ($null -ne $proc) { Write-Output ([string]$proc.Name) }
"""


def attach_job_object(pid: int):
    """把 pid 对应的进程挂进一个新建的 Job Object，返回 job 句柄（失败返回 None）。

    Job 成员按**句柄**归属，后续由该进程创建的子孙会自动加入同一个 Job，
    因此后面只需一次 TerminateJobObject 就能精确终止整棵进程树，
    且完全不涉及 PID 查找，不存在「PID 复用误杀」的风险。
    """
    if sys.platform != "win32" or _kernel32 is None:
        return None
    try:
        job = _kernel32.CreateJobObjectW(None, None)
    except Exception:
        return None
    if not job:
        return None
    handle = None
    try:
        handle = _kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not handle:
            raise OSError("OpenProcess failed")
        if not _kernel32.AssignProcessToJobObject(job, handle):
            raise OSError("AssignProcessToJobObject failed")
    except Exception:
        try:
            _kernel32.CloseHandle(job)
        except Exception:
            pass
        return None
    finally:
        if handle:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                pass
    return job


def terminate_job_object(job) -> bool:
    """终止 Job 内**全部**进程（仅可能是我们挂进去的那棵树）。"""
    if sys.platform != "win32" or _kernel32 is None or not job:
        return False
    try:
        return bool(_kernel32.TerminateJobObject(job, 1))
    except Exception:
        return False


def close_job_object(job) -> None:
    """释放 job 句柄（进程已退出后调用，无副作用）。"""
    if sys.platform != "win32" or _kernel32 is None or not job:
        return
    try:
        _kernel32.CloseHandle(job)
    except Exception:
        pass


def _name_matches(actual: Optional[str], expect: str) -> bool:
    """映像名是否匹配（按基名比较，不区分大小写；Windows 的映像名大小写不固定）。"""
    if not actual or not expect:
        return False
    return os.path.basename(str(actual).strip()).lower() == os.path.basename(str(expect).strip()).lower()


def _snapshot_processes() -> Optional[List[dict]]:
    """用 ToolHelp32 快照本机全部进程的 pid / 父 pid / 映像名。

    相比 PowerShell CIM，这是**进程内**调用（几十毫秒），不会给「分片 403 快速失败」
    增加秒级延迟（实测 CIM 一次查询约 0.85s）。失败返回 None，由调用方回退 CIM。

    Returns:
        [{"pid": int, "ppid": int, "name": str}, ...] 或 None
    """
    if sys.platform != "win32" or _kernel32 is None:
        return None
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or int(snap) == _INVALID_HANDLE:
        return None
    procs: List[dict] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            procs.append({
                "pid": int(entry.th32ProcessID),
                "ppid": int(entry.th32ParentProcessID),
                "name": str(entry.szExeFile or ""),
            })
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    except Exception:
        return None
    finally:
        try:
            _kernel32.CloseHandle(snap)
        except Exception:
            pass
    return procs


def _terminate_pid_native(pid: int, expect_name: str = YTDLP_EXE_NAME) -> str:
    """打开 PID 对应进程，按**映像路径**校验后再终止。

    关键点：校验与终止作用于**同一个已打开的进程句柄**——句柄绑定的是具体的进程对象，
    因此即便 PID 在「查询」与「终止」之间被系统复用，也不会误杀别的进程。

    Returns:
        "killed" | "notfound" | "skipped:<映像名>" | "failed"
    """
    if sys.platform != "win32" or _kernel32 is None:
        return "failed"
    # 一口气申请「查询 + 终止」两种权限，避免两次 OpenProcess 之间出现窗口
    access = _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_TERMINATE
    handle = _kernel32.OpenProcess(access, False, int(pid))
    if not handle:
        # OpenProcess 失败有两种完全不同的含义，必须区分：
        #   ERROR_ACCESS_DENIED(5)     = 进程还在，只是我们权限不够 → 交给上层换手段重试
        #   ERROR_INVALID_PARAMETER(87)（含 PID 不存在）= 进程已退出
        try:
            err = int(ctypes.get_last_error())
        except Exception:
            err = 0
        return "failed" if err == 5 else "notfound"
    try:
        buf = ctypes.create_unicode_buffer(_MAX_PATH)
        size = ctypes.c_uint32(_MAX_PATH)
        got = _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        image = str(buf.value) if got else ""
        if not image:
            # 拿不到映像路径 → 无法确认身份 → **绝不盲杀**
            return "failed"
        if not _name_matches(image, expect_name):
            return f"skipped:{os.path.basename(image)}"
        return "killed" if _kernel32.TerminateProcess(handle, 1) else "failed"
    except Exception:
        return "failed"
    finally:
        try:
            _kernel32.CloseHandle(handle)
        except Exception:
            pass


def _pid_alive(pid: int) -> bool:
    """该 PID 上是否仍有**任意**进程存在（只申请最低限度的查询权限）。

    拿不到句柄即视为已不存在；平台不支持时返回 False（让等待立即结束，不拖慢流程）。
    """
    if sys.platform != "win32" or _kernel32 is None:
        return False
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        _kernel32.CloseHandle(handle)
    except Exception:
        pass
    return True


def _wait_pids_gone(pids: List[int], timeout: float = 2.0) -> None:
    """轮询等待这批 PID 真的消失（最多 timeout 秒，50ms 一次）。

    目的是让「到底还有没有残留」在 kill_process_tree 返回前就有确定答案。
    """
    deadline = time.time() + max(0.0, float(timeout))
    pending = [int(p) for p in pids]
    while pending and time.time() < deadline:
        pending = [p for p in pending if _pid_alive(p)]
        if pending:
            time.sleep(0.05)


def _kill_pid_taskkill_filtered(pid: int, expect_name: str) -> bool:
    """用带 IMAGENAME 过滤的 taskkill 精确终止**单个** PID（绝不带 /T）。

    taskkill 会在真正下手的那一刻按过滤器再核一次映像名，等于给「查到 PID → 下手」
    之间那几十毫秒的 PID 复用窗口加了第二道锁。

    坑：没匹配到任务时 taskkill **仍返回 0**，只能靠输出文本判断是否真杀了
    （英文「SUCCESS: …」/ 中文「成功: …」）。
    """
    name = os.path.basename(str(expect_name or "").strip())
    if not name:
        return False
    try:
        proc = subprocess.run(
            ["taskkill", "/F", "/FI", f"PID eq {int(pid)}", "/FI", f"IMAGENAME eq {name}"],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return False
    text = decode_stream_line(proc.stdout or b"").lower()
    return ("success" in text) or ("成功" in text)


def _run_powershell(script: str, timeout: float = _PS_TIMEOUT) -> Optional[str]:
    """执行一段 PowerShell 脚本并回传 stdout 文本；失败/超时返回 None。"""
    try:
        proc = subprocess.run(
            [_POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return decode_stream_line(proc.stdout or b"")


def query_process_tree(root_pid: int) -> Optional[List[dict]]:
    """查出 root_pid 的**全部后代**（不含 root 自身）。

    Returns:
        [{"pid": int, "name": str}, ...]（按广度优先，父在前、子在后）；
        CIM 不可用或查询失败时返回 None——调用方据此走回退路径。
    """
    if sys.platform != "win32":
        return None
    try:
        root = int(root_pid)
    except (TypeError, ValueError):
        return None
    if root <= 0:
        return None

    # 首选进程内 ToolHelp32 快照：几十毫秒，不依赖 PowerShell。
    # （实测一次 CIM 查询约 0.85s；把它放在「分片 403 快速失败」路径上，
    #   会把 on_finished 的回调时间从 QA 基线 4.0s 拖慢到 ~5.5s。）
    snap = _snapshot_processes()
    if snap:
        children_of = {}
        for item in snap:
            children_of.setdefault(item["ppid"], []).append(item)
        nodes: List[dict] = []
        seen = {root}
        queue = [root]
        while queue:
            cur = queue.pop(0)
            for child in children_of.get(cur, []):
                if child["pid"] in seen:
                    continue
                seen.add(child["pid"])
                nodes.append({"pid": child["pid"], "name": child["name"]})
                queue.append(child["pid"])
        return nodes

    # 回退：PowerShell CIM
    text = _run_powershell(_PS_TREE_SCRIPT.replace("__ROOT__", str(root)))
    if text is None:
        return None
    nodes = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or "\t" not in ln:
            continue
        pid_str, _, name = ln.partition("\t")
        try:
            nodes.append({"pid": int(pid_str.strip()), "name": name.strip()})
        except ValueError:
            continue
    return nodes


def kill_pids_if_named(pids: List[int], expect_name: str = YTDLP_EXE_NAME) -> dict:
    """逐个精确终止 pids：只杀映像名 == expect_name 的进程，其余一律跳过。

    校验与终止作用于**同一个 CIM 进程对象**，可最大限度避免「PID 已被系统复用」
    导致的误杀。

    Returns:
        {pid: "killed" | "notfound" | "skipped:<实际名>" | "failed"}
    """
    result = {}
    if sys.platform != "win32":
        return {int(p): "failed" for p in pids}
    clean = []
    for p in pids:
        try:
            clean.append(int(p))
        except (TypeError, ValueError):
            continue
    if not clean:
        return result

    # 首选进程内实现：OpenProcess 拿到句柄 → 按**映像路径**校验 → 成功才 TerminateProcess。
    # 校验与终止作用于同一个句柄，而句柄绑定的是具体的进程对象，因此不会因
    # 「PID 已被系统复用」而误杀别的进程。
    if _kernel32 is not None:
        for pid in clean:
            result[pid] = _terminate_pid_native(pid, expect_name)
        # 少数 PID 可能因权限不足没能下手（"failed"），交给下面的 CIM 再试一轮
        clean = [p for p in clean if result.get(p) == "failed"]
        if not clean:
            return result

    # 回退：PowerShell CIM（同样先取回进程对象校验映像名，再对同一个对象 Terminate）
    script = (_PS_KILL_SCRIPT
              .replace("__NAME__", str(expect_name).replace("'", "''"))
              .replace("__PIDS__", ",".join(str(p) for p in clean)))
    text = _run_powershell(script)
    if text is None:
        return {p: "failed" for p in clean}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or "\t" not in ln:
            continue
        pid_str, _, status = ln.partition("\t")
        try:
            result[int(pid_str.strip())] = status.strip()
        except ValueError:
            continue
    for p in clean:
        result.setdefault(p, "failed")
    return result


def process_name_of(pid: int) -> Optional[str]:
    """查询指定 PID 的映像名；查不到或 CIM 不可用返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        target = int(pid)
    except (TypeError, ValueError):
        return None
    snap = _snapshot_processes()
    if snap:
        for item in snap:
            if item["pid"] == target:
                return item["name"] or None
        return None
    text = _run_powershell(_PS_NAME_SCRIPT.replace("__PID__", str(target)))
    if text is None:
        return None
    name = text.strip().splitlines()[0].strip() if text.strip() else ""
    return name or None


def kill_process_tree(root_pid: int, expect_name: str = YTDLP_EXE_NAME,
                      wait: float = 0.0) -> dict:
    """精确终止以 root_pid 为根的进程树（含 root 自身）。

    **绝不盲目 taskkill /F /T**：/T 是「按 PID 杀整棵树」，目标进程若已退出、
    且该 PID 被 Windows 重新分配给别的进程，/T 会把那个**无关进程的整棵树**
    一起杀掉。用户机器上什么进程都可能撞上这个 PID，后果承担不起。

    本函数改为「先认清，再动手」：
      1) 查 ParentProcessId 链上的真实后代（进程内 ToolHelp32 快照优先，失败回退
         CIM/WMI），**不假设**某个 PID 一定属于我们；
      2) 逐个按 PID 精确杀，每一刀之前都校验映像名 == expect_name，不符就跳过。
         注意 root 自身同样要过这一关——PID 被复用时最危险的恰恰是 root；
      3) 三层实现全部是「按单个 PID / 按句柄」的精确终止，没有一层按树杀：
           ① OpenProcess 句柄 → 按映像**路径**校验 → TerminateProcess
              （毫秒级；句柄绑定进程对象，彻底不存在 PID 复用竞态）
           ② PowerShell CIM：取回进程对象、校验映像名，再对同一对象 Terminate
           ③ taskkill /F /FI "PID eq X" /FI "IMAGENAME eq <name>"
              （taskkill 下手那一刻会按过滤器再核一次映像名）
      4) 只有三层全部失败时才允许回退 taskkill /F /T，且回退前**重新确认**
         root 的映像名；确认不上就宁可不杀，并在 skipped 里写明原因。

    Args:
        root_pid: 我们 Popen 出来的进程 PID（进程树的根）。
        expect_name: 允许终止的映像名，默认 yt-dlp.exe。
        wait: 杀完后最多等几秒确认这些 PID 真的消失（0 = 不等）。
              DownloadEngine.stop() 传 2.0，让「有没有残留」在返回前就有结论。

    Returns:
        {"killed": [pid...], "skipped": {pid: 原因}, "method": str, "query_ok": bool}

    本函数保证不向调用方抛异常——stop() 若被异常炸掉，界面会彻底卡死。
    """
    out = {"killed": [], "skipped": {}, "method": "", "query_ok": False}
    try:
        if sys.platform != "win32":
            out["method"] = "unsupported_platform"
            return out
        try:
            root = int(root_pid)
        except (TypeError, ValueError):
            out["method"] = "invalid_pid"
            return out
        if root <= 0:
            out["method"] = "invalid_pid"
            return out
        name = os.path.basename(str(expect_name or "").strip())
        # expect_name 必须是单个映像文件名：带引号/空值一律拒绝，避免它作为
        # taskkill 过滤器或 PowerShell 变量时被解释成别的东西
        if not name or name in (".", "..") or '"' in name or "'" in name:
            out["method"] = "invalid_expect_name"
            return out

        tree = query_process_tree(root)
        if tree is None:
            # 两种查询都拿不到进程表 → 允许回退 taskkill /F /T，
            # 但**回退前必须先确认 root 的映像名**仍是 expect_name，否则宁可不杀
            out["method"] = "fallback_taskkill_tree"
            cur = process_name_of(root)
            if _name_matches(cur, name):
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(root)],
                                   capture_output=True, timeout=10,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    out["killed"].append(root)
                except Exception:
                    out["skipped"][root] = "taskkill_failed"
            else:
                out["skipped"][root] = f"name_mismatch:{cur or 'unknown'}"
            if wait and out["killed"]:
                _wait_pids_gone(out["killed"], wait)
            return out

        out["query_ok"] = True
        # 后代先杀：BFS 序列里后代一定排在祖先之后，反转即可保证
        # 「先杀子进程、最后杀根进程」，避免父进程死后又派生出新的子进程
        ordered = []
        for node in reversed(tree):
            try:
                ordered.append(int(node.get("pid")))
            except (TypeError, ValueError):
                continue
        ordered.append(root)

        status = kill_pids_if_named(ordered, name)
        escalated = False
        for pid in ordered:
            st = status.get(pid, "failed")
            if st == "killed":
                out["killed"].append(pid)
            elif str(st).startswith("skipped:") or st == "notfound":
                # 名字不符（疑似 PID 被复用）或已退出：不再换任何手段重试
                out["skipped"][pid] = st
            else:
                # 精确终止没做成（权限不足等，但进程还在）→ 再试第三层：
                # 带 IMAGENAME 过滤的**单 PID** taskkill（同样不是按树杀）
                if _kill_pid_taskkill_filtered(pid, name):
                    out["killed"].append(pid)
                    escalated = True
                else:
                    out["skipped"][pid] = st
        out["method"] = "precise+taskkill_filtered" if escalated else "precise"
        if wait and out["killed"]:
            _wait_pids_gone(out["killed"], wait)
        return out
    except Exception as exc:  # 绝不把异常抛给调用方
        out["method"] = f"exception:{type(exc).__name__}"
        return out

# ---------- 下载引擎（与 UI 解耦） ----------
class DownloadEngine:
    def __init__(self, on_log, on_progress, on_status, on_finished):
        self.proc = None
        self.running = False
        self.on_log = on_log
        self.on_progress = on_progress
        self.on_status = on_status
        self.on_finished = on_finished
        # 由 _reader 写入：非空代表本轮是「被我们主动中止」的，并说明中止原因
        self.abort_reason = ""
        self._frag403 = 0
        # 由 stop() 写入：最近一次进程树终止的结果（便于排障与测试断言）
        self.last_kill = None
        # Job Object 句柄：整棵进程树的归属容器（精确终止的主要手段）
        self.job = None

    def start(self, cmd: List[str]):
        self.running = True
        self.abort_reason = ""
        self._frag403 = 0
        self.last_kill = None
        self.job = None
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        # 构造子进程环境：保留系统环境，并在存在 WorkBuddy node shim 时中和 Node 权限模型，
        # 否则 yt-dlp 调 node 解 n 挑战会被 "Access to this API has been restricted" 拦住。
        env = build_node_env(os.environ)
        # 按**字节**读取：yt-dlp 在 Windows 管道下的实际编码可能是 GBK(ANSI 代码页)，
        # 若在这里直接 text=True/encoding="utf-8" 硬解，中文标题会变成乱码。
        # 编码判定交给 decode_stream_line() 逐行回退处理。
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=-1, creationflags=create_no_window, env=env,
        )
        # 立刻把子进程挂进 Job Object：yt-dlp.exe 是 PyInstaller 两级进程，
        # 只有**挂载之后**创建的子孙才会在 Job 内，所以必须在 Popen 后马上做
        # （bootloader 解包需数百毫秒，这里必然赶在它创建子进程之前）。
        self.job = attach_job_object(self.proc.pid)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _abort_on_fragment_403(self):
        """分片被连续 403：立即中止，避免 292 个分片各重试 10 次慢慢磨。"""
        if self.abort_reason:
            # 已中止过：子进程终止前管道里可能还有残留行，这里不再重复打日志、重复 kill
            return
        self.abort_reason = "hls403"
        self.on_log("🛑 已主动中止：HLS(m3u8) 分片连续被 YouTube 拒绝（HTTP 403）。"
                    "继续重试也不会成功，停止以免长时间空转。")
        self.stop()

    def _reader(self, proc):
        had_error = False
        saw_bot = False
        frag_403_hinted = False
        saw_hls = False
        for raw in proc.stdout:
            line = decode_stream_line(raw).rstrip("\r\n")
            if HLS_MARKER_RE.search(line):
                saw_hls = True
            # 分片 403 快速失败：必须在吞掉整行之前判定，否则会一直磨到所有分片重试完
            if (FRAGMENT_403_RE.search(line) or FRAGMENT_SKIP_RE.search(line)) \
                    and not self.abort_reason:
                self._frag403 += 1
                if not frag_403_hinted:
                    frag_403_hinted = True
                    kind = "HLS(m3u8) 分片" if saw_hls else "分片"
                    self.on_log(f"⚠ {kind}被拒绝（403）：通常是缺少有效的 PO Token，"
                                f"或当前出口 IP 被风控。")
                if self._frag403 >= FRAGMENT_403_ABORT_AT:
                    self._abort_on_fragment_403()
                    continue
            if "[download]" in line:
                # 当前文件序号 / 总数
                mi = ITEM_RE.search(line)
                if mi:
                    self.on_status(f"正在下载第 {mi.group(1)}/{mi.group(2)} 个")
                md = DEST_RE.search(line)
                if md:
                    self.on_log(f"📄 输出文件：{md.group(1).strip()}")
                m = PERCENT_RE.search(line)
                if m:
                    try:
                        self.on_progress(float(m.group(1)))
                    except ValueError:
                        pass
                me = ETA_RE.search(line)
                ms = SPEED_RE.search(line)
                if me or ms:
                    extra = []
                    if ms:
                        extra.append(f"速度 {ms.group(1)}")
                    if me:
                        extra.append(f"剩余 {me.group(1)}")
                    if extra:
                        self.on_status("下载中… " + "，".join(extra))
            # 常见失败模式给出中文引导
            if COOKIE_LOCK_RE.search(line):
                self.on_log("⚠ 无法读取浏览器 cookie 库：请先【关闭 Chrome/Edge 全部窗口】，或改用「选择 cookies.txt」方式，再重试。")
            elif LOGIN_REQUIRED_RE.search(line):
                saw_bot = True
                self.on_log("⚠ 检测到登录/人机验证拦截（bot check）。")
            elif UNSUPPORTED_RE.search(line):
                self.on_log("⚠ 链接不被支持：试试把抖音链接改成 https://www.douyin.com/video/<ID> 形式。")
            elif ERROR_RE.search(line):
                had_error = True
            self.on_log(line)
        proc.stdout.close()
        rc = proc.wait()
        self.running = False
        close_job_object(getattr(self, "job", None))
        self.job = None
        self.on_finished(rc, had_error, saw_bot)

    def stop(self):
        """终止下载进程树。

        yt-dlp.exe 是 PyInstaller --onefile 打包，「父 bootloader + 子真实进程」两级，
        只 terminate() 父进程的话，子进程会带着 stdout 管道句柄继续跑完 292 分片 ×
        10 次重试，同时管道永远不 EOF → _reader 不退出 → on_finished 永不回调
        → 界面一直卡在「下载中」，用户点「停止」同样失效。

        但这里**不能**直接用 taskkill /F /T /PID：那是按 PID 杀整棵树，目标若已退出
        且 PID 被系统复用，会误杀无关进程的整棵树。因此按四级递进终止：
          1) Job Object（首选，按句柄归属，不做 PID 查找，约 0ms）；
          2) ToolHelp32 查真实后代 → 逐个按**映像路径**校验后 TerminateProcess；
          3) PowerShell CIM 查后代 → 按映像名校验后 Terminate；
          4) 都不可用时才 taskkill /F /T，且回退前同样校验映像名；
        最后仍保留 proc.terminate() 兜底，并起看门狗覆盖 Job 的挂载竞态。
        """
        if not (self.proc and self.running):
            return
        # 1) Job Object（首选）：一次调用终止整棵进程树，成员按句柄归属，
        #    不做任何 PID 查找 → 不存在 PID 复用误杀的可能，且耗时约 0ms。
        killed_by_job = False
        if sys.platform == "win32" and self.job:
            killed_by_job = terminate_job_object(self.job)
            if killed_by_job:
                self.last_kill = {"killed": [self.proc.pid], "skipped": {},
                                  "method": "job_object", "query_ok": True}
        # 2) Job 不可用（如本机不支持/挂载失败）→ 精确查进程树 + 映像名校验后 kill
        if not killed_by_job and sys.platform == "win32":
            try:
                self.last_kill = kill_process_tree(self.proc.pid, YTDLP_EXE_NAME)
            except Exception:
                self.last_kill = None
        # 3) 兜底：非 Windows，或上面全部失败/跳过时，仍能杀掉直接子进程
        try:
            self.proc.terminate()
        except Exception:
            pass
        # 4) 看门狗：Job 只覆盖「挂载之后」创建的子孙。万一 bootloader 抢先一步
        #    已经起了子进程（理论上的竞态），管道就不会 EOF。这里 0.5s 后若仍在
        #    运行，就补一次「查进程树 + 映像名校验」的精确 kill。
        threading.Thread(target=self._terminate_watchdog, daemon=True).start()

    def _terminate_watchdog(self, grace: float = 0.5):
        """stop() 之后的兜底：仍在运行就补一次精确 kill（见 stop() 第 4 步）。"""
        time.sleep(grace)
        if not (self.proc and self.running):
            return
        if sys.platform != "win32":
            return
        try:
            extra = kill_process_tree(self.proc.pid, YTDLP_EXE_NAME)
        except Exception:
            return
        prev = self.last_kill or {}
        self.last_kill = {
            "killed": list(prev.get("killed") or []) + list(extra.get("killed") or []),
            "skipped": dict(prev.get("skipped") or {}, **(extra.get("skipped") or {})),
            "method": f"{prev.get('method', 'none')}+watchdog:{extra.get('method')}",
            "query_ok": bool(extra.get("query_ok")),
        }


# ---------- 环境诊断（无 GUI 也可用） ----------
def run_self_test(emit):
    """打印/回传环境自检结果；emit 为输出函数（GUI 中接 on_log，CLI 中接 print）。"""

    def line(s):
        emit(s)

    line("==== 环境诊断 ====")
    # yt-dlp
    if os.path.isfile(YTDLP):
        try:
            r = subprocess.run([YTDLP, "--version"], capture_output=True, text=True,
                               timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            line(f"[yt-dlp]      ✅ {r.stdout.strip()}")
        except Exception as e:
            line(f"[yt-dlp]      ⚠ 存在但无法运行：{e}")
    else:
        p = shutil.which("yt-dlp")
        line(f"[yt-dlp]      {'✅ ' + p if p else '❌ 未在程序目录或 PATH 找到 yt-dlp.exe'}")
    # ffmpeg
    ff = find_ffmpeg()
    if ff:
        v = ffmpeg_version(ff)
        line(f"[ffmpeg]      ✅ {ff}（v{v}）")
    else:
        line("[ffmpeg]      ❌ 未找到，音视频合并将不可用")
    # node
    nd = find_node()
    if nd:
        try:
            r = subprocess.run([nd, "--version"], capture_output=True, text=True,
                               timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ver = r.stdout.strip()
            node_major = int(re.search(r"v(\d+)", ver).group(1)) if re.search(r"v(\d+)", ver) else 0
            note = "（支持 --allow-fs-read 权限模型）" if node_major >= 20 else "（<20，无权限模型，一般可直接解 n 挑战）"
            line(f"[node]        ✅ {nd} {ver} {note}")
        except Exception as e:
            line(f"[node]        ⚠ 存在但无法运行：{e}")
    else:
        line("[node]        ⚠ 未找到，YouTube 可能因无法解 n 签名挑战而失败")
    if NODE_SHIM_PRESENT:
        line(f"[NODE_OPTIONS] 检测到 WorkBuddy node-language-shim，已配置中和（{sanitize_node_options(os.environ.get('NODE_OPTIONS',''))}）")
    # cookies
    ck = find_cookies()
    if ck:
        level, why = cookies_login_report(ck)
        flag = "✅" if level == "ok" else "⚠"
        line(f"[cookies]     {flag} 自动加载 {ck}（{cookies_summary(ck)}）")
        line(f"[登录态]       {flag} {why}")
        if level != "ok":
            line("                → 用浏览器扩展导出 cookies.txt 放到程序目录，才能拿到完整登录态")
    else:
        line("[cookies]     ⚠ 未找到 cookies.txt（放程序根目录可自动启用），YouTube 可能被 bot 检测拦截")
    # Chrome cookie 加密体检（只读）
    try:
        diag = chrome_cookie_diagnostics()
        if diag["profiles"]:
            encs = {p["enc"] for p in diag["profiles"] if p["enc"]}
            login_p = [p["profile"] for p in diag["profiles"] if p["login"]]
            if chrome_abe_enabled():
                line(f"[Chrome 加密]  🔒 v20 应用绑定加密（{', '.join(sorted(encs))}）"
                     f"——--cookies-from-browser 必然失败，须用浏览器扩展导出 cookies.txt")
            else:
                line(f"[Chrome 加密]  ✅ 非 v20（{', '.join(sorted(encs)) or '未知'}），可由 yt-dlp 直接读取")
            line(f"[Chrome 配置档] " + "；".join(
                f"{p['profile']}({p['total']}条/YT {p['youtube']}{'/✅登录' if p['login'] else ''})"
                for p in diag["profiles"]))
            if login_p:
                line(f"                → YouTube 登录态在：{', '.join(login_p)}，"
                     f"请在该配置档里用扩展导出")
    except Exception as e:
        line(f"[Chrome 加密]  ⚠ 体检失败：{e}")
    # SOCKS
    sp = detect_socks_proxy()
    line(f"[SOCKS 代理]   {'✅ ' + sp + '（已验证可访问 YouTube）' if sp else 'ℹ 未检测到可用本机 SOCKS，YouTube 可能不可达'}")
    if sp:
        info = probe_exit_ip(sp)
        if info:
            line(f"[出口 IP]      {info['ip']} · {info.get('country')} {info.get('city')} · {info.get('isp')}")
            if is_datacenter_ip(info):
                line(f"               ⚠ 数据中心/云主机出口（{info.get('org')}）：YouTube 对这类 IP 风控"
                     f"极严，即使有登录态也常弹人机验证 → 建议切换到住宅/ISP 节点")
            else:
                line("               ✅ 非云机房出口，风控压力较小")
        else:
            line("[出口 IP]      ℹ 未能查询（代理不支持或 ip-api.com 不可达）")
    line("===================")


# ---------- UI 层 ----------
class DownloaderApp:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.engine = None
        self._bot_retried = False
        self._browser_fallback = False
        self._client_fallback = ""
        # 后台任务结果队列：工作线程只负责投递，主线程轮询取回，
        # 避免跨线程直接操作 Tk 控件（也不再阻塞 UI 导致「未响应」）
        self._task_q = queue.Queue()
        self.root.title("视频下载器  VideoDownloader")
        self.root.geometry("800x720")
        self.root.minsize(680, 600)
        self.build_ui()
        self.load_to_ui()
        self.root.after(100, self._poll_tasks)
        # 启动即在后台预热代理探测结果，后续点按钮无需再等
        self._warmup()

    # ---------------- 异步任务框架 ----------------
    def _poll_tasks(self):
        """主线程定时轮询后台任务结果（线程安全的 Tk 更新方式）。"""
        try:
            while True:
                cb, payload = self._task_q.get_nowait()
                try:
                    cb(payload)
                except Exception as e:
                    self.log_line(f"⚠ 后台任务回调异常：{e}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_tasks)

    def run_async(self, work, on_done, busy_text="处理中…"):
        """work() 在后台线程执行并返回结果；on_done(result) 回到主线程调用。

        work 内**禁止**读取 Tk 控件——需要的值请在调用前于主线程取好再闭包传入。
        """
        self.set_status(busy_text)

        def _w():
            try:
                res = work()
            except Exception as e:
                res = ("__error__", e)
            self._task_q.put((on_done, res))

        threading.Thread(target=_w, daemon=True).start()

    def _warmup(self):
        """后台预热：代理探测 + 出口 IP 查询，结果进缓存。"""
        def work():
            p = get_socks_proxy(force=True)
            return (p, get_exit_ip(p, force=True) if p else None)

        def done(res):
            if isinstance(res, tuple) and res and res[0] == "__error__":
                return
            p, info = res
            if p:
                self.log_line(f"🔌 启动预热：已发现本地 SOCKS 代理 {p}")
                if info and is_datacenter_ip(info):
                    self.log_line(f"   ⚠ 当前出口 {info.get('ip')} 属数据中心（{info.get('org')}），YouTube 风控极严")

        self.run_async(work, done, busy_text="就绪（后台探测网络中…）")

    # ---------------- UI ----------------
    def build_ui(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root)
        frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        ttk.Label(frm, text="视频链接（每行一个，支持批量）：").pack(anchor=tk.W, **pad)
        self.url_text = scrolledtext.ScrolledText(frm, height=6, wrap=tk.WORD)
        self.url_text.pack(fill=tk.BOTH, expand=False, **pad)

        mode_frame = ttk.LabelFrame(frm, text="下载模式")
        mode_frame.pack(fill=tk.X, **pad)
        self.mode_var = tk.StringVar()
        ttk.Radiobutton(mode_frame, text="视频（最佳画质）", variable=self.mode_var,
                        value="video_best", command=self.on_mode_change).grid(row=0, column=0, sticky=tk.W, padx=4)
        ttk.Radiobutton(mode_frame, text="视频（指定分辨率）", variable=self.mode_var,
                        value="video_res", command=self.on_mode_change).grid(row=0, column=1, sticky=tk.W, padx=4)
        ttk.Radiobutton(mode_frame, text="仅音频（MP3）", variable=self.mode_var,
                        value="audio", command=self.on_mode_change).grid(row=0, column=2, sticky=tk.W, padx=4)

        res_frame = ttk.Frame(mode_frame)
        res_frame.grid(row=1, column=0, columnspan=3, sticky=tk.W, padx=4, pady=2)
        ttk.Label(res_frame, text="分辨率：").pack(side=tk.LEFT)
        self.res_var = tk.StringVar()
        self.res_combo = ttk.Combobox(res_frame, textvariable=self.res_var,
                                      values=["2160", "1440", "1080", "720", "480", "360"],
                                      width=8, state="readonly")
        self.res_combo.pack(side=tk.LEFT)

        out_frame = ttk.Frame(frm)
        out_frame.pack(fill=tk.X, **pad)
        ttk.Label(out_frame, text="保存位置：").pack(side=tk.LEFT)
        self.out_var = tk.StringVar()
        ttk.Entry(out_frame, textvariable=self.out_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(out_frame, text="浏览", command=self.choose_dir).pack(side=tk.LEFT, padx=4)

        proxy_frame = ttk.Frame(frm)
        proxy_frame.pack(fill=tk.X, **pad)
        ttk.Label(proxy_frame, text="SOCKS/HTTP 代理：").pack(side=tk.LEFT)
        self.proxy_var = tk.StringVar()
        self.proxy_var.trace_add("write", self._on_proxy_edited)
        ttk.Entry(proxy_frame, textvariable=self.proxy_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(proxy_frame, text="留空=自动探测本机 SOCKS（YouTube 必需）").pack(side=tk.LEFT, padx=4)

        node_frame = ttk.Frame(frm)
        node_frame.pack(fill=tk.X, **pad)
        ttk.Label(node_frame, text="Node.js（可选）：").pack(side=tk.LEFT)
        self.node_var = tk.StringVar()
        ttk.Entry(node_frame, textvariable=self.node_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(node_frame, text="浏览", command=self.choose_node).pack(side=tk.LEFT, padx=4)

        ck_frame = ttk.LabelFrame(frm, text="登录态（抖音等需登录的视频 / YouTube 反爬）")
        ck_frame.pack(fill=tk.X, **pad)
        self.use_browser_ck = tk.BooleanVar()
        ttk.Checkbutton(ck_frame, text="使用浏览器已登录的 cookies：",
                        variable=self.use_browser_ck, command=self.on_ck_change).grid(row=0, column=0, sticky=tk.W)
        self.browser_var = tk.StringVar()
        self.browser_combo = ttk.Combobox(ck_frame, textvariable=self.browser_var,
                                          values=["chrome", "edge", "firefox", "brave"],
                                          width=10, state="readonly")
        self.browser_combo.grid(row=0, column=1, padx=4)
        ttk.Button(ck_frame, text="选择 cookies.txt", command=self.choose_cookies).grid(row=0, column=2, padx=4)
        ttk.Button(ck_frame, text="刷新 cookies", command=self.refresh_cookies).grid(row=0, column=3, padx=4)
        self.cookies_file_var = tk.StringVar()
        ttk.Entry(ck_frame, textvariable=self.cookies_file_var).grid(row=1, column=0, columnspan=3, sticky=tk.EW, padx=4, pady=2)
        # PO Token：YouTube 的 GVS 播放凭证，缺它时 HLS(m3u8) 分片会整条 403
        self.po_token_var = tk.StringVar()
        ttk.Label(ck_frame, text="PO Token（可选，格式 web+xxxxx）：").grid(
            row=2, column=0, sticky=tk.W, padx=4, pady=2)
        ttk.Entry(ck_frame, textvariable=self.po_token_var).grid(
            row=2, column=1, columnspan=2, sticky=tk.EW, padx=4, pady=2)
        ttk.Label(ck_frame, text="缺它时 HLS 分片会 403；留空则用 DASH 优先策略").grid(
            row=3, column=0, columnspan=3, sticky=tk.W, padx=4)
        ck_frame.columnconfigure(0, weight=1)

        opt_frame = ttk.Frame(frm)
        opt_frame.pack(fill=tk.X, **pad)
        self.playlist_var = tk.BooleanVar()
        ttk.Checkbutton(opt_frame, text="包含整个播放列表（默认仅当前视频）",
                        variable=self.playlist_var).pack(side=tk.LEFT)

        btn_frame = ttk.Frame(frm)
        btn_frame.pack(fill=tk.X, **pad)
        self.start_btn = ttk.Button(btn_frame, text="开始下载", command=self.start)
        self.start_btn.pack(side=tk.LEFT, padx=4)
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self.stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=4)
        self.diag_btn = ttk.Button(btn_frame, text="环境诊断", command=self.on_diag)
        self.diag_btn.pack(side=tk.LEFT, padx=4)
        self.open_btn = ttk.Button(btn_frame, text="打开保存文件夹", command=self.open_folder)
        self.open_btn.pack(side=tk.LEFT, padx=4)

        self.progress = ttk.Progressbar(frm, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X, **pad)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(frm, textvariable=self.status_var).pack(anchor=tk.W, **pad)

        ttk.Label(frm, text="日志：").pack(anchor=tk.W, **pad)
        self.log = scrolledtext.ScrolledText(frm, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self.log.pack(fill=tk.BOTH, expand=True, **pad)

    def _on_proxy_edited(self, *args):
        """只有用户手动改过代理框才把它持久化；否则每次启动都重新自动探测，
        避免「某次探测结果被写死，VPN 换端口后失效」。"""
        if getattr(self, "_loading", False):
            return
        self._proxy_manual = True

    def load_to_ui(self):
        s = self.settings
        self._loading = True
        self.mode_var.set(s["mode"])
        self.res_var.set(s["resolution"])
        self.out_var.set(s["output_dir"])
        self._proxy_manual = bool(s.get("proxy_manual", False))
        # 未手动填写过的代理不回填（否则旧的探测结果会被当成用户输入长期固化）
        self.proxy_var.set(s["proxy"] if self._proxy_manual else "")
        self.use_browser_ck.set(s["use_browser_cookies"])
        self.browser_var.set(s["browser"])
        self.cookies_file_var.set(s["cookies_file"])
        self.node_var.set(s.get("node_path", ""))
        self.playlist_var.set(s.get("include_playlist", False))
        self.po_token_var.set(s.get("po_token", ""))
        # 用户未手动指定时，自动接管程序根目录下的 cookies.txt（此前正是缺失这一步）
        if not s["cookies_file"] and not s["use_browser_cookies"]:
            auto_ck = find_cookies()
            if auto_ck:
                self.cookies_file_var.set(auto_ck)
                level, why = cookies_login_report(auto_ck)
                self.log_line(f"🍪 自动加载同目录 cookies：{auto_ck}（{cookies_summary(auto_ck)}）")
                self.log_line(f"   登录态：{'✅' if level == 'ok' else '⚠'} {why}")
        self.on_mode_change()
        self.on_ck_change()
        self._loading = False

    def on_mode_change(self):
        self.res_combo.configure(state="disabled" if self.mode_var.get() == "audio" else "readonly")

    def on_ck_change(self):
        # 两种登录态来源互斥：勾选「浏览器 cookies」时才清空文件选择。
        # 注意：此前写成「未勾选 → 清空」，导致启动时自动加载的 cookies.txt 被立刻抹掉。
        state = "readonly" if self.use_browser_ck.get() else "disabled"
        self.browser_combo.configure(state=state)
        if self.use_browser_ck.get():
            self.cookies_file_var.set("")

    def choose_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.out_var.set(d)

    def choose_node(self):
        f = filedialog.askopenfilename(filetypes=[("Node", "node.exe"), ("All", "*.*")])
        if f:
            self.node_var.set(f)

    def choose_cookies(self):
        f = filedialog.askopenfilename(filetypes=[("Cookies", "*.txt"), ("All", "*.*")])
        if f:
            self.cookies_file_var.set(f)
            self.use_browser_ck.set(False)
            self.on_ck_change()
            level, why = cookies_login_report(f)
            self.log_line(f"🍪 cookies 状态：{'✅' if level == 'ok' else '⚠'} {why}")

    def refresh_cookies(self):
        """获取 cookies：先只读体检，能自动提取才提取，否则给出精确的手动导出指引。

        安全红线：绝不启动用户的 Chrome 进程去读 cookie。实测表明，用非常规路径
        （目录联接 / 复制配置档）启动 Chrome 读取真实配置档，会导致其无法解密 v20
        应用绑定加密 cookie，进而把整个 cookie 表清空——这是不可逆的数据损失。
        """
        browser = self.browser_var.get() or "chrome"

        def work():
            diag = chrome_cookie_diagnostics()
            login_profiles = [p["profile"] for p in diag["profiles"]
                              if p["login"] and p["youtube"] > 0]
            encs = {p["enc"] for p in diag["profiles"] if p["enc"]}
            abe = diag["abe"] or "v20" in encs
            return ("__diag__", diag, login_profiles, abe, browser)

        def done(res):
            if isinstance(res, tuple) and res and res[0] == "__error__":
                self.log_line(f"❌ 体检失败：{res[1]}")
                return
            _, diag, login_profiles, abe, browser = res
            for p in diag["profiles"]:
                mark = "✅含登录态" if p["login"] else "—"
                self.log_line(f"   [{p['profile']}] 共 {p['total']} 条 / YouTube {p['youtube']} 条 "
                              f"/ 加密={p['enc'] or '未知'} {mark}")
            if not diag["profiles"]:
                self.log_line("ℹ 未扫描到 Chrome 配置档。请改用「选择 cookies.txt」手动导入。")
                self.set_status("就绪")
                return
            if abe:
                self.log_line("🔒 检测到 Chrome 使用 v20 应用绑定加密 cookie：")
                self.log_line("   yt-dlp 的 --cookies-from-browser 无法解密（必然报 "
                              "Failed to decrypt with DPAPI），已跳过自动提取。")
                if login_profiles:
                    self.log_line(f"   ✅ 你的 YouTube 登录态在配置档：{', '.join(login_profiles)}")
                self.log_line("   请用浏览器扩展导出（推荐 Get cookies.txt LOCALLY）：")
                self.log_line("   1) 在 Chrome 中打开 youtube.com 并确认已登录；")
                if login_profiles:
                    self.log_line(f"   2) 确认右上角头像是配置档「{login_profiles[0]}」的账号；")
                self.log_line("   3) 点扩展图标 → Export / 导出 → 保存为 cookies.txt；")
                self.log_line(f"   4) 把文件放到 {COOKIES_FILE_DEFAULT}，或点「选择 cookies.txt」导入。")
                self.log_line("   ⚠ 切勿用「关闭浏览器再让程序直连数据库」的土办法："
                              "本程序不会启动你的浏览器读 cookie，其它工具这样做有清空 cookie 的风险。")
                self.set_status("就绪")
                return
            if browser_process_running(browser):
                self.log_line(f"❌ {browser} 仍在运行，cookie 数据库被锁。请完全退出后再点「刷新 cookies」。")
                self.set_status("就绪")
                return
            self._extract_with_ytdlp(browser)

        self.run_async(work, done, busy_text="正在体检 Chrome cookie…")

    def _extract_with_ytdlp(self, browser):
        """仅在确认浏览器已退出、且 cookie 非 v20 加密时，才用 yt-dlp 提取。"""
        raw = [u.strip() for u in self.url_text.get("1.0", tk.END).splitlines() if u.strip()]
        url = normalize_url(raw[0]) if raw else "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        typed_proxy = self.proxy_var.get().strip()
        node = self.node_var.get().strip() or NODE_PATH
        tmp = os.path.join(tempfile.mkdtemp(prefix="vdrefresh_"), "cookies.txt")
        self.log_line(f"🔄 准备从 {browser} 提取 cookies…")

        def work():
            proxy = typed_proxy or get_socks_proxy() or ""
            cmd = [YTDLP, "--no-warnings", "--newline", "--simulate", "--no-playlist",
                   "--cookies-from-browser", browser, "--cookies", tmp, "--proxy", proxy, url]
            if YTDLP_OUTPUT_ENCODING and ytdlp_supports_encoding(YTDLP):
                cmd += ["--encoding", YTDLP_OUTPUT_ENCODING]
            if node:
                cmd += ["--js-runtimes", f"node:{node}"]
            env = build_node_env(os.environ)
            # 按字节捕获再回退解码：yt-dlp 管道输出在 Windows 下可能是 GBK
            r = subprocess.run(cmd, capture_output=True, env=env, timeout=180,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = decode_stream_line(r.stdout or b"") + decode_stream_line(r.stderr or b"")
            return (" ".join(cmd), text)

        def done(res):
            if isinstance(res, tuple) and res and res[0] == "__error__":
                self.log_line(f"❌ 提取失败：{res[1]}")
                return
            cmdline, out = res
            self.log_line("▶ 命令：" + cmdline)
            for ln in out.splitlines()[-15:]:
                self.log_line(ln)
            if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                level, why = cookies_login_report(tmp)
                if level == "ok":
                    try:
                        shutil.copy2(tmp, COOKIES_FILE_DEFAULT)
                        self.cookies_file_var.set(COOKIES_FILE_DEFAULT)
                        self.use_browser_ck.set(False)
                        self.on_ck_change()
                        self.log_line(f"✅ 已刷新并保存 cookies：{COOKIES_FILE_DEFAULT}（{why}）")
                    except Exception as e:
                        self.log_line(f"⚠ 保存失败：{e}")
                else:
                    self.log_line(f"⚠ 提取到的 cookies 仍无有效登录态（{why}）。请确认浏览器已登录 YouTube。")
            else:
                low = out.lower()
                if "could not copy" in low or "cookie database" in low:
                    self.log_line("❌ 浏览器 cookie 数据库被占用：请完全关闭 Chrome/Edge（含后台进程）后重试。")
                else:
                    self.log_line("❌ 未能提取 cookies，详见上方输出。")
            self.set_status("就绪")

        self.run_async(work, done, busy_text="正在从浏览器提取 cookies…")

    def open_folder(self):
        d = self.out_var.get()
        if d and os.path.isdir(d):
            os.startfile(d)

    # ---------------- 运行 ----------------
    def log_line(self, text):
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def set_status(self, text):
        self.status_var.set(text)

    def set_progress(self, val):
        self.progress["value"] = val

    def on_diag(self):
        """环境诊断放到后台线程：涉及版本查询、端口扫描、出口 IP 查询，主线程跑会卡死界面。"""
        self.log_line("🔍 运行环境诊断…（后台执行，界面保持响应）")

        def work():
            buf = []
            run_self_test(buf.append)
            return buf

        def done(res):
            if isinstance(res, tuple) and res and res[0] == "__error__":
                self.log_line(f"❌ 诊断失败：{res[1]}")
                return
            for ln in res:
                self.log_line(ln)
            self.set_status("诊断完成")

        self.run_async(work, done, busy_text="环境诊断中…")

    def start(self, is_retry: bool = False):
        if self.engine and self.engine.running:
            return
        if getattr(self, "_probing", False):
            return
        if not is_retry:
            self._bot_retried = False
            self._browser_fallback = False
            self._client_fallback = ""
            self._hls403_retried = False
            # 记住用户本意：bot 兜底会自动勾选浏览器 cookies，这个临时改动不应被持久化
            self._user_browser_ck = self.use_browser_ck.get()

        # 二进制自检
        if not os.path.exists(YTDLP):
            messagebox.showerror("缺少 yt-dlp.exe",
                                 f"未在同目录找到 yt-dlp.exe：\n{YTDLP}\n请将 yt-dlp.exe 放在本程序所在文件夹。")
            return
        ff = find_ffmpeg()
        if not ff:
            messagebox.showwarning("缺少 ffmpeg",
                                   "未找到 ffmpeg.exe（同目录或 PATH 均无）。音频提取与音视频合并将不可用，"
                                   "建议把 ffmpeg.exe 放到本程序所在文件夹。")
        if not NODE_PATH and not self.node_var.get().strip():
            self.log_line("⚠ 未找到 Node.js：YouTube 可能因无法解开签名挑战而失败，建议安装 Node.js 或将 node.exe 放到本程序目录。")

        raw_urls = [u.strip() for u in self.url_text.get("1.0", tk.END).splitlines() if u.strip()]
        if not raw_urls:
            messagebox.showwarning("提示", "请先粘贴至少一个视频链接。")
            return

        # 归一化链接（含抖音各种形态自动转 video/<ID>）
        urls = []
        for u in raw_urls:
            nu = normalize_url(u)
            if nu != u:
                self.log_line(f"🔧 链接归一化：{u}\n    → {nu}")
            urls.append(nu)

        out = self.out_var.get().strip() or DEFAULT_OUTPUT
        self.out_var.set(out)
        os.makedirs(out, exist_ok=True)

        # 网络探测（SOCKS 端口扫描 + 出口 IP 查询）一律放后台线程：
        # 此前在主线程串行扫 8 个端口（最坏 12 秒）不处理消息，会被系统标记「未响应」。
        typed_proxy = self.proxy_var.get().strip()
        self._probing = True
        self._pending = {"urls": urls, "out": out, "ff": ff}
        if typed_proxy:
            self._begin_download((typed_proxy, None, False))
        else:
            self.log_line("🔌 未指定代理，后台探测本机可用的 SOCKS 代理…")

            def work():
                p = get_socks_proxy()
                return (p or "", get_exit_ip(p) if p else None, True)

            self.run_async(work, self._begin_download, busy_text="正在探测 SOCKS 代理…")
        return

    def _begin_download(self, res):
        """网络探测结束后回到主线程，组装并启动下载。"""
        self._probing = False
        if isinstance(res, tuple) and res and res[0] == "__error__":
            self.log_line(f"❌ 网络探测失败：{res[1]}")
            self.set_status("就绪")
            return
        proxy, info, auto_proxy = res
        urls = self._pending["urls"]
        out = self._pending["out"]
        ff = self._pending["ff"]

        if auto_proxy:
            if proxy:
                self.log_line(f"✅ 自动选用本地 SOCKS 代理：{proxy}（已验证可访问 YouTube）")
                if info:
                    self.log_line(f"🌐 出口 IP：{info['ip']}（{info.get('country')} {info.get('city')} · {info.get('isp')}）")
                    if is_datacenter_ip(info):
                        self.log_line(f"   ⚠ 数据中心/云主机出口（{info.get('org')}）：YouTube 对这类 IP 风控极严，"
                                      f"建议换成住宅/ISP 节点，否则极易人机验证。")
            else:
                self.log_line("ℹ 未检测到可用的本地 SOCKS 代理，将尝试直连（若 YouTube 不可达可能失败）。")

        # cookies：显式选择 > 程序根目录自动探测 > （被拦后）浏览器实时 cookie
        cookies_file = ""
        use_browser = self.use_browser_ck.get()

        # Chrome v20 应用绑定加密 → --cookies-from-browser 必然 decrypt 失败。
        # 直接禁用该来源，避免每轮都白跑一次下载。
        if use_browser and (self.browser_var.get() or "chrome").lower() == "chrome" \
                and chrome_abe_enabled():
            self.log_line("🔒 Chrome cookie 为 v20 应用绑定加密，--cookies-from-browser 必然失败"
                          "（Failed to decrypt with DPAPI）；已自动改用 cookies 文件。")
            use_browser = False
            self.use_browser_ck.set(False)
            self.on_ck_change()

        # 浏览器没关 → --cookies-from-browser 必定失败。与其白跑一轮，
        # 不如提前拦截：有 cookies.txt 就自动改用文件，没有则明确告知原因。
        if use_browser and browser_process_running(self.browser_var.get()):
            self.log_line(f"⚠ 检测到 {self.browser_var.get()} 仍在运行：其 cookie 数据库被占用，"
                          f"「浏览器 cookies」本轮必定失败。")
            alt = self.cookies_file_var.get().strip() or find_cookies()
            if alt:
                self.log_line(f"   → 自动改用可用的 cookies 文件：{alt}")
                use_browser = False
                self.use_browser_ck.set(False)
                self.on_ck_change()
                self.cookies_file_var.set(alt)
            else:
                self.log_line("   → 无 cookies.txt 可回退。请完全关闭浏览器后点「刷新 cookies」再下载。")

        # _browser_fallback 只影响日志文案，**不参与分支选择**：
        # 之前写成互斥 if/elif/elif，一旦 _browser_fallback=True 且 use_browser=False，
        # 就会命中「继续沿用 cookies 文件」这一支并直接跳过下面真正给 cookies_file 赋值的
        # `elif not use_browser`，导致兜底轮次反而完全没有 cookies（比第一轮更差）。
        if getattr(self, "_browser_fallback", False) and use_browser:
            # 上一轮被人机验证拦下，本轮改用浏览器实时 cookie（浏览器需完全关闭）
            self.log_line("🔁 上轮触发人机验证，本轮改用浏览器实时 cookies。")
        else:
            if getattr(self, "_browser_fallback", False):
                self.log_line("🔁 浏览器实时 cookies 不可用，本轮继续沿用 cookies 文件。")
            # 只要不用浏览器 cookies，就一定执行文件 cookies 的赋值与登录态报告
            if not use_browser:
                cookies_file = self.cookies_file_var.get().strip()
                if not cookies_file:
                    cookies_file = find_cookies() or ""
                    if cookies_file:
                        self.cookies_file_var.set(cookies_file)
                        self.log_line(f"🍪 自动启用同目录 cookies：{cookies_file}（{cookies_summary(cookies_file)}）")
                if cookies_file:
                    level, why = cookies_login_report(cookies_file)
                    if level == "ok":
                        self.log_line(f"🍪 cookies 状态：✅ {why}")
                    elif level == "weak":
                        self.log_line(f"🍪 cookies 状态：⚠ {why}")
                        self.log_line("   → 建议关闭 Chrome/Edge 后点「刷新 cookies」，或直接勾选「使用浏览器 cookies」。")
                    else:
                        self.log_line(f"🍪 cookies 状态：⚠ {why}")
        if not cookies_file and not use_browser:
            self.log_line("⚠ 未提供任何登录态（无 cookies.txt、未勾选浏览器 cookies）——YouTube 大概率触发人机验证。")

        # 关键：传给 yt-dlp 的是临时副本，避免其回写破坏原始 cookies.txt
        cookies_for_run = prepare_cookies_temp(cookies_file) or ""

        node_override = self.node_var.get().strip() or NODE_PATH
        opts = DownloadOptions(
            ytdlp_path=YTDLP,
            ffmpeg_path=ff,
            node_path=node_override,
            mode=self.mode_var.get(),
            resolution=self.res_var.get(),
            output_dir=out,
            proxy=proxy,
            use_browser_cookies=use_browser,
            browser=self.browser_var.get(),
            cookies_file=cookies_for_run,
            include_playlist=self.playlist_var.get(),
            player_clients=getattr(self, "_client_fallback", ""),
            po_token=self.po_token_var.get().strip(),
        )

        # 持久化用户设置。
        # 注意：自动探测出的代理不写回设置，否则下次启动被固定住、VPN 换端口后就失效了。
        self.settings.update({
            "output_dir": out,
            "mode": opts.mode,
            "resolution": opts.resolution,
            "proxy": opts.proxy if getattr(self, "_proxy_manual", False) else "",
            "proxy_manual": getattr(self, "_proxy_manual", False),
            "use_browser_cookies": getattr(self, "_user_browser_ck", opts.use_browser_cookies),
            "browser": opts.browser,
            "cookies_file": cookies_file if self.cookies_file_var.get().strip() else "",
            "node_path": self.node_var.get().strip(),
            "include_playlist": opts.include_playlist,
            "po_token": self.po_token_var.get().strip(),
        })
        save_settings(self.settings)

        cmd = build_command(opts, urls)
        self.engine = DownloadEngine(self.log_line, self.set_progress, self.set_status, self.on_finish)
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.log_line("▶ 命令：" + " ".join(cmd))
        if node_override:
            self.log_line(f"🔧 已定位 Node.js：{node_override}（用于解开 YouTube 签名挑战）")
        self.set_status(f"下载中…（共 {len(urls)} 个任务）")
        self.set_progress(0)
        self.engine.start(cmd)

    def on_finish(self, rc, had_error, saw_bot=False):
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

        # 最高优先级：本轮是我们自己因「分片连续 403」中止的。
        # 这不是人机验证，走下面的 bot 兜底链没有意义（只会再磨一轮 403）。
        abort = getattr(self.engine, "abort_reason", "")
        if abort == "hls403":
            if not getattr(self, "_hls403_retried", False):
                self._hls403_retried = True
                self._client_fallback = YT_FALLBACK_PLAYER_CLIENTS
                self.log_line("🔁 分片 403：自动扩展 player_client 列表重试一次"
                              "（先争取拿到 DASH 直链，而不是 HLS 分片）。")
                self.root.after(300, lambda: self.start(is_retry=True))
                return
            self._report_hls403()
            self.set_status("已中止：HLS 分片 403")
            return

        # saw_bot 必须一并计入：_reader 里 LOGIN_REQUIRED_RE 先于 ERROR_RE 命中（elif 链），
        # 所以 bot 行只置 saw_bot、不置 had_error。若某些 yt-dlp 版本在 bot 拦截下仍返回 rc=0，
        # 只看 rc/had_error 会把整条兜底链当成「全部任务完成」静默跳过。
        if rc == 0 and not had_error and not saw_bot:
            self.set_status("完成 ✓")
            self.set_progress(100)
            self.log_line("✔ 全部任务完成。")
            return

        # 兜底链第一级：人机验证 → 扩展 player_client 列表重试
        # yt-dlp 默认只试 tv_samsung/visionos/web；实测显式列出完整客户端列表时，
        # 有机会从 mweb / tv / web_embedded 等拿到可用数据而绕过 bot 校验。
        # 这一步无副作用（不碰浏览器、不改 cookies），优先于换登录态来源。
        if saw_bot and not getattr(self, "_client_fallback", ""):
            self._client_fallback = YT_FALLBACK_PLAYER_CLIENTS
            self.log_line("🔁 触发人机验证：自动扩展 player_client 列表重试一次"
                          "（默认只试 tv_samsung/visionos/web）。")
            self.root.after(300, lambda: self.start(is_retry=True))
            return

        # 兜底链第二级：人机验证 → 换登录态来源重试
        # 若 Chrome 是 v20 应用绑定加密，浏览器 cookies 这条路已证实不通，不必重试。
        if saw_bot and not getattr(self, "_bot_retried", False) and not chrome_abe_enabled():
            self._bot_retried = True
            self._browser_fallback = True
            self.log_line("🔁 触发人机验证：自动换用浏览器实时 cookies 重试一次。")
            self.log_line("   （若提示 cookie 数据库被锁，请先完全关闭 Chrome/Edge 再重试）")
            self.root.after(300, lambda: self.start(is_retry=True))
            return

        if saw_bot:
            # 按实际探测结果给出针对性结论，而不是罗列泛泛建议
            info = _SOCKS_CACHE.get("info")
            ck = self.cookies_file_var.get().strip() or find_cookies()
            lvl = cookies_login_report(ck)[0] if ck else "none"
            self.log_line("❌ 人机验证仍未通过（已自动尝试 player_client 轮换）。"
                          "按当前探测，阻塞点如下（按影响排序）：")
            n = 0
            if info and is_datacenter_ip(info):
                n += 1
                self.log_line(f"   {n}) 出口 IP {info.get('ip')} 属数据中心（{info.get('org')}）——"
                              f"这是最关键的一项，建议换住宅/ISP VPN 节点。")
            if lvl != "ok":
                n += 1
                self.log_line(f"   {n}) cookies 缺少第一方登录态（{lvl}）——完全关闭 Chrome/Edge 后点「刷新 cookies」。")
            if not self.po_token_var.get().strip():
                n += 1
                self.log_line(f"   {n}) 未填 PO Token——拿到 token 后填进上方「PO Token」框"
                              f"（格式 web+xxxxx），可显著提升通过率。")
            n += 1
            self.log_line(f"   {n}) 若上面都排除了，确认浏览器里已登录 YouTube，并降低请求频率。")
        self.set_status(f"结束（返回码 {rc}）")
        self.log_line(f"⚠ 进程结束，返回码 {rc}。若中途失败，可查看上方日志或点「环境诊断」。")

    def _report_hls403(self):
        """HLS 分片 403 的定向诊断：按当前探测到的实际阻塞点给出可执行建议。"""
        self.log_line("❌ 下载中止：YouTube 对 HLS(m3u8) 分片返回 HTTP 403，重试无效。")
        self.log_line("   原因：当前会话缺少有效的 PO Token（YouTube 的 GVS 播放凭证）。"
                      "在「数据中心出口 IP + 无第一方登录 cookie」的组合下，"
                      "YouTube 只肯给 HLS 流，而 HLS 分片没有凭证就必然 403。")
        info = _SOCKS_CACHE.get("info")
        ck = self.cookies_file_var.get().strip() or find_cookies()
        n = 0
        if not self.po_token_var.get().strip():
            n += 1
            self.log_line(f"   {n}) 【最有效】在上方的「PO Token」框填入 token"
                          f"（形如 web+xxxxx），程序会自动注入 "
                          f"--extractor-args youtube:po_token=... 后重试。")
        if info and is_datacenter_ip(info):
            n += 1
            self.log_line(f"   {n}) 当前出口 {info.get('ip')} 属数据中心（{info.get('org')}），"
                          f"风控最严：建议换成住宅/ISP 的代理节点。")
        if ck and cookies_login_report(ck)[0] != "ok":
            n += 1
            self.log_line(f"   {n}) cookies 缺少第一方登录态（SID/HSID/SSID/…）："
                          f"完全关闭 Chrome/Edge 后点「刷新 cookies」重新导出。")
        n += 1
        self.log_line(f"   {n}) 若只想先拿到视频：把下载模式改成较低分辨率（如 720）再试，"
                      f"有时能拿到另一组直链。")

    def stop(self):
        if self.engine:
            self.engine.stop()
            self.log_line("■ 已发送停止信号。")


def main():
    # 无界面自检模式：VideoDownloader.exe --selftest
    if "--selftest" in sys.argv:
        # 打包为 --windowed 时，stdout 若被重定向到文件/管道，默认编码是本机 ANSI(GBK)，
        # 而自检输出含 ✅/⚠/❌ 等字符，直接 print 会抛 UnicodeEncodeError 直接崩掉。
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        run_self_test(print)
        return
    root = tk.Tk()
    DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
