"""P2 回归测试：验证 _browser_fallback 不再阻断 cookies_file 赋值。

用桩替换 tkinter（不弹真实窗口），直接驱动 DownloaderApp._begin_download，
覆盖 QA 报的三轮兜底链场景，断言每一轮 cookies_file 都不为空。

后续追加的两组用例（Bug #1 / Bug #2）：
  - Bug #1 YouTube HLS 分片 403：DASH 优先的 -f 表达式、PO Token 注入、
    分片 403 的快速失败与定向诊断。
  - Bug #2 日志文件名乱码：子进程输出的编码回退解码 + --encoding UTF-8 注入。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
from unittest import mock

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC_DIR)


# ---------- 构造 tkinter 桩 ----------
class _Var:
    """真实的 StringVar/BooleanVar/IntVar 语义（Mock 返回 Mock 会改变分支走向）。"""

    def __init__(self, master=None, value=None):
        self._v = value

    def set(self, v):
        self._v = v

    def get(self):
        return self._v

    def trace_add(self, *args, **kwargs):
        return ""

    def trace_remove(self, *args, **kwargs):
        return ""


def _install_fake_tkinter():
    tk = types.ModuleType("tkinter")
    tk.Tk = mock.MagicMock()
    tk.Toplevel = mock.MagicMock()
    tk.StringVar = _Var
    tk.BooleanVar = lambda master=None, value=False: _Var(master, value)
    tk.IntVar = lambda master=None, value=0: _Var(master, value)
    tk.DoubleVar = lambda master=None, value=0.0: _Var(master, value)
    for name in (
        "Frame", "Label", "Button", "Entry", "Checkbutton", "Radiobutton",
        "Listbox", "Text", "Scrollbar", "Menu", "LabelFrame", "Canvas",
        "PanedWindow", "Spinbox", "Scale", "Message", "PhotoImage",
    ):
        setattr(tk, name, mock.MagicMock())
    tk.END = "end"
    tk.NORMAL = "normal"
    tk.DISABLED = "disabled"
    tk.W = "w"
    tk.E = "e"
    tk.N = "n"
    tk.S = "s"
    tk.LEFT = "left"
    tk.RIGHT = "right"
    tk.BOTH = "both"
    tk.X = "x"
    tk.Y = "y"

    ttk = types.ModuleType("tkinter.ttk")
    for name in ("Frame", "Label", "Button", "Entry", "Checkbutton",
                 "Radiobutton", "Combobox", "Progressbar", "Notebook",
                 "LabelFrame", "Separator", "Style", "Treeview", "Scrollbar"):
        setattr(ttk, name, mock.MagicMock())

    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askopenfilename = mock.MagicMock(return_value="")
    filedialog.askdirectory = mock.MagicMock(return_value="")

    scrolledtext = types.ModuleType("tkinter.scrolledtext")
    scrolledtext.ScrolledText = mock.MagicMock()

    messagebox = types.ModuleType("tkinter.messagebox")
    for name in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel"):
        setattr(messagebox, name, mock.MagicMock(return_value=True))

    # 其余未显式列出的常量（WORD / INSERT / SUNKEN ...）一律返回可用的占位对象
    def _tk_getattr(name):
        if name.startswith("__"):
            raise AttributeError(name)
        return mock.MagicMock(name="tk." + name)

    tk.__getattr__ = _tk_getattr
    tk.ttk = ttk
    tk.filedialog = filedialog
    tk.scrolledtext = scrolledtext
    tk.messagebox = messagebox
    for mod_name, mod in (("tkinter", tk), ("tkinter.ttk", ttk),
                          ("tkinter.filedialog", filedialog),
                          ("tkinter.scrolledtext", scrolledtext),
                          ("tkinter.messagebox", messagebox)):
        sys.modules[mod_name] = mod
    return tk


tk = _install_fake_tkinter()
import video_downloader as vd  # noqa: E402


# ---------- 测试驱动 ----------
def run_round(name, bot_retried, browser_fallback, client_fallback,
              use_browser_ck, expect_cookies_nonempty, cookies_found=True):
    """构造一次 _begin_download，返回 (cookies_file, log 文本)。"""
    root = mock.MagicMock()
    app = vd.DownloaderApp(root)

    app._probing = False
    app._bot_retried = bot_retried
    app._browser_fallback = browser_fallback
    app._client_fallback = client_fallback
    app.use_browser_ck = _Var(None, use_browser_ck)
    app.cookies_file_var = _Var(None, "")
    app.browser_var = _Var(None, "chrome")
    app.node_var = _Var(None, "")
    app.mode_var = _Var(None, "video_best")
    app.res_var = _Var(None, "1080")
    app.playlist_var = _Var(None, False)
    app._pending = {"urls": ["https://youtu.be/kHGbQ6yCFG4"],
                    "out": os.path.join(SRC_DIR, "_test_out"),
                    "ff": ""}

    logs = []
    app.log_line = logs.append

    captured = {}
    real_build = vd.build_command

    def _spy_build(opts, urls):
        captured["cookies_file"] = opts.cookies_file
        captured["player_clients"] = opts.player_clients
        return real_build(opts, urls)

    with mock.patch.object(vd, "build_command", _spy_build), \
         mock.patch.object(vd, "chrome_abe_enabled", return_value=False), \
         mock.patch.object(vd, "browser_process_running", return_value=False), \
         mock.patch.object(vd, "DownloadEngine", mock.MagicMock()), \
         mock.patch.object(vd, "save_settings", mock.MagicMock()), \
         mock.patch.object(vd, "find_cookies",
                           return_value=os.path.join(
                               r"C:\Users\linxin\Downloads\VideoDownloader", "cookies.txt")
                           if cookies_found else None), \
         mock.patch.object(vd, "prepare_cookies_temp", side_effect=lambda p: p), \
         mock.patch.object(vd.DownloaderApp, "on_ck_change", lambda self: None), \
         mock.patch.object(vd.DownloaderApp, "set_status", lambda self, s: None), \
         mock.patch.object(vd.DownloaderApp, "set_progress", lambda self, p: None):
        app._begin_download(("socks5://127.0.0.1:10808", None, False))

    ck = captured.get("cookies_file", "")
    ok = (bool(ck) == expect_cookies_nonempty)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(f"       cookies_file = {ck!r}")
    print(f"       player_clients = {captured.get('player_clients')!r}")
    for line in logs:
        if "🍪" in line or "🔁" in line or "⚠ 未提供" in line:
            print(f"       log | {line}")
    return ok


# ---------- Bug #1 / Bug #2 的桩与用例 ----------
class _FakeStream:
    """替代子进程 stdout：按行吐出 bytes，模拟 Windows 下真实的字节流。"""

    def __init__(self, raw_lines):
        self._lines = list(raw_lines)

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        return None


class _FakeProc:
    """替代 subprocess.Popen 返回对象，只实现 DownloadEngine 用到的接口。"""

    def __init__(self, raw_lines, rc=1):
        self.stdout = _FakeStream(raw_lines)
        self.rc = rc
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self):
        return self.rc


def _run_engine(raw_lines):
    """跑一遍 DownloadEngine._reader，返回 (engine, logs, finished)。"""
    logs = []
    finished = {}

    def on_finished(rc, had_error, saw_bot=False):
        finished.update(rc=rc, had_error=had_error, saw_bot=saw_bot)

    eng = vd.DownloadEngine(logs.append, lambda v: None, lambda s: None, on_finished)
    proc = _FakeProc(raw_lines)
    eng.proc = proc
    eng.running = True
    eng._reader(proc)
    return eng, logs, finished, proc


def check_decode_stream_line():
    """Bug #2：GBK 字节必须被解成正确中文（此前按 UTF-8 硬解 → 一串问号）。"""
    print()
    print("=== Bug #2：子进程输出编码回退解码 ===")
    title = "峰哥亡命天涯马驹桥原版视频，墙内已删。"
    all_ok = True

    ok = vd.decode_stream_line(title.encode("gbk")) == title
    print(f"[{'PASS' if ok else 'FAIL'}] GBK 字节 → 正确中文（{title[:8]}…）")
    all_ok &= ok

    ok = vd.decode_stream_line(title.encode("utf-8")) == title
    print(f"[{'PASS' if ok else 'FAIL'}] UTF-8 字节 → 正确中文")
    all_ok &= ok

    ok = vd.decode_stream_line(b"[download] 100.0% of 1.00MiB") == "[download] 100.0% of 1.00MiB"
    print(f"[{'PASS' if ok else 'FAIL'}] 纯 ASCII 行不受影响")
    all_ok &= ok

    ok = "utf-8" in vd._STREAM_ENCODINGS and vd._STREAM_ENCODINGS[0] == "utf-8"
    print(f"[{'PASS' if ok else 'FAIL'}] 解码候选顺序以 UTF-8 优先：{vd._STREAM_ENCODINGS}")
    all_ok &= ok
    return all_ok


