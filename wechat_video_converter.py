#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""微信视频格式转换器 WeChatVideoConverter（独立外挂小工具）。

把任意常见视频一键转换为「微信可直接播放」的 MP4：
    * 容器  : MP4
    * 视频  : H.264（不是 H.265/HEVC，微信 8.0 以下完全无法识别 H.265）
    * 音频  : AAC
    * 像素格式: yuv420p（必须显式指定；yuv422p 会导致 iOS 黑屏 / 安卓花屏）
    * faststart: moov 前置，实现边下边播

转换策略为「智能判断」：
    * 源已是 H.264 + AAC（且像素格式兼容）=> 只重封装（remux），秒级完成、画质零损失；
    * 否则（H.265 / VP9 / AV1 / MPEG-4 / 音轨非 AAC / 像素格式非 yuv420p）=> 真正转码；
    * 若开启了体积上限且源体积超限 => 即使是 H.264+AAC 也必须走转码重压路径。

技术要点（本项目踩过的坑）：
    1. ffprobe.exe 可能不存在：优先用 ffprobe，缺失时回退解析 `ffmpeg -i` 的 stderr；
    2. Windows 子进程管道输出可能是 GBK(ANSI)：统一按字节读取，再 UTF-8 -> ANSI 回退解码；
    3. 长耗时任务一律放后台线程，通过 queue.Queue + root.after 轮询回主线程更新 UI（绝不阻塞 Tk）；
    4. 输出文件名为 <原名>_wechat.mp4，永不覆盖输入文件，目标文件已存在时自动改名。

作者：Software Development Team / Engineer
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

APP_TITLE = "微信视频格式转换器  WeChatVideoConverter"
APP_GEOMETRY = "860x720"
APP_MINSIZE = (720, 620)

# 微信私聊单条上限 1GB；朋友圈单条时长上限 30 秒
WECHAT_CHAT_LIMIT_MB = 1024
WECHAT_MOMENTS_LIMIT_SEC = 30

# 目标视频码率计算时的安全系数（给音频码率波动、容器开销、faststart 预留余量）
SIZE_SAFETY_FACTOR = 0.95
# 1MB -> kbps 的换算系数（1MB = 1024KiB，1024*8 = 8192）
MB_TO_KBPS = 8192

DEFAULT_AUDIO_KBPS = 128
LOW_AUDIO_KBPS = 64
# 极小目标时的音频下限（语音级音质，5 秒约 15KB）
MIN_AUDIO_KBPS = 24
MIN_VIDEO_KBPS = 120
# 用户明确设定目标大小时的视频码率硬下限：
# 目标优先于画质保底（宁可糊也要发得出去），但低于 16kbps 画面基本不可编码
HARD_MIN_VIDEO_KBPS = 16
MAX_VIDEO_KBPS = 40000

# 超过目标大小时的自适应重试次数
MAX_SIZE_RETRY = 2

# 被视为「微信可直接播放」的视频 / 音频编码
WECHAT_VIDEO_CODECS = {"h264", "avc"}
WECHAT_AUDIO_CODECS = {"aac"}
# 微信可安全播放的像素格式（yuvj420p 为旧版 full-range，MP4 中会被当作 yuv420p 处理）
WECHAT_PIX_FMTS = {"yuv420p", "yuvj420p", "nv12"}

CREATE_NO_WINDOW = 0x08000000

VIDEO_EXT_FILTER = [
    ("视频文件", "*.mp4 *.mov *.mkv *.avi *.flv *.wmv *.webm *.m4v *.ts *.m2ts *.3gp *.mpg *.mpeg *.rmvb *.vob"),
    ("所有文件", "*.*"),
]

# --------------------------------------------------------------------------- #
# 子进程输出解码：Windows 下管道可能是 GBK(ANSI)，必须字节读取后回退解码
# --------------------------------------------------------------------------- #

_DECODE_CHAIN = ("utf-8", "mbcs", "gbk", "cp936", "latin-1")


def decode_bytes(data: bytes) -> str:
    """把子进程输出的原始字节解码为 str。

    先按 UTF-8 尝试；失败则回退本机 ANSI 代码页(mbcs) / GBK，最后 latin-1 兜底。
    绝不直接抛异常，保证中文文件名在日志中不变成 U+FFFD 或直接崩溃。
    """
    if not data:
        return ""
    for enc in _DECODE_CHAIN:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _startupinfo() -> Optional[subprocess.STARTUPINFO]:
    """构造隐藏控制台窗口的 STARTUPINFO（仅 Windows 有效）。"""
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def run_capture(cmd: Sequence[str], timeout: int = 30) -> Tuple[int, str, str]:
    """同步执行一条命令并收集 stdout/stderr（字节读取 + 回退解码）。"""
    proc = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        startupinfo=_startupinfo(),
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        out_b, err_b = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out_b, err_b = proc.communicate()
    return proc.returncode, decode_bytes(out_b), decode_bytes(err_b)


# --------------------------------------------------------------------------- #
# 工具定位
# --------------------------------------------------------------------------- #

