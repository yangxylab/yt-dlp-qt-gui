import os
import sys
import tempfile
from typing import Any

import yt_dlp
from PySide6.QtCore import QObject, Signal, Slot
from yt_dlp.utils import DownloadCancelled

from .config import DEFAULT_FORMAT, NO_PROGRESS, OUTPUT_TEMPLATE, get_task_log_path


def _is_on_external_volume(path: str) -> bool:
    """判断一个路径是不是在外接/网络驱动器上（macOS 上 /Volumes 下的盘、USB、SMB、Time Machine 等）。
    通过 stat 的 st_dev 和系统盘根目录 / 的 st_dev 对比。"""
    try:
        st_dir = os.stat(path)
        st_root = os.stat("/")
        return st_dir.st_dev != st_root.st_dev
    except OSError:
        return False


def _pick_staging_dir() -> str:
    """选 SSD 上的临时目录：优先 $TMPDIR（macOS SSD），fallback 系统 tempfile，最后 $HOME"""
    for candidate in (os.environ.get("TMPDIR"), tempfile.gettempdir(), os.path.expanduser("~/Library/Caches")):
        if candidate and os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            try:
                probe = os.path.join(candidate, ".yt_dlp_gui_probe_write")
                with open(probe, "w") as f:
                    f.write("x")
                os.remove(probe)
                return candidate
            except OSError:
                continue
    return os.path.expanduser("~")


class _GuidedNamingYoutubeDL(yt_dlp.YoutubeDL):
    """在文件名生成前往 info_dict 注入 name_suffix 字段，供 OUTPUT_TEMPLATE 使用。

    注意 yt-dlp 会对同一条视频多次调用 prepare_filename：
    早期（格式选定前）vcodec/height 还是 None，格式选定后才有值。
    所以不能"首次注入后锁定"，必须每次按当前信息重算，
    并对已算出的非空后缀做 sticky，防止后续衍生调用清空。
      视频   vcodec='avc1.640028' height=1080 → ' [1080p avc1]'
      DoVi   vcodec='dvhe.05.06'  height=2160 → ' [2160p dvhe]'
      纯音频 vcodec='none'                    → None（文件名不带分辨率/编码）
    """

    @staticmethod
    def _build_name_suffix(info: dict[str, Any]) -> str | None:
        vcodec = str(info.get("vcodec") or "")
        height = info.get("height")
        # 部分 extractor 不把选中视频轨字段合并到顶层，
        # 需从 requested_formats 里找视频子轨兜底
        if (not vcodec or vcodec == "none") and info.get("requested_formats"):
            for f in info["requested_formats"]:
                fv = str(f.get("vcodec") or "")
                if fv and fv != "none":
                    vcodec, height = fv, f.get("height")
                    break
        codec = vcodec.split(".")[0] if vcodec and vcodec != "none" else ""
        # 编码和分辨率都没有：纯音频，或格式信息尚未到齐的早期探查调用 → 不加后缀
        if not codec and not height:
            return None
        parts = []
        if height:
            parts.append(f"{height}p")
        if codec:
            parts.append(codec)
        return f" [{' '.join(parts)}]"

    def prepare_filename(self, info_dict, dir_type: str = "", *, outtmpl=None, warn: bool = False):
        suffix = self._build_name_suffix(info_dict)
        if suffix or not info_dict.get("name_suffix"):
            # 当前算出了后缀（信息已到齐），或此前还没有非空后缀（早期 incomplete 调用）→ 更新
            # 用 None 而非空串：yt-dlp 模板对 None + 空默认值 %(...|)s 渲染为空，
            # 空串会被它强制当成缺失字段（渲染成 NA）
            info_dict["name_suffix"] = suffix
        return super().prepare_filename(info_dict, dir_type, outtmpl=outtmpl, warn=warn)