def check_command_flags():
    """Bug #1 + Bug #2：build_command 注入的开关必须正确。"""
    print()
    print("=== build_command：DASH 优先 / PO Token / --encoding ===")
    all_ok = True
    out_dir = os.path.join(SRC_DIR, "_test_out")

    # 场景 1：支持 --encoding + 同时给 player_client 与 po_token
    opts = vd.DownloadOptions(
        mode="video_best", resolution="1080", output_dir=out_dir,
        player_clients=vd.YT_FALLBACK_PLAYER_CLIENTS, po_token="web+ABC123")
    with mock.patch.object(vd, "ytdlp_supports_encoding", return_value=True):
        cmd = vd.build_command(opts, ["https://www.youtube.com/watch?v=KWGhxyu5osA"])

    ok = cmd.count("--extractor-args") == 1
    print(f"[{'PASS' if ok else 'FAIL'}] --extractor-args 只出现 1 次"
          f"（实测重复传会后者覆盖前者）= {cmd.count('--extractor-args')}")
    all_ok &= ok

    arg = cmd[cmd.index("--extractor-args") + 1]
    ok = arg == f"youtube:player_client={vd.YT_FALLBACK_PLAYER_CLIENTS};po_token=web+ABC123"
    print(f"[{'PASS' if ok else 'FAIL'}] 参数用 ';' 合并为一条：{arg[:70]}…")
    all_ok &= ok

    ok = "--encoding" in cmd and cmd[cmd.index("--encoding") + 1] == "UTF-8"
    print(f"[{'PASS' if ok else 'FAIL'}] 注入 --encoding UTF-8（修日志乱码）")
    all_ok &= ok

    fmt = cmd[cmd.index("-f") + 1]
    ok = "[protocol!=m3u8_native]" in fmt and fmt.endswith("/bv*+ba/best")
    print(f"[{'PASS' if ok else 'FAIL'}] -f 首选纯 DASH 且保留回退：{fmt}")
    all_ok &= ok

    # 场景 2：不支持 --encoding 的老版本 yt-dlp → 不得注入
    with mock.patch.object(vd, "ytdlp_supports_encoding", return_value=False):
        cmd2 = vd.build_command(opts, ["https://x"])
    ok = "--encoding" not in cmd2
    print(f"[{'PASS' if ok else 'FAIL'}] 老版本 yt-dlp 不注入 --encoding（避免 no such option）")
    all_ok &= ok

    # 场景 3：未填 PO Token → 不出现 po_token
    opts3 = vd.DownloadOptions(mode="video_res", resolution="720",
                               output_dir=out_dir, po_token="")
    with mock.patch.object(vd, "ytdlp_supports_encoding", return_value=True):
        cmd3 = vd.build_command(opts3, ["https://x"])
    ok = "po_token" not in " ".join(cmd3)
    print(f"[{'PASS' if ok else 'FAIL'}] 未填 PO Token 时不注入 po_token")
    all_ok &= ok

    fmt3 = cmd3[cmd3.index("-f") + 1]
    ok = "height<=720" in fmt3 and "[protocol!=m3u8_native]" in fmt3
    print(f"[{'PASS' if ok else 'FAIL'}] 指定分辨率模式同样避开 HLS：{fmt3}")
    all_ok &= ok

    # 场景 4：非法分辨率不应抛异常
    opts4 = vd.DownloadOptions(mode="video_res", resolution="abc", output_dir=out_dir)
    with mock.patch.object(vd, "ytdlp_supports_encoding", return_value=True):
        cmd4 = vd.build_command(opts4, ["https://x"])
    ok = "height<=1080" in cmd4[cmd4.index("-f") + 1]
    print(f"[{'PASS' if ok else 'FAIL'}] 非法分辨率回退 1080：{cmd4[cmd4.index('-f') + 1]}")
    all_ok &= ok
    return all_ok


