import os
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
        if self._log_file:
            try:
                self._log_file.write(msg + "\n")
            except Exception:
                pass
        self.log_message.emit(self.task_id, msg)

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
            # playlist 场景不能把具体 format_id 硬塞给 yt-dlp——各条目可能不一样。
            # format_preset 是形如 "30126+30280" 的硬编码 id 时退回通用 selector。
            effective_format = self.format_preset
            if self.download_playlist and effective_format and "+" in effective_format:
                effective_format = DEFAULT_FORMAT
                self._write_log(
                    "playlist 下载忽略具体 format_id（各条目格式 id 不同），"
                    f"改用通用 selector: {effective_format}"
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
                base_options["cookiesfrombrowser"] = (self.cookie_browser,)

            if self.ratelimit:
                self._write_log(f"设置下载限速: {self.ratelimit}")
                base_options["ratelimit"] = self.ratelimit

            base_options.update(self.ydl_opts)

            with yt_dlp.YoutubeDL(base_options) as ydl:
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