def app_dir() -> str:
    """返回本程序所在目录（兼容 PyInstaller --onefile 冻结后的 sys.executable）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _candidate_dirs() -> List[str]:
    dirs: List[str] = []
    base = app_dir()
    dirs.append(base)
    # PyInstaller 解包临时目录（若日后把 ffmpeg 一并打包）
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(meipass)
    cwd = os.getcwd()
    if cwd not in dirs:
        dirs.append(cwd)
    return dirs


def locate_tool(name: str) -> Optional[str]:
    """在程序目录 / MEIPASS / 当前目录 / PATH 中查找可执行文件。"""
    exe_name = name + (".exe" if os.name == "nt" else "")
    for d in _candidate_dirs():
        p = os.path.join(d, exe_name)
        if os.path.isfile(p):
            return p
    which = shutil.which(exe_name)
    if which:
        return which
    return None


class ToolBox:
    """ffmpeg / ffprobe 定位与能力探测（结果缓存）。"""

    def __init__(self) -> None:
        self.ffmpeg: Optional[str] = locate_tool("ffmpeg")
        self.ffprobe: Optional[str] = locate_tool("ffprobe")
        self._utf8_opt: Optional[bool] = None

    @property
    def has_ffmpeg(self) -> bool:
        return bool(self.ffmpeg)

    def supports_utf8_option(self) -> bool:
        """探测 ffmpeg 是否支持 `-encoding UTF-8`（该选项在部分版本标注 experimental）。

        用 `ffmpeg -h full` 的选项清单做一次静态探测（约 0.4s，结果缓存）。
        即便探测为 True，真正转码时仍带「失败自动去掉该选项重试」的兜底。
        """
        if self._utf8_opt is not None:
            return self._utf8_opt
        if not self.ffmpeg:
            self._utf8_opt = False
            return False
        try:
            rc, out, err = run_capture([self.ffmpeg, "-hide_banner", "-h", "full"], timeout=60)
            text = out + err
            self._utf8_opt = bool(re.search(r"^\s*-encoding\s", text, re.M))
        except Exception:
            self._utf8_opt = False
        return self._utf8_opt

    def disable_utf8_option(self) -> None:
        """运行时发现 ffmpeg 不接受 `-encoding` 后，关闭该能力缓存。"""
        self._utf8_opt = False


# --------------------------------------------------------------------------- #
# 媒体信息探测
# --------------------------------------------------------------------------- #

@dataclass
class MediaStream:
    """一条媒体流的关键信息。"""

    index: int = -1
    codec_type: str = ""          # video / audio / subtitle / data
    codec_name: str = ""          # h264 / hevc / aac ...
    pix_fmt: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    channels: int = 0
    bit_rate: int = 0             # bps
    sample_rate: int = 0


@dataclass
class MediaInfo:
    """一次探测的结果。"""

    path: str = ""
    duration: float = 0.0
    size_bytes: int = 0
    bit_rate: int = 0             # bps（容器整体码率）
    format_name: str = ""
    streams: List[MediaStream] = field(default_factory=list)
    probe_method: str = ""        # ffprobe / ffmpeg

    @property
    def video(self) -> Optional[MediaStream]:
        for s in self.streams:
            if s.codec_type == "video":
                return s
        return None

    @property
    def audio(self) -> Optional[MediaStream]:
        for s in self.streams:
            if s.codec_type == "audio":
                return s
        return None

    @property
    def has_video(self) -> bool:
        return self.video is not None

    @property
    def has_audio(self) -> bool:
        return self.audio is not None


def _to_float(v, default: float = 0.0) -> float:
    try:
        if v in (None, "", "N/A"):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default: int = 0) -> int:
    return int(_to_float(v, float(default)))


def _parse_rate(rate: str) -> float:
    """解析 ffprobe 的 r_frame_rate / avg_frame_rate（形如 '30000/1001'）。"""
    if not rate:
        return 0.0
    if "/" in rate:
        num, _, den = rate.partition("/")
        try:
            num_f, den_f = float(num), float(den)
            return num_f / den_f if den_f else 0.0
        except ValueError:
            return 0.0
    return _to_float(rate)


class ProbeError(RuntimeError):
    """探测失败。"""


def probe_with_ffprobe(ffprobe: str, path: str) -> MediaInfo:
    """用 ffprobe 探测（JSON 输出，最可靠）。"""
    cmd = [
        ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_streams", "-show_format",
        path,
    ]
    rc, out, err = run_capture(cmd, timeout=60)
    if rc != 0 and not out.strip():
        raise ProbeError(f"ffprobe 返回码 {rc}：{err.strip()[:400]}")
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise ProbeError(f"ffprobe 输出无法解析为 JSON：{exc}") from exc

    info = MediaInfo(path=path, probe_method="ffprobe")
    fmt = data.get("format") or {}
    info.format_name = fmt.get("format_name", "") or ""
    info.duration = _to_float(fmt.get("duration"))
    info.size_bytes = _to_int(fmt.get("size")) or _safe_size(path)
    info.bit_rate = _to_int(fmt.get("bit_rate"))

    for raw in data.get("streams") or []:
        st = MediaStream()
        st.index = _to_int(raw.get("index"), -1)
        st.codec_type = raw.get("codec_type", "") or ""
        st.codec_name = (raw.get("codec_name", "") or "").lower()
        st.pix_fmt = (raw.get("pix_fmt", "") or "").lower()
        st.width = _to_int(raw.get("width"))
        st.height = _to_int(raw.get("height"))
        st.fps = _parse_rate(raw.get("avg_frame_rate") or raw.get("r_frame_rate") or "")
        st.channels = _to_int(raw.get("channels"))
        st.bit_rate = _to_int(raw.get("bit_rate"))
        st.sample_rate = _to_int(raw.get("sample_rate"))
        info.streams.append(st)

    _finalize_duration(info)
    return info


# ffmpeg 输出样例：
#   Duration: 00:00:10.03, start: 0.000000, bitrate: 1234 kb/s
#   Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 1920x1080, 1234 kb/s, 30 fps
#   Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 128 kb/s
_RE_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_RE_BITRATE = re.compile(r"bitrate:\s*(\d+)\s*kb/s", re.I)
_RE_STREAM = re.compile(r"Stream\s+#\d+:\d+.*?:\s*(Video|Audio|Subtitle|Data|Attachment)\s*:\s*([A-Za-z0-9_\-]+)")
_RE_PIXFMT = re.compile(r"\b(yuv[a-z0-9]+|nv[0-9]{2}|rgb[a-z0-9]*|bgr[a-z0-9]*|gray[a-z0-9]*|pal8)\b", re.I)
_RE_RES = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_RE_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:fps|tbr)", re.I)
_RE_HZ = re.compile(r"(\d+)\s*Hz", re.I)
_RE_CH = re.compile(r"\b(mono|stereo|5\.1|7\.1|quad)\b", re.I)


def probe_with_ffmpeg(ffmpeg: str, path: str) -> MediaInfo:
    """ffprobe 缺失时的回退：解析 `ffmpeg -i <file>` 的 stderr 流信息。

    `ffmpeg -i file` 不带输出参数时会以返回码 1 结束，这是预期行为。
    """
    cmd = [ffmpeg, "-hide_banner", "-i", path]
    rc, out, err = run_capture(cmd, timeout=60)
    text = (out or "") + "\n" + (err or "")
    if not text.strip():
        raise ProbeError("ffmpeg 无任何输出，无法探测")
    if "No such file or directory" in text or "Invalid data found" in text or "does not exist" in text:
        raise ProbeError(f"ffmpeg 无法打开该文件的媒体信息：{_first_error_line(text)}")

    info = MediaInfo(path=path, probe_method="ffmpeg")
    info.size_bytes = _safe_size(path)

    m = _RE_DURATION.search(text)
    if m:
        info.duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = _RE_BITRATE.search(text)
    if m:
        info.bit_rate = int(m.group(1)) * 1000

    # 只看第一个 Input 块，避免输出文件信息干扰（虽然这里没有输出文件）
    for line in text.splitlines():
        m = _RE_STREAM.search(line)
        if not m:
            continue
        st = MediaStream()
        st.codec_type = m.group(1).lower()
        st.codec_name = m.group(2).lower()
        if st.codec_type == "video":
            pm = _RE_PIXFMT.search(line)
            st.pix_fmt = (pm.group(1).lower() if pm else "")
            rm = _RE_RES.search(line)
            if rm:
                st.width, st.height = int(rm.group(1)), int(rm.group(2))
            fm = _RE_FPS.search(line)
            st.fps = float(fm.group(1)) if fm else 0.0
        elif st.codec_type == "audio":
            hm = _RE_HZ.search(line)
            st.sample_rate = int(hm.group(1)) if hm else 0
            cm = _RE_CH.search(line)
            if cm:
                st.channels = {"mono": 1, "stereo": 2, "5.1": 6, "7.1": 8, "quad": 4}.get(cm.group(1).lower(), 0)
        bm = re.search(r"(\d+)\s*kb/s", line)
        if bm:
            st.bit_rate = int(bm.group(1)) * 1000
        info.streams.append(st)

    if not info.streams:
        raise ProbeError(f"未能从 ffmpeg 输出中解析出任何媒体流：\n{text[:800]}")
    _finalize_duration(info)
    return info


def _first_error_line(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s and ("rror" in s or "nvalid" in s or "o such" in s):
            return s
    return text.strip().splitlines()[0] if text.strip() else "未知错误"


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _finalize_duration(info: MediaInfo) -> None:
    """时长缺失时用「文件大小 / 容器码率」反推，保证体积估算不失效。"""
    if info.duration > 0:
        return
    if info.bit_rate > 0 and info.size_bytes > 0:
        info.duration = info.size_bytes * 8.0 / float(info.bit_rate)


def probe_media(tools: ToolBox, path: str, log: Callable[[str], None]) -> MediaInfo:
    """统一探测入口：ffprobe 优先，缺失时回退 ffmpeg -i 解析。"""
    if tools.ffprobe:
        try:
            info = probe_with_ffprobe(tools.ffprobe, path)
            if info.streams:
                return info
            log("   ⚠ ffprobe 未返回任何流信息，改用 ffmpeg -i 回退探测")
        except ProbeError as exc:
            log(f"   ⚠ ffprobe 探测失败（{exc}），改用 ffmpeg -i 回退探测")
        except Exception as exc:  # noqa: BLE001
            log(f"   ⚠ ffprobe 异常（{exc}），改用 ffmpeg -i 回退探测")
    else:
        log("   ℹ 未找到 ffprobe.exe，使用 ffmpeg -i 输出解析方式探测（回退路径）")

    if not tools.ffmpeg:
        raise ProbeError("既没有 ffprobe.exe 也没有 ffmpeg.exe，无法探测媒体信息")

    info = probe_with_ffmpeg(tools.ffmpeg, path)
    return info


# --------------------------------------------------------------------------- #
# 转换策略决策
# --------------------------------------------------------------------------- #

@dataclass
class Plan:
    """一次转换的计划。"""

    mode: str = "remux"                 # remux / transcode
    reason: str = ""
    target_bytes: int = 0               # 0 表示不限
    video_kbps: int = 0                 # 0 表示用 CRF 质量模式
    audio_kbps: int = DEFAULT_AUDIO_KBPS
    crf: int = 20
    level: str = "4.0"
    profile: str = "main"
    extra_video_args: List[str] = field(default_factory=list)


def _level_for_resolution(height: int, fps: float) -> str:
    """按分辨率/帧率给出「物理上成立」的最小 H.264 level。

    level 同时约束宏块数（分辨率）与每秒宏块数（分辨率×帧率），
    标得过低会被严格的硬件解码器直接拒绝，因此必须随分辨率走。
    """
    if height <= 480:
        return "3.0"
    if height <= 720:
        return "3.1"
    if height <= 1080:
        return "4.0" if fps <= 30.5 else "4.2"
    return "5.1"


def pick_x264_level(height: int, fps: float, ultra_compatible: bool) -> str:
    """挑选 H.264 level / profile 组合。

    权衡说明（默认 main + 4.0，不盲目用 baseline + 3.0）：
      * main profile 支持 CABAC / B 帧，同样画质下码率比 baseline 低 10%~20%，
        微信 PC / iOS / 近三年安卓都完全支持，对 1080p 更合适，故作为默认；
      * baseline 无 CABAC / 无 B 帧，只在极老的安卓硬解上更保险，但同码率画质更差。
        因此「极致兼容模式」只把 profile 降到 baseline，并在 720p 及以下把 level 压到 3.1，
        1080p 及以上仍按分辨率派生 —— 否则标一个物理上不成立的 level 反而会被拒绝。
    """
    base = _level_for_resolution(height, fps)
    if ultra_compatible and height <= 720:
        cap = "3.1" if fps <= 30.5 else "4.0"
        if float(base) > float(cap):
            return cap
    return base


def decide_plan(info: MediaInfo, target_mb: float, ultra_compatible: bool) -> Plan:
    """智能判断：能只重封装就绝不转码；体积超限则必须转码重压。"""
    plan = Plan()
    plan.target_bytes = int(target_mb * 1024 * 1024) if target_mb and target_mb > 0 else 0
    plan.crf = 23 if ultra_compatible else 20
    plan.profile = "baseline" if ultra_compatible else "main"

    v = info.video
    a = info.audio
    reasons: List[str] = []

    if v is None:
        raise ProbeError("该文件没有视频流，无法转换为微信视频")

    v_codec = (v.codec_name or "").lower()
    a_codec = (a.codec_name or "").lower() if a else ""
    pix_fmt = (v.pix_fmt or "").lower()

    codec_ok = v_codec in WECHAT_VIDEO_CODECS
    audio_ok = (a is None) or (a_codec in WECHAT_AUDIO_CODECS)
    # 像素格式未知时（部分容器探测不到）按「可 remux」处理，风险由用户可见日志承担
    pix_ok = (not pix_fmt) or (pix_fmt in WECHAT_PIX_FMTS)

    if not codec_ok:
        reasons.append(f"视频编码为 {v_codec or '未知'}（非 H.264）")
    if not audio_ok:
        reasons.append(f"音频编码为 {a_codec or '未知'}（非 AAC）")
    if not pix_ok:
        reasons.append(f"像素格式为 {pix_fmt}（非 yuv420p，会导致 iOS 黑屏/安卓花屏）")
    if a is None:
        reasons.append("源无音轨（转换后仍为无声视频）")

    need_transcode = bool(reasons)

    # 体积判断：remux 后体积≈源体积（+少量 faststart 开销）
    if plan.target_bytes > 0:
        predicted = info.size_bytes * 1.02 + 256 * 1024
        if predicted > plan.target_bytes:
            over_mb = (predicted - plan.target_bytes) / (1024.0 * 1024.0)
            reasons.append(
                f"目标大小 {target_mb:.0f}MB，源体积 {info.size_bytes / 1048576.0:.1f}MB 超限（约超 {over_mb:.1f}MB）"
            )
            need_transcode = True

    if need_transcode:
        plan.mode = "transcode"
        plan.reason = "；".join(reasons) if reasons else "需要重编码"
        plan.level = pick_x264_level(v.height, v.fps, ultra_compatible)
        if plan.target_bytes > 0:
            _apply_size_target(plan, info)
    else:
        plan.mode = "remux"
        plan.reason = (
            f"源已是 H.264 + {a_codec if a else '无音轨'} + "
            f"{pix_fmt or '像素格式未知'}，且体积未超目标，直接重封装（画质零损失）"
        )
    return plan


def _apply_size_target(plan: Plan, info: MediaInfo) -> None:
    """按目标大小反算视频码率。

    公式：目标总码率(kbps) ≈ 目标大小(MB) × 8192 / 时长(秒)
          目标视频码率 = 目标总码率 × 安全系数 - 音频码率
    """
    duration = info.duration
    if duration <= 0:
        # 时长未知：退化为 CRF 质量模式，并明确告知无法保证体积
        plan.video_kbps = 0
        plan.crf = 26
        return

    total_kbps = plan.target_bytes / 1048576.0 * MB_TO_KBPS / duration
    audio_kbps = DEFAULT_AUDIO_KBPS
    budget = total_kbps * SIZE_SAFETY_FACTOR
    if budget - audio_kbps < MIN_VIDEO_KBPS:
        audio_kbps = LOW_AUDIO_KBPS
    if budget - audio_kbps < MIN_VIDEO_KBPS:
        # 预算极紧：音频降到语音级，把码率让给画面
        audio_kbps = MIN_AUDIO_KBPS
    video_kbps = int(budget - audio_kbps)

    # 本函数仅在用户设定了目标大小时被调用：
    # 目标优先于画质保底——只有无目标（crf 质量模式）才用 MIN_VIDEO_KBPS 保底，
    # 有目标时降到硬下限也要尽量达成，否则像「40KB@5s」这类小目标永远无法满足。
    if video_kbps < HARD_MIN_VIDEO_KBPS:
        video_kbps = HARD_MIN_VIDEO_KBPS
    if video_kbps > MAX_VIDEO_KBPS:
        video_kbps = MAX_VIDEO_KBPS

    plan.audio_kbps = audio_kbps
    plan.video_kbps = video_kbps

    # 码率极度受限时同步降分辨率，否则低码率+高分辨率会糊成马赛克
    v = info.video
    if v and v.height > 720 and video_kbps < 900:
        plan.extra_video_args = ["-vf", "scale=-2:720"]
    elif v and v.height > 480 and video_kbps < 350:
        plan.extra_video_args = ["-vf", "scale=-2:480"]


# --------------------------------------------------------------------------- #
# 命令构建
# --------------------------------------------------------------------------- #

def unique_output_path(src: str, out_dir: Optional[str]) -> str:
    """生成 <原名>_wechat.mp4；已存在则自动加序号；绝不与输入文件同路径。"""
    directory = out_dir if out_dir else os.path.dirname(os.path.abspath(src))
    stem = os.path.splitext(os.path.basename(src))[0]
    if not stem:
        stem = "output"
    candidate = os.path.join(directory, f"{stem}_wechat.mp4")
    src_key = os.path.normcase(os.path.abspath(src))

    def _collide(p: str) -> bool:
        return os.path.normcase(os.path.abspath(p)) == src_key or os.path.exists(p)

    if not _collide(candidate):
        return candidate
    for i in range(1, 1000):
        alt = os.path.join(directory, f"{stem}_wechat_{i}.mp4")
        if not _collide(alt):
            return alt
    return os.path.join(directory, f"{stem}_wechat_{int(time.time())}.mp4")


def build_command(ffmpeg: str, src: str, dst: str, plan: Plan, info: MediaInfo,
                  use_utf8_opt: bool) -> List[str]:
    """构建 ffmpeg 命令（列表形式，不经 shell，天然支持中文路径与空格）。"""
    cmd: List[str] = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    if use_utf8_opt:
        # 该选项在部分 ffmpeg 版本标注 experimental，已做能力探测 + 失败重试兜底
        cmd += ["-encoding", "UTF-8"]
    cmd += ["-i", src]
    # 只取第一条视频流与第一条音频流（音频可缺省，故用 0:a:0?）
    cmd += ["-map", "0:v:0", "-map", "0:a:0?"]

    if plan.mode == "remux":
        cmd += ["-c", "copy"]
    else:
        cmd += [
            "-c:v", "libx264",
            "-profile:v", plan.profile,
            "-level:v", plan.level,
            "-pix_fmt", "yuv420p",
            "-preset", "medium",
        ]
        if plan.video_kbps > 0:
            maxrate = int(plan.video_kbps * 1.3)
            bufsize = int(plan.video_kbps * 2)
            cmd += ["-b:v", f"{plan.video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k"]
        else:
            cmd += ["-crf", str(plan.crf)]
        cmd += plan.extra_video_args
        if info.has_audio:
            cmd += ["-c:a", "aac", "-b:a", f"{plan.audio_kbps}k", "-ac", "2"]
        else:
            cmd += ["-an"]

    # moov 前置，实现边下边播（微信必选项）
    cmd += ["-movflags", "+faststart"]
    cmd += [dst]
    return cmd


# --------------------------------------------------------------------------- #
# 带进度的 ffmpeg 执行
# --------------------------------------------------------------------------- #

class FFmpegRunner:
    """执行 ffmpeg 并通过回调上报进度（-progress pipe:1 结构化输出）。"""

    def __init__(self, ffmpeg: str) -> None:
        self.ffmpeg = ffmpeg
        self._proc: Optional[subprocess.Popen] = None
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def run(self, args: List[str], duration: float, on_progress) -> Tuple[int, str]:
        """args 为不含可执行文件的参数列表；返回 (returncode, stderr_text)。"""
        cmd = [self.ffmpeg] + list(args)
        self._cancel.clear()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            startupinfo=_startupinfo(),
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._proc = proc
        err_chunks: List[str] = []

        def _read_stderr() -> None:
            try:
                for raw in proc.stderr:  # 字节迭代，按 \n 切分
                    err_chunks.append(decode_bytes(raw))
            except Exception:  # noqa: BLE001
                pass

        def _read_stdout() -> None:
            """-progress pipe:1 输出 key=value 行，逐个事件解析。"""
            buf: dict = {}
            try:
                for raw in proc.stdout:
                    line = decode_bytes(raw).strip()
                    if not line:
                        continue
                    if "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    buf[key.strip()] = val.strip()
                    if key.strip() == "progress":
                        self._emit(buf, duration, on_progress)
                        if val.strip() == "end":
                            break
                        buf = {}
            except Exception:  # noqa: BLE001
                pass

        t_err = threading.Thread(target=_read_stderr, daemon=True)
        t_out = threading.Thread(target=_read_stdout, daemon=True)
        t_err.start()
        t_out.start()

        while proc.poll() is None:
            if self._cancel.is_set():
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                break
            time.sleep(0.1)

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        t_err.join(timeout=5)
        t_out.join(timeout=5)
        self._proc = None
        return proc.returncode or 0, "".join(err_chunks)

    @staticmethod
    def _emit(buf: dict, duration: float, on_progress) -> None:
        """把一次 progress 事件换算成百分比（0-100）。"""
        if duration <= 0:
            return
        ms = buf.get("out_time_ms")
        pct = None
        if ms and ms.isdigit():
            pct = int(ms) / 1000000.0 / duration * 100.0
        else:
            t = buf.get("out_time", "")
            m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", t or "")
            if m:
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                pct = secs / duration * 100.0
        if pct is None:
            return
        pct = max(0.0, min(100.0, pct))
        try:
            on_progress(pct)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 单个文件的完整转换流程
# --------------------------------------------------------------------------- #

class ConversionError(RuntimeError):
    """转换失败。"""


def convert_one(
    tools: ToolBox,
    src: str,
    out_dir: Optional[str],
    target_mb: float,
    ultra_compatible: bool,
    log: Callable[[str], None],
    progress: Callable[[float], None],
    is_cancelled: Callable[[], bool],
) -> dict:
    """转换单个文件，返回结果字典。"""
    src_abs = os.path.abspath(src)
    if not os.path.isfile(src_abs):
        raise ConversionError(f"文件不存在：{src_abs}")

    log(f"▶ 源文件：{src_abs}")
    info = probe_media(tools, src_abs, log)
    log(
        f"   探测方式：{info.probe_method} | 容器：{info.format_name or '-'} | "
        f"时长：{info.duration:.2f}s | 大小：{info.size_bytes / 1048576.0:.2f}MB"
    )
    if info.video:
        log(
            f"   视频：{info.video.codec_name or '?'} {info.video.width}x{info.video.height} "
            f"pix_fmt={info.video.pix_fmt or '?'} fps={info.video.fps:.2f}"
        )
    log(f"   音频：{(info.audio.codec_name if info.audio else '无音轨')}")

    plan = decide_plan(info, target_mb, ultra_compatible)
    mode_text = "重封装(remux，无损)" if plan.mode == "remux" else "转码(transcode)"
    log(f"✅ 转换路径：{mode_text}")
    log(f"   判定依据：{plan.reason}")
    if plan.mode == "transcode" and plan.video_kbps > 0:
        log(
            f"   目标码率：视频 {plan.video_kbps}kbps + 音频 {plan.audio_kbps}kbps"
            f"（level {plan.level} / profile {plan.profile}）"
        )
    if info.duration > WECHAT_MOMENTS_LIMIT_SEC:
        log(f"   ⚠ 时长 {info.duration:.0f}s 超过朋友圈 30 秒上限，发朋友圈需自行裁剪")

    dst = unique_output_path(src_abs, out_dir)
    log(f"   输出：{dst}")

    src_before = _file_signature(src_abs)
    use_utf8 = tools.supports_utf8_option()
    if use_utf8:
        log("   ℹ ffmpeg 支持 -encoding UTF-8，已注入（中文元数据/字幕兼容性更好）")

    runner = FFmpegRunner(tools.ffmpeg)
    attempt = 0
    start = time.time()
    returncode = 0
    stderr_text = ""

    while True:
        if is_cancelled():
            runner.cancel()
            raise ConversionError("已取消")

        cmd = build_command(tools.ffmpeg, src_abs, dst, plan, info, use_utf8)
        # -progress / -nostats / -loglevel 是全局选项，必须出现在 -i 之前，
        # 因此直接插在可执行文件路径之后（build_command 的第 0 项即 ffmpeg 路径）。
        progress_args = ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]
        args = progress_args + cmd[1:]
        log(f"   $ {' '.join(_q(a) for a in cmd)}")
        returncode, stderr_text = runner.run(args, info.duration, progress)

        if returncode != 0 and use_utf8 and _is_unrecognized_option(stderr_text):
            log("   ⚠ ffmpeg 拒绝 -encoding 选项，去掉该选项重试")
            use_utf8 = False
            tools.disable_utf8_option()
            continue

        if returncode != 0:
            raise ConversionError(f"ffmpeg 失败（返回码 {returncode}）：\n{_tail(stderr_text, 1200)}")

        out_size = _safe_size(dst)
        if plan.target_bytes > 0 and out_size > plan.target_bytes and attempt < MAX_SIZE_RETRY and plan.video_kbps > 0:
            attempt += 1
            ratio = plan.target_bytes / float(out_size)
            new_kbps = int(plan.video_kbps * ratio * 0.97)
            if new_kbps < HARD_MIN_VIDEO_KBPS:
                log(
                    f"   ⚠ 输出 {out_size / 1048576.0:.1f}MB 仍超目标，但继续降码率会低于 "
                    f"{HARD_MIN_VIDEO_KBPS}kbps（画面无法编码），停止重压"
                )
                break
            log(
                f"   ⚠ 输出 {out_size / 1048576.0:.1f}MB 仍超目标 "
                f"{plan.target_bytes / 1048576.0:.1f}MB，第 {attempt} 次自适应重压："
                f"{plan.video_kbps}kbps → {new_kbps}kbps"
            )
            plan.video_kbps = new_kbps
            continue
        break

    elapsed = time.time() - start
    out_size = _safe_size(dst)
    src_after = _file_signature(src_abs)

    if src_before != src_after:
        raise ConversionError(f"❌ 源文件在转换后发生变化（{src_before} → {src_after}），请立即检查！")

    log(f"🎉 完成：{os.path.basename(dst)} | {out_size / 1048576.0:.2f}MB | 耗时 {elapsed:.1f}s | {mode_text}")
    if plan.target_bytes > 0:
        if out_size <= plan.target_bytes:
            log(f"   ✅ 体积已达标（{out_size / 1048576.0:.2f}MB ≤ {plan.target_bytes / 1048576.0:.1f}MB）")
        else:
            log(f"   ⚠ 体积仍略超目标（{out_size / 1048576.0:.2f}MB > {plan.target_bytes / 1048576.0:.1f}MB）")

    return {
        "src": src_abs,
        "dst": dst,
        "mode": plan.mode,
        "reason": plan.reason,
        "size": out_size,
        "duration": info.duration,
        "elapsed": elapsed,
        "info": info,
    }


def _q(arg: str) -> str:
    """命令行回显时的简单加引号（仅用于日志展示）。"""
    s = str(arg)
    return s if (s and not re.search(r"[\s\"']", s)) else f'"{s}"'


def _is_unrecognized_option(stderr_text: str) -> bool:
    return bool(
        re.search(r"Unrecognized option|Option .* not found|no such option|Unknown option", stderr_text, re.I)
    )


def _tail(text: str, n: int) -> str:
    return text[-n:] if len(text) > n else text


def _file_signature(path: str) -> Tuple[int, int]:
    """源文件指纹：(大小, mtime_ns)，用于验证「绝不破坏源文件」。"""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return (-1, -1)


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

class ConverterApp:
    """微信视频格式转换器主界面。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.tools = ToolBox()
        self._q: "queue.Queue" = queue.Queue()
        self._cancel = threading.Event()
        self._running = False
        self._last_outputs: List[str] = []

        self.root.title(APP_TITLE)
        self.root.geometry(APP_GEOMETRY)
        self.root.minsize(*APP_MINSIZE)
        self._build_ui()
        self.root.after(100, self._poll)

        if not self.tools.has_ffmpeg:
            self._log("❌ 未找到 ffmpeg.exe！请把 ffmpeg.exe 放到本程序所在目录后重启。")
            messagebox.showerror(
                "缺少 ffmpeg",
                "未找到 ffmpeg.exe。\n\n请把 ffmpeg.exe 放到本程序所在目录：\n"
                f"{app_dir()}\n\n然后重新启动本工具。",
            )
        else:
            self._log(f"✅ 已找到 ffmpeg：{self.tools.ffmpeg}")
            if self.tools.ffprobe:
                self._log(f"✅ 已找到 ffprobe：{self.tools.ffprobe}（使用精确探测）")
            else:
                self._log("ℹ 未找到 ffprobe.exe —— 将使用 `ffmpeg -i` 输出解析方式探测（回退路径，功能完整）")
                self._log("   建议把 ffprobe.exe 也放到同目录，可获得更精确的时长/像素格式信息。")

    # ---------------- 后台任务框架（queue + after，绝不阻塞 Tk 主线程） ----------------
    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                try:
                    self._handle(kind, payload)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"⚠ UI 回调异常：{exc}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, kind: str, payload) -> None:
        if kind == "log":
            self._log(payload)
        elif kind == "progress":
            self._set_progress(payload)
        elif kind == "status":
            self.status_var.set(payload)
        elif kind == "busy":
            self._set_busy(payload)
        elif kind == "total_pct":
            self.total_var.set(payload)
        elif kind == "total_label":
            self.total_label.configure(text=payload)
        elif kind == "finished":
            self._on_finished(payload)
        elif kind == "open_folder":
            self._open_folder(payload)

    # ---------------- UI 构建 ----------------
    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root)
        frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 文件选择
        file_frame = ttk.LabelFrame(frm, text="选择视频文件（可多选，依次转换）")
        file_frame.pack(fill=tk.BOTH, expand=False, **pad)
        # 说明：tkinter 原生不支持文件拖放（需额外的 tkinterdnd2/tkdnd 依赖），
        # 因此提供「添加文件」多选对话框 + 直接粘贴路径的方式。
        inner = ttk.Frame(file_frame)
        inner.pack(fill=tk.BOTH, expand=True, **pad)
        self.listbox = tk.Listbox(inner, height=5, activestyle="dotbox")
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(inner, orient=tk.VERTICAL, command=self.listbox.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(file_frame)
        btns.pack(fill=tk.X, **pad)
        ttk.Button(btns, text="添加文件…", command=self.add_files).pack(side=tk.LEFT)
        ttk.Button(btns, text="移除选中", command=self.remove_selected).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="清空", command=self.clear_files).pack(side=tk.LEFT)
        ttk.Label(btns, text="（提示：也可把文件路径粘贴到下方「手动路径」后回车）").pack(side=tk.LEFT, padx=10)

        path_row = ttk.Frame(file_frame)
        path_row.pack(fill=tk.X, **pad)
        ttk.Label(path_row, text="手动路径：").pack(side=tk.LEFT)
        self.path_var = tk.StringVar()
        entry = ttk.Entry(path_row, textvariable=self.path_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Return>", lambda _e: self.add_path_entry())
        ttk.Button(path_row, text="加入列表", command=self.add_path_entry).pack(side=tk.LEFT, padx=6)

        # 参数
        opt_frame = ttk.LabelFrame(frm, text="转换参数")
        opt_frame.pack(fill=tk.X, **pad)

        size_row = ttk.Frame(opt_frame)
        size_row.pack(fill=tk.X, **pad)
        ttk.Label(size_row, text="目标大小：").pack(side=tk.LEFT)
        self.size_var = tk.StringVar(value="none")
        for text, val in (
            ("不限（推荐）", "none"),
            (f"1GB（微信私聊上限 {WECHAT_CHAT_LIMIT_MB}MB）", "1024"),
            ("500MB", "500"),
            ("200MB", "200"),
            ("自定义", "custom"),
        ):
            ttk.Radiobutton(size_row, text=text, variable=self.size_var, value=val,
                            command=self._on_size_change).pack(side=tk.LEFT, padx=4)
        self.custom_var = tk.StringVar(value="100")
        self.custom_spin = ttk.Spinbox(size_row, from_=1, to=10240, width=6, textvariable=self.custom_var)
        self.custom_spin.pack(side=tk.LEFT)
        ttk.Label(size_row, text="MB").pack(side=tk.LEFT)
        self._on_size_change()

        comp_row = ttk.Frame(opt_frame)
        comp_row.pack(fill=tk.X, **pad)
        self.ultra_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            comp_row,
            text="极致兼容模式（baseline profile + 更低 level + CRF 23，兼容微信 8.0 以下老设备，画质略降）",
            variable=self.ultra_var,
        ).pack(side=tk.LEFT)
        self.same_dir_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(comp_row, text="输出到源文件同目录", variable=self.same_dir_var,
                        command=self._on_same_dir_change).pack(side=tk.LEFT, padx=12)

        out_row = ttk.Frame(opt_frame)
        out_row.pack(fill=tk.X, **pad)
        ttk.Label(out_row, text="输出目录：").pack(side=tk.LEFT)
        self.out_var = tk.StringVar()
        self.out_entry = ttk.Entry(out_row, textvariable=self.out_var)
        self.out_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(out_row, text="浏览…", command=self.choose_out_dir).pack(side=tk.LEFT, padx=6)
        self._on_same_dir_change()

        # 操作
        act_frame = ttk.Frame(frm)
        act_frame.pack(fill=tk.X, **pad)
        self.start_btn = ttk.Button(act_frame, text="开始转换", command=self.start_convert)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(act_frame, text="停止", command=self.stop_convert, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=8)
        self.open_btn = ttk.Button(act_frame, text="打开所在文件夹", command=self.open_last_folder,
                                   state=tk.DISABLED)
        self.open_btn.pack(side=tk.LEFT)

        prog_frame = ttk.Frame(frm)
        prog_frame.pack(fill=tk.X, **pad)
        ttk.Label(prog_frame, text="当前文件：").pack(side=tk.LEFT)
        self.cur_var = tk.DoubleVar(value=0.0)
        self.cur_bar = ttk.Progressbar(prog_frame, mode="determinate", maximum=100.0,
                                       variable=self.cur_var, length=300)
        self.cur_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.cur_label = ttk.Label(prog_frame, text="0%", width=6)
        self.cur_label.pack(side=tk.LEFT, padx=4)

        total_frame = ttk.Frame(frm)
        total_frame.pack(fill=tk.X, **pad)
        ttk.Label(total_frame, text="总体进度：").pack(side=tk.LEFT)
        self.total_var = tk.DoubleVar(value=0.0)
        self.total_bar = ttk.Progressbar(total_frame, mode="determinate", maximum=100.0,
                                         variable=self.total_var, length=300)
        self.total_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.total_label = ttk.Label(total_frame, text="0/0", width=8)
        self.total_label.pack(side=tk.LEFT, padx=4)

        # 日志
        log_frame = ttk.LabelFrame(frm, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=14, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.log_text.configure(state=tk.DISABLED)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        status = ttk.Label(frm, textvariable=self.status_var, anchor=tk.W, relief=tk.SUNKEN)
        status.pack(fill=tk.X, padx=8, pady=(0, 4))

    # ---------------- UI 事件 ----------------
    def _on_size_change(self) -> None:
        state = tk.NORMAL if self.size_var.get() == "custom" else tk.DISABLED
        self.custom_spin.configure(state=state)

    def _on_same_dir_change(self) -> None:
        state = tk.DISABLED if self.same_dir_var.get() else tk.NORMAL
        self.out_entry.configure(state=state)

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="选择视频文件", filetypes=VIDEO_EXT_FILTER)
        for p in paths:
            self._add_one(p)

    def add_path_entry(self) -> None:
        p = self.path_var.get().strip().strip('"').strip("'")
        if not p:
            return
        self._add_one(p)
        self.path_var.set("")

    def _add_one(self, path: str) -> None:
        ap = os.path.abspath(path)
        existing = self.listbox.get(0, tk.END)
        if ap in existing:
            return
        self.listbox.insert(tk.END, ap)

    def remove_selected(self) -> None:
        for idx in reversed(self.listbox.curselection()):
            self.listbox.delete(idx)

    def clear_files(self) -> None:
        self.listbox.delete(0, tk.END)

    def choose_out_dir(self) -> None:
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.out_var.set(d)
            self.same_dir_var.set(False)
            self._on_same_dir_change()

    def _log(self, text: str) -> None:
        """仅在主线程调用的日志写入。"""
        try:
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, str(text) + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        except Exception:  # noqa: BLE001
            pass

    def _set_progress(self, pct: float) -> None:
        self.cur_var.set(pct)
        self.cur_label.configure(text=f"{pct:.0f}%")

    def _set_busy(self, busy: bool) -> None:
        self._running = busy
        self.start_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.stop_btn.configure(state=tk.NORMAL if busy else tk.DISABLED)

    def open_last_folder(self) -> None:
        if not self._last_outputs:
            return
        self._open_folder(os.path.dirname(self._last_outputs[-1]))

    def _open_folder(self, directory: str) -> None:
        try:
            if os.name == "nt":
                os.startfile(directory)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", directory])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", f"{exc}")

    # ---------------- 转换流程 ----------------
    def get_target_mb(self) -> float:
        """读取目标大小（MB）；0 表示不限。"""
        v = self.size_var.get()
        if v == "none":
            return 0.0
        if v == "custom":
            try:
                return max(1.0, float(self.custom_var.get()))
            except ValueError:
                return 0.0
        try:
            return float(v)
        except ValueError:
            return 0.0

    def start_convert(self) -> None:
        if self._running:
            return
        files = list(self.listbox.get(0, tk.END))
        if not files:
            messagebox.showinfo("提示", "请先添加至少一个视频文件。")
            return
        if not self.tools.has_ffmpeg:
            messagebox.showerror("缺少 ffmpeg", "未找到 ffmpeg.exe，无法转换。")
            return

        # 所有 UI 取值必须在主线程完成，闭包内不得再读 Tk 控件
        target_mb = self.get_target_mb()
        ultra = bool(self.ultra_var.get())
        out_dir = None if self.same_dir_var.get() else (self.out_var.get().strip() or None)
        if out_dir and not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("输出目录无效", f"无法创建输出目录：{exc}")
                return

        self._cancel.clear()
        self._last_outputs = []
        self._set_busy(True)
        self.cur_var.set(0.0)
        self.cur_label.configure(text="0%")
        self.total_var.set(0.0)
        self.total_label.configure(text=f"0/{len(files)}")
        self.open_btn.configure(state=tk.DISABLED)
        self._log("=" * 70)
        self._log(f"开始转换，共 {len(files)} 个文件；目标大小：{'不限' if target_mb <= 0 else f'{target_mb:.0f}MB'}")

        def _work() -> None:
            results: List[dict] = []
            errors: List[str] = []
            total = len(files)
            for i, src in enumerate(files, start=1):
                if self._cancel.is_set():
                    self._q.put(("log", "⏹ 已取消后续任务"))
                    break
                self._q.put(("status", f"处理第 {i}/{total} 个：{os.path.basename(src)}"))
                self._q.put(("progress", 0.0))

                def _p(pct: float, base=(i - 1) / total, step=1.0 / total) -> None:
                    self._q.put(("progress", pct))
                    self._q.put(("total_pct", (base + step * pct / 100.0) * 100.0))

                try:
                    res = convert_one(
                        tools=self.tools,
                        src=src,
                        out_dir=out_dir,
                        target_mb=target_mb,
                        ultra_compatible=ultra,
                        log=lambda t: self._q.put(("log", t)),
                        progress=_p,
                        is_cancelled=lambda: self._cancel.is_set(),
                    )
                    results.append(res)
                except ConversionError as exc:
                    errors.append(f"{os.path.basename(src)}：{exc}")
                    self._q.put(("log", f"❌ 转换失败：{exc}"))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{os.path.basename(src)}：{exc}")
                    self._q.put(("log", f"❌ 未预期异常：{exc}\n{traceback.format_exc()}"))

                self._q.put(("total_pct", i / total * 100.0))
                self._q.put(("total_label", f"{i}/{total}"))

            self._q.put(("finished", {"results": results, "errors": errors, "total": total}))

        threading.Thread(target=_work, daemon=True).start()

    def stop_convert(self) -> None:
        if not self._running:
            return
        self._cancel.set()
        self.status_var.set("正在停止…")
        self._log("⏹ 正在请求停止（等待当前 ffmpeg 进程退出）…")

    def _on_finished(self, payload: dict) -> None:
        results = payload.get("results") or []
        errors = payload.get("errors") or []
        self._set_busy(False)
        self.total_var.set(100.0)
        self.total_label.configure(text=f"{len(results)}/{payload.get('total', 0)}")
        self._set_progress(100.0)
        self._last_outputs = [r["dst"] for r in results]
        if self._last_outputs:
            self.open_btn.configure(state=tk.NORMAL)

        ok = len(results)
        fail = len(errors)
        if fail == 0:
            self.status_var.set(f"全部完成（{ok} 个）")
            self._log(f"✅ 全部完成：成功 {ok} 个")
            messagebox.showinfo("转换完成", f"成功转换 {ok} 个文件。")
        else:
            self.status_var.set(f"完成 {ok} 个，失败 {fail} 个")
            self._log(f"⚠ 完成：成功 {ok} 个，失败 {fail} 个")
            messagebox.showwarning(
                "转换结束",
                f"成功 {ok} 个，失败 {fail} 个。\n\n" + "\n".join(errors[:5]),
            )


def main() -> None:
    root = tk.Tk()
    # 中文字体（与主程序 VideoDownloader 风格保持一致）
    try:
        root.option_add("*Font", "{Microsoft YaHei UI} 9")
    except Exception:  # noqa: BLE001
        pass
    app = ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