def check_fragment_403_fastfail():
    """Bug #1：连续分片 403 必须主动中止，而不是 292 分片 × 10 次重试慢慢磨。"""
    print()
    print("=== Bug #1：分片 403 快速失败 ===")
    all_ok = True

    lines = [b"[hlsnative] Total fragments: 292\n"]
    lines += [f"[download] Got error: HTTP Error 403: Forbidden. "
              f"Retrying fragment 1 ({i}/10)...\n".encode("utf-8") for i in range(1, 7)]
    eng, logs, finished, proc = _run_engine(lines)

    ok = eng.abort_reason == "hls403"
    print(f"[{'PASS' if ok else 'FAIL'}] 触发主动中止，abort_reason = {eng.abort_reason!r}")
    all_ok &= ok

    ok = proc.terminated
    print(f"[{'PASS' if ok else 'FAIL'}] 已向子进程发送终止信号")
    all_ok &= ok

    ok = eng._frag403 < len(lines)
    print(f"[{'PASS' if ok else 'FAIL'}] 未等满 {len(lines) - 1} 条重试就中止"
          f"（实际计数 {eng._frag403}，阈值 {vd.FRAGMENT_403_ABORT_AT}）")
    all_ok &= ok

    ok = any("🛑" in ln for ln in logs)
    print(f"[{'PASS' if ok else 'FAIL'}] 日志给出中止说明")
    all_ok &= ok

    ok = sum(1 for ln in logs if "403" in ln and "⚠" in ln) == 1
    print(f"[{'PASS' if ok else 'FAIL'}] 403 提示只打印一次（不刷屏）")
    all_ok &= ok

    # 正常下载（无 403）不应被误中止
    normal = [b"[download] Destination: test.mp4\n",
              b"[download]   0.5% of 10.00MiB at 1.00MiB/s ETA 00:10\n",
              b"[download] 100.0% of 10.00MiB in 00:09\n"]
    eng2, _, finished2, proc2 = _run_engine(normal)
    ok = (eng2.abort_reason == "") and (not proc2.terminated) and finished2.get("rc") == 1
    print(f"[{'PASS' if ok else 'FAIL'}] 正常下载不误触发中止"
          f"（abort_reason={eng2.abort_reason!r}）")
    all_ok &= ok
    return all_ok


