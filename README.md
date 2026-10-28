# VideoDownloader

Windows 桌面视频下载器，图形界面，基于 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 与 FFmpeg。
支持抖音与 YouTube，支持 SOCKS5 代理，开箱即用。

## 功能

- **抖音**：支持分享短链、`jingxuan?modal_id=` 等形态，自动归一化为标准视频地址
- **YouTube**：自动解开签名挑战（需 Node.js）
- **代理**：自动探测本机可用 SOCKS5 代理，并识别出口 IP 是否属于数据中心
- **Cookie**：自动加载同目录 `cookies.txt`，可判定是否含第一方登录态
- **诊断**：启动即做环境自检，按影响排序给出阻塞点

## 安装

把下面三个文件下载到**同一个文件夹**内即可使用（本程序免安装，双击运行）。

### 1. 主程序

| 文件 | 下载 |
|---|---|
| `VideoDownloader.exe` | **[Releases 页面 → v1.0.0](https://github.com/Lynnhs1n/VideoDownloader/releases/latest)** |

### 2. 依赖（必须自备，本仓库不收录二进制）

| 文件 | 直达下载链接 | 项目主页 |
|---|---|---|
| `yt-dlp.exe` | **[yt-dlp.exe 直链](https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe)** | https://github.com/yt-dlp/yt-dlp/releases/latest |
| `ffmpeg.exe` | **[ffmpeg-master-latest-win64-gpl.zip](https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip)** | https://github.com/BtbN/FFmpeg-Builds/releases |

> **FFmpeg 注意**：下载的 zip 解压后，需要的是 `bin/` 目录里的 **`ffmpeg.exe`** 这一个文件，
> 把它单独拷到上面的文件夹即可。同目录的 `ffplay.exe`、`ffprobe.exe` 不是必需的。
>
> **yt-dlp 注意**：直链拿到的是 Windows 独立可执行文件，直接能用，无需安装 Python。

`cookies.txt` 可选，放在同目录会被自动加载。

## 使用

1. 粘贴视频链接 → 选清晰度 → 点开始
2. 需要代理时，程序会自动探测；也可在设置里手动指定
3. YouTube 建议先提供登录 cookie（Netscape 格式），通过浏览器扩展导出

### 导出 YouTube Cookie

由于 Chrome 的 App-Bound Encryption（v20），外部程序无法直接解密浏览器 Cookie 库。
本仓库提供 `_vd_cookie_ext/` 扩展：

1. Chrome 打开 `chrome://extensions/`，开启**开发者模式**
2. 点「加载已解压的扩展程序」，选择 `_vd_cookie_ext` 目录
3. 扩展运行后会自动导出，含 `.youtube.com` 与 `.google.com` 双域 Cookie

注意：导出范围必须包含 `.google.com` 域，只导出 YouTube 单站的 Cookie 认证链是不完整的。

## 关于 YouTube 的登录/人机验证

YouTube 对自动化下载有三层风控，需逐项排除：

| 阻塞项 | 说明 |
|---|---|
| 出口 IP 属数据中心 | 影响最大。建议换住宅/ISP 节点 |
| 缺第一方登录 Cookie | 需含 `SID` / `HSID` / `SSID` 等 |
| 未配置 PO Token | 在界面「PO Token」框填入 `web+<token>` |

## 从源码构建

需要 Python 3.12（系统默认的 3.13 无 tkinter）：

```bash
py -3.12 -m PyInstaller --onefile --windowed --name VideoDownloader video_downloader.py
```

测试：

```bash
py -3.12 test_p2_branch.py
```

## 说明

本项目仅供个人学习与技术交流，下载内容请遵守各平台服务条款与所在地区法律法规。