class DownloadWorker(QObject):
    """下载任务工作器，运行在独立 QThread 中"""

    progress = Signal(int, dict)
    log_message = Signal(int, str)
    finished = Signal(int, bool, str)

    def __init__(
        self,
        task_id: int,
        url: str,
        download_path: str,
        format_preset: str | None = None,
        ydl_opts: dict | None = None,
        proxy: str | None = None,
        concurrent_fragments: int | None = None,
        write_subs: bool = False,
        download_playlist: bool = False,
        playlist_items: str | None = None,
        playlist_random: bool = False,
        max_downloads: int | None = None,
        impersonate: str | None = None,
        no_cookies: bool = False,
        cookie_browser: str | None = None,
        write_danmaku: bool = False,
        ratelimit: str | None = None,
    ) -> None:
        super().__init__()
        self.task_id = task_id
        self.url = url
        self.download_path = download_path
        self.format_preset = format_preset or DEFAULT_FORMAT
        self.ydl_opts = ydl_opts if ydl_opts else {}
        self.proxy = proxy
        self.concurrent_fragments = concurrent_fragments
        self.write_subs = write_subs
        self.download_playlist = download_playlist
        self.playlist_items = playlist_items
        self.playlist_random = playlist_random
        self.max_downloads = max_downloads
        self.impersonate = impersonate
        self.no_cookies = no_cookies
        self.cookie_browser = cookie_browser
        self.write_danmaku = write_danmaku
        self.ratelimit = ratelimit
        self._is_cancelled = False
        self._log_file = None
        # 追踪本次 extract_info+download 是否真的触发过文件下载
        # （yt-dlp 遇到已存在的文件会跳过，不发 downloading 事件）
        self._saw_downloading = False

    def _write_log(self, msg: str) -> None:
        # 每条日志加 [HH:MM:SS] 时间戳，便于排查时序问题
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        if self._log_file:
            try:
                self._log_file.write(line + "\n")
            except Exception:
                pass
        self.log_message.emit(self.task_id, line)

    def _progress_hook(self, d: dict[str, Any]) -> None:
        if self._is_cancelled:
            self._write_log("正在中断下载...")
            raise DownloadCancelled("用户取消下载")

        if d["status"] == "downloading":
            self._saw_downloading = True
            self.progress.emit(self.task_id, d)
        elif d["status"] == "finished":
            # 视频 / 音频文件下载完成
            filename = d.get("filename", "")
            info = d.get("info_dict", {})

            # 无论跳过还是真下载，都把 info_dict 推给 scheduler
            # （scheduler 从里面取 title 去更新数据库）
            self.progress.emit(self.task_id, d)

            if filename:
                if self._saw_downloading:
                    self._write_log(f"文件下载完成: {os.path.basename(filename)}")
                else:
                    # 没见过 downloading 事件 → yt-dlp 跳过了（文件已存在）
                    self._write_log(
                        f"文件已存在，跳过下载: {os.path.basename(filename)}"
                    )

            # B 站弹幕下载（每集 finished 各自触发，playlist 天然支持）
            # cid 有两条获取路径：
            #   1) yt-dlp info_dict["cid"]（BV 单视频通常有；bangumi extractor 有时漏写）
            #   2) URL → extract_bvid_ep_id → resolve_cid（兜底）
            if self.write_danmaku and info.get("extractor_key") in ("Bilibili", "BilibiliBangumi"):
                try:
                    from .bilibili_danmaku import (
                        download_danmaku, extract_bvid_ep_id, resolve_cid,
                    )

                    cid = info.get("cid")
                    if not cid:
                        ep_url = (info.get("webpage_url")
                                  or info.get("original_url")
                                  or info.get("url")
                                  or self.url)
                        if ep_url and ep_url.startswith("http"):
                            bvid, ep_id = extract_bvid_ep_id(ep_url)
                            cid = resolve_cid(bvid, ep_id)

                    if cid and filename:
                        base, _ext = os.path.splitext(filename)
                        xml_out = download_danmaku(cid, base + ".danmaku.xml")
                        if xml_out:
                            self._write_log(
                                f"B 站弹幕已下载: {os.path.basename(xml_out)}"
                            )
                except Exception as e:
                    self._write_log(f"B 站弹幕下载失败: {e}")
        elif d["status"] == "error":
            self._write_log(f"下载错误: {d.get('filename', '未知文件')}")

    def _postprocessor_hook(self, d: dict[str, Any]) -> None:
        """只有 FFmpegMerger 真跑起来才触发——假 merge 识别就在这里断掉"""
        if self._is_cancelled:
            return
        pp = d.get("postprocessor", "")
        status = d.get("status", "")
        if pp == "FFmpegMerger":
            if status == "started":
                self._write_log("开始合并视频和音频...")
                self.progress.emit(self.task_id, {"status": "merging"})
            elif status == "finished":
                self._write_log("合并完成")

    @Slot()
    def run(self) -> None:
        if not self.url:
            self.finished.emit(self.task_id, False, "URL 不能为空")
            return

        log_path = get_task_log_path(self.task_id)
        try:
            self._log_file = open(log_path, "w", encoding="utf-8", buffering=1)
        except Exception:
            pass

        try:
            # playlist 场景不能把裸 format_id（如 "30126+30280"）硬塞给 yt-dlp：
            # 各分集 id 可能不同，某集缺失会整条任务失败。
            # dialog 传来的特征 fallback 链（含 '/' 或 '['）已自带编码/分辨率意图和兜底，
            # 必须原样透传——否则用户选普通 HEVC 会被通用 selector 换成杜比视界。
            # 这里只兼容历史任务（DB 里存的裸 id spec）：追加一层通用兜底。
            effective_format = self.format_preset
            if (
                self.download_playlist
                and effective_format
                and "+" in effective_format
                and "/" not in effective_format
                and "[" not in effective_format
            ):
                effective_format = f"{effective_format}/{DEFAULT_FORMAT}"
                self._write_log(
                    "playlist 下载为具体 format_id 追加通用兜底 selector: "
                    f"{effective_format}"
                )

            base_options: Any = {
                "format": effective_format,
                "outtmpl": os.path.join(self.download_path, OUTPUT_TEMPLATE),
                "progress_hooks": [self._progress_hook],
                "postprocessor_hooks": [self._postprocessor_hook],
                "noplaylist": not self.download_playlist,
                "logger": self.YtdlpLogger(self._write_log),
                "noprogress": NO_PROGRESS,
                "merge_output_format": "mp4",
                "allow_unplayable_formats": False,
                "extract_flat": False,
                "nocheckcertificate": True,
                "socket_timeout": 30,
                "retries": 10,
                "fragment_retries": 10,
            }

            # 外接盘暂存：仅设置 paths.temp 让 .part / 中间文件走 SSD，
            # outtmpl 已经是绝对路径（L193），paths.home 对它无效
            # （yt-dlp 源码 L1569：绝对路径的 outtmpl 直接跳过 paths）
            # 注意：paths.temp 通过 get_output_path('temp') 生效，
            # 拼接规则是 paths.home + paths.temp + filename，
            # 所以 paths.home 必须保持默认（空串），不能设成 download_path！
            if _is_on_external_volume(self.download_path):
                staging = _pick_staging_dir()
                base_options["paths"] = {"temp": staging}
                self._write_log(
                    f"保存目录在非系统盘，启用 SSD 暂存: 中间文件 → {staging}, "
                    f"最终文件 → {self.download_path}"
                )

            if self.proxy:
                self._write_log(f"使用代理: {self.proxy}")
                base_options["proxy"] = self.proxy

            if self.concurrent_fragments is not None:
                base_options["concurrent_fragments"] = self.concurrent_fragments

            if self.write_subs:
                base_options["writesubtitles"] = True
                base_options["subtitleslangs"] = ["all"]

            if self.download_playlist:
                if self.playlist_items:
                    base_options["playlist_items"] = self.playlist_items
                if self.playlist_random:
                    base_options["playlist_random"] = True
                if self.max_downloads is not None:
                    base_options["max_downloads"] = self.max_downloads

            if self.impersonate:
                self._write_log(f"浏览器伪装: {self.impersonate}")
                try:
                    from yt_dlp.networking.impersonate import ImpersonateTarget
                    base_options["impersonate"] = ImpersonateTarget.from_str(self.impersonate)
                except (ImportError, AttributeError):
                    base_options["impersonate"] = self.impersonate

            if self.cookie_browser:
                # 从本机指定浏览器导入 cookies（yt-dlp 原生支持）
                # macOS 上 Chromium 系（chrome / edge / brave / arc）最稳，Safari 暂不支持
                self._write_log(f"从浏览器导入 Cookies: {self.cookie_browser}")
                # 提前跑一次预检查：yt-dlp 在 cookiesfrombrowser 找不到用户数据时
                # 会在内部 logger 打 ERROR 但继续跑（不崩），用户只看到两行神秘红字。
                # 我们预跑 extract_cookies_from_browser，拿到它的 CookieLoadError
                # 翻译成清晰提示，同时验证 cookies 能真读到。
                try:
                    from yt_dlp.cookies import extract_cookies_from_browser, SUPPORTED_BROWSERS
                    if self.cookie_browser not in SUPPORTED_BROWSERS:
                        self._write_log(f"⚠️ 浏览器 {self.cookie_browser} 不受 yt-dlp 支持，忽略 cookies")
                        self.cookie_browser = None
                    else:
                        # extract_cookies_from_browser 会返回 CookieJar，
                        # 也会自动解密 / 处理 keychain 等
                        jar = extract_cookies_from_browser(
                            self.cookie_browser,
                            logger=self._SilentLogger(),
                        )
                        count = sum(1 for _ in jar)
                        self._write_log(f"Extracted {count} cookies from {self.cookie_browser}")
                        base_options["cookiesfrombrowser"] = (self.cookie_browser,)
                except Exception as e:
                    err_msg = str(e)
                    # macOS 27+ 对 ~/Library/Application Support/<第三方浏览器> 目录加强了 TCC 权限控制：
                    # 内层 Cookies 文件其实存在，但外层目录 listing 被拒绝
                    # 报错关键词：Operation not permitted / could not find <browser> cookies
                    is_macos_tcc = (
                        sys.platform == "darwin"
                        and ("Operation not permitted" in err_msg
                             or "could not find" in err_msg)
                    )
                    self._write_log(f"⚠️ 无法从 {self.cookie_browser} 读取 cookies: {e}")
                    if is_macos_tcc:
                        self._write_log(
                            "💡 macOS 权限提示: 新版 macOS 加强了浏览器数据目录保护。"
                            "请到「系统设置 → 隐私与安全性 → 完全磁盘访问权限」"
                            "把本 App（或 .venv/bin/python3）加进去，然后重启 App。"
                        )
                    else:
                        self._write_log(
                            "提示: 如果是首次安装浏览器，请先启动一次并登录 B 站账号；"
                            "或到「高级设置」里改用 Chrome / Firefox。"
                        )
                    self.cookie_browser = None

            if self.ratelimit:
                self._write_log(f"设置下载限速: {self.ratelimit}")
                base_options["ratelimit"] = self.ratelimit

            base_options.update(self.ydl_opts)

            with _GuidedNamingYoutubeDL(base_options) as ydl:
                ydl.extract_info(self.url, download=True)

            if self._is_cancelled:
                self.finished.emit(self.task_id, False, "用户取消")
            else:
                self.finished.emit(self.task_id, True, "完成")

        except DownloadCancelled:
            self.finished.emit(self.task_id, False, "已取消")
        except Exception as e:
            self._write_log(f"下载出错: {e}")
            self.finished.emit(self.task_id, False, str(e))
        finally:
            if self._log_file:
                try:
                    self._log_file.close()
                except Exception:
                    pass
                self._log_file = None

    def cancel(self) -> None:
        if not self._is_cancelled:
            self._is_cancelled = True
            self._write_log("请求取消下载...")

    class _SilentLogger:
        """extract_cookies_from_browser 内部会调 logger 的多个方法，
        我们不想让它的错误和我们自己的提示混在一起，所以用个静音 stub，
        真正的失败由外层 except 捕获。"""
        def debug(self, msg: str) -> None: ...  # noqa: E704
        def info(self, msg: str) -> None: ...  # noqa: E704
        def warning(self, msg: str) -> None: ...  # noqa: E704
        def error(self, msg: str) -> None: ...  # noqa: E704

    class YtdlpLogger:
        """yt-dlp 日志记录器，过滤并转发日志消息"""

        def __init__(self, write_func) -> None:
            self.write_func = write_func

        def debug(self, msg: str) -> None:
            # 屏蔽 verbose 的进度输出（每个 chunk 都来一条），
            # 但保留"已经跳过"、"下载第 N 条"这种有意义的行
            if msg.startswith("[debug] "):
                return
            if msg.startswith("[download]"):
                lowered = msg.lower()
                if ("already" in lowered
                        or "downloading item" in lowered
                        or "has already been" in lowered):
                    self.write_func(msg)
                return
            self.write_func(msg)

        def warning(self, msg: str) -> None:
            self.write_func(f"警告: {msg}")

        def error(self, msg: str) -> None:
            self.write_func(f"错误: {msg}")

        def info(self, msg: str) -> None:
            if not msg.startswith("[download]"):
                self.write_func(msg)