def check_gbk_log_not_garbled():
    """Bug #2 端到端：喂 GBK 字节流，日志里必须出现正确的中文文件名。"""
    print()
    print("=== Bug #2：GBK 字节流 → 日志中文正确（不会变问号） ===")
    title = "峰哥亡命天涯马驹桥原版视频，墙内已删。"
    dest = f"[download] Destination: C:\\out\\{title} [KWGhxyu5osA].f616.mp4\n"
    eng, logs, _, _ = _run_engine([dest.encode("gbk")])
    joined = "\n".join(logs)
    ok = (title in joined) and ("📄" in joined)
    print(f"[{'PASS' if ok else 'FAIL'}] 日志中的输出文件名中文正确")
    print(f"       log | {joined.splitlines()[0][:90]}")
    return ok


def _make_app():
    """构造一个装好桩的 DownloaderApp，供 on_finish 用例使用。"""
    root = mock.MagicMock()
    app = vd.DownloaderApp(root)
    app.po_token_var = _Var(None, "")
    app.cookies_file_var = _Var(None, "")
    app.log_line = print
    app.set_status = lambda s: None
    app.start_btn = mock.MagicMock()
    app.stop_btn = mock.MagicMock()
    return app


def check_on_finish_hls403():
    """Bug #1：分片 403 不得走进 bot 兜底链，而是「重试一次 + 定向诊断」。"""
    print()
    print("=== Bug #1：on_finish 对 hls403 的处理 ===")
    all_ok = True
    app = _make_app()
    app._hls403_retried = False
    app._client_fallback = ""
    app.engine = mock.MagicMock()
    app.engine.abort_reason = "hls403"

    with mock.patch.object(vd.DownloaderApp, "start", lambda self, is_retry=False: None):
        app.on_finish(1, True, saw_bot=False)

    ok = bool(app._hls403_retried) and app._client_fallback == vd.YT_FALLBACK_PLAYER_CLIENTS
    print(f"[{'PASS' if ok else 'FAIL'}] 第一次：扩展 player_client 重试一次"
          f"（_hls403_retried={app._hls403_retried}）")
    all_ok &= ok

    app.root.reset_mock()
    app.on_finish(1, True, saw_bot=False)
    ok = app.root.after.call_count == 0
    print(f"[{'PASS' if ok else 'FAIL'}] 第二次：不再重试，改为输出诊断"
          f"（root.after 调用 {app.root.after.call_count} 次）")
    all_ok &= ok

    # 普通失败（无 abort_reason）仍应照旧走完兜底逻辑，不被新分支吞掉
    app2 = _make_app()
    app2.engine = mock.MagicMock()
    app2.engine.abort_reason = ""
    app2._bot_retried = True
    app2._client_fallback = vd.YT_FALLBACK_PLAYER_CLIENTS
    app2.root.reset_mock()
    with mock.patch.object(vd.DownloaderApp, "start", lambda self, is_retry=False: None):
        app2.on_finish(0, False, saw_bot=False)
    ok = app2.root.after.call_count == 0
    print(f"[{'PASS' if ok else 'FAIL'}] 无 abort_reason 时不误入 hls403 分支")
    all_ok &= ok
    return all_ok


def _spawn_sleeper():
    """起一个**真实**的非 yt-dlp 子进程（用于验证不会被误杀）。"""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _alive(proc):
    return proc.poll() is None


def check_no_mistaken_kill():
    """防误杀：把**非 yt-dlp** 的真实 PID 交给终止函数，绝不能被杀掉。

    用真实子进程，不用 mock——mock 掉被测函数等于测假设。
    """
    print()
    print("=== 防误杀：非 yt-dlp 进程绝不被 kill_process_tree / kill_pids_if_named 终止 ===")
    all_ok = True
    sleeper = _spawn_sleeper()
    try:
        time.sleep(1.0)
        ok = _alive(sleeper)
        print(f"[{'PASS' if ok else 'FAIL'}] 前置：参照进程已启动 PID={sleeper.pid}")
        all_ok &= ok

        res = vd.kill_process_tree(sleeper.pid, vd.YTDLP_EXE_NAME)
        time.sleep(1.0)
        ok = _alive(sleeper) and res.get("killed") == []
        print(f"[{'PASS' if ok else 'FAIL'}] kill_process_tree 未杀它"
              f"（killed={res.get('killed')} method={res.get('method')}）")
        all_ok &= ok

        ok = all("skipped:" in str(v)
                 for v in (res.get("skipped") or {}).values()) and bool(res.get("skipped"))
        print(f"[{'PASS' if ok else 'FAIL'}] 跳过原因均带 skipped: 前缀：{res.get('skipped')}")
        all_ok &= ok

        status = vd.kill_pids_if_named([sleeper.pid], vd.YTDLP_EXE_NAME)
        time.sleep(0.5)
        ok = _alive(sleeper) and str(status.get(sleeper.pid, "")).startswith("skipped:")
        print(f"[{'PASS' if ok else 'FAIL'}] kill_pids_if_named 返回"
              f" {status.get(sleeper.pid)!r} 且进程仍存活")
        all_ok &= ok

        name = vd.process_name_of(sleeper.pid)
        ok = bool(name) and name.lower() != vd.YTDLP_EXE_NAME.lower()
        print(f"[{'PASS' if ok else 'FAIL'}] process_name_of 取回真实映像名 {name!r}"
              f"（≠ {vd.YTDLP_EXE_NAME}）")
        all_ok &= ok
    finally:
        try:
            sleeper.kill()
            sleeper.wait(timeout=5)
        except Exception:
            pass
    return all_ok


def check_job_object_scope():
    """Job Object：只终止 Job 成员，绝不波及无关进程（真实进程验证）。"""
    print()
    print("=== Job Object：一次终止整棵进程树，且作用域仅限 Job 成员 ===")
    all_ok = True
    if sys.platform != "win32":
        print("[SKIP] 非 Windows 平台，跳过 Job Object 用例")
        return True

    sleeper = _spawn_sleeper()
    outsider = _spawn_sleeper()
    try:
        time.sleep(1.0)
        job = vd.attach_job_object(sleeper.pid)
        ok = bool(job)
        print(f"[{'PASS' if ok else 'FAIL'}] attach_job_object 成功：job={job}")
        all_ok &= ok
        if not job:
            return all_ok

        ok = bool(vd.terminate_job_object(job))
        print(f"[{'PASS' if ok else 'FAIL'}] terminate_job_object 返回 True")
        all_ok &= ok

        time.sleep(1.5)
        ok = not _alive(sleeper)
        print(f"[{'PASS' if ok else 'FAIL'}] Job 成员已终止（poll={sleeper.poll()}）")
        all_ok &= ok

        ok = _alive(outsider)
        print(f"[{'PASS' if ok else 'FAIL'}] Job **外**的无关进程未被波及，仍然存活")
        all_ok &= ok

        vd.close_job_object(job)
    finally:
        for p in (sleeper, outsider):
            try:
                if _alive(p):
                    p.kill()
                    p.wait(timeout=5)
            except Exception:
                pass
    return all_ok


def check_encoding_cache_per_path():
    """OBS-01：`ytdlp_supports_encoding` 的缓存必须以 ytdlp_path 为 key。"""
    print()
    print("=== OBS-01：--encoding 能力探测缓存按路径分别记录 ===")
    all_ok = True
    tmpbin = tempfile.mkdtemp(prefix="p2_fakebin_")

    def make_fake(dirname, has_encoding):
        d = os.path.join(tmpbin, dirname)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "fake_ytdlp.cmd")
        lines = ["@echo off", "echo usage: yt-dlp [OPTIONS] URL [URL...]"]
        lines.append("echo   --encoding ENCODING     Force the specified encoding"
                     if has_encoding else "echo   --no-playlist           Download only")
        with open(p, "w", encoding="ascii") as fh:
            fh.write("\r\n".join(lines) + "\r\n")
        return p

    fake_new = make_fake("new", True)
    fake_old = make_fake("old", False)

    vd._ENCODING_SUPPORT.clear()
    a = vd.ytdlp_supports_encoding(fake_old)
    b = vd.ytdlp_supports_encoding(fake_new)
    ok = (a is False) and (b is True)
    print(f"[{'PASS' if ok else 'FAIL'}] 先探老版(False)再探新版 → "
          f"{a} / {b}（修复前第二次会错误地返回 False）")
    all_ok &= ok

    vd._ENCODING_SUPPORT.clear()
    c = vd.ytdlp_supports_encoding(fake_new)
    d = vd.ytdlp_supports_encoding(fake_old)
    ok = (c is True) and (d is False)
    print(f"[{'PASS' if ok else 'FAIL'}] 反向顺序同样正确 → {c} / {d}")
    all_ok &= ok

    ok = isinstance(vd._ENCODING_SUPPORT, dict)
    print(f"[{'PASS' if ok else 'FAIL'}] 缓存容器是 dict（按路径分键）")
    all_ok &= ok
    shutil.rmtree(tmpbin, ignore_errors=True)
    vd._ENCODING_SUPPORT.clear()
    return all_ok


def main():
    print("=== P2：_browser_fallback 不得阻断 cookies_file 赋值 ===\n")
    all_ok = True

    # 场景 A：第 1 轮，纯文件 cookies（基线，改动前后都应非空）
    all_ok &= run_round("A 第1轮 文件cookies（基线）", False, False, "", False, True)

    # 场景 B：第 2 轮，文件 cookies + player_client 兜底
    all_ok &= run_round("B 第2轮 文件cookies + client兜底", False, False,
                        vd.YT_FALLBACK_PLAYER_CLIENTS, False, True)

    # 场景 C：第 3 轮 —— QA 报的缺陷场景：
    # _browser_fallback=True 且 use_browser=False，旧代码会跳过赋值导致 cookies 为空
    all_ok &= run_round("C 第3轮 browser_fallback=True 且 use_browser=False（缺陷场景）",
                        True, True, vd.YT_FALLBACK_PLAYER_CLIENTS, False, True)

    # 场景 D：_browser_fallback=True 且真的走浏览器 cookies（应保留浏览器分支，不给文件 cookies）
    all_ok &= run_round("D browser_fallback=True 且 use_browser=True", True, True, "", True, False)

    # 场景 E：目录里根本没有 cookies.txt，应给出「未提供任何登录态」提示
    all_ok &= run_round("E 无 cookies 可兜底（应为空 + 提示）", False, False, "",
                        False, False, cookies_found=False)

    # 缺陷「修复前」对照：复刻旧的互斥 if/elif/elif 结构，证明它会把 cookies 饿死
    print()
    print("=== 对照：修复前的旧结构（互斥 elif）在场景 C 下的行为 ===")
    cookies_file = ""
    use_browser = False
    _browser_fallback = True
    if _browser_fallback and use_browser:
        pass
    elif _browser_fallback:
        pass                      # ← 旧代码在此命中并整体跳过下一个 elif
    elif not use_browser:
        cookies_file = "cookies.txt"
    old_ok = (cookies_file == "")
    print(f"[{'PASS' if old_ok else 'FAIL'}] 旧结构在场景 C 下 cookies_file = {cookies_file!r}"
          f"（为空 → 复现 QA 报的缺陷）")
    all_ok &= old_ok

    print()
    print("=== P1：client 列表不得含不受支持的 tv_embedded ===")
    clients = [c.strip() for c in vd.YT_FALLBACK_PLAYER_CLIENTS.split(",")]
    ok = "tv_embedded" not in clients
    print(f"[{'PASS' if ok else 'FAIL'}] tv_embedded 已移除；当前列表 = {clients}")
    all_ok &= ok

    ok = "android" in clients
    print(f"[{'PASS' if ok else 'FAIL'}] 已补入 android（2026.10.26 实测受支持）")
    all_ok &= ok

    all_ok &= check_decode_stream_line()
    all_ok &= check_command_flags()
    all_ok &= check_fragment_403_fastfail()
    all_ok &= check_gbk_log_not_garbled()
    all_ok &= check_on_finish_hls403()
    all_ok &= check_no_mistaken_kill()
    all_ok &= check_job_object_scope()
    all_ok &= check_encoding_cache_per_path()

    print()
    print("RESULT:", "ALL PASS" if all_ok else "HAS FAILURE")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
