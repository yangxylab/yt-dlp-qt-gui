import os
import re
from typing import Any, Callable, Optional

import qtawesome as qta
from PySide6.QtCore import QObject, QStandardPaths, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .components import Switch
from .config import DEFAULT_FORMAT, DOVI_PRESET_NAME, FALLBACK_FORMAT_NO_DOVI, FORMAT_PRESETS, get_last_save_path, load_ui_state, save_ui_state
from .models import DownloadTask


class LogDialog(QDialog):
    """任务日志对话框 —— 带富文本着色 / 搜索 / 自动滚动 / 导出"""

    # 着色规则：(regex, CSS color)，按顺序匹配
    # 注意：Qt QTextEdit 不支持 span 里嵌套 span，所以一条日志只能有一个主颜色
    _COLOR_RULES: list[tuple[str, str]] = [
        # 错误类 — 红色
        (r"(?i)错误:|\[ERROR\]|Traceback|Exception|^ERROR|失败|失败|失败:", "#FF6B6B"),
        # 警告类 — 黄色
        (r"(?i)警告:|\[WARN|\[warning|WARNING|⚠️|⚠ | 警告", "#FFD93D"),
        # ffmpeg 合并 — 青绿
        (r"(?i)\[ffmpeg\]|合并|merg", "#4ECDC4"),
        # 弹幕 — 紫色
        (r"(?i)弹幕|danmaku|danmu", "#A78BFA"),
        # yt-dlp info / download / extractor 标签 — 灰色（降低视觉权重）
        (r"^\[[A-Za-z_]+\] ", "#9AA0A6"),
        # 业务高亮（我们自己写的关键动作）— 淡绿
        (r"下载完成|开始下载|跳过下载|开始合并|合并完成|正在中断|请求取消", "#86EFAC"),
    ]

    def __init__(self, task_id, title, parent=None):
        super().__init__(parent)
        self.task_id = task_id
        self.setWindowTitle(f"任务日志 - {title}")
        self.resize(820, 560)
        self._auto_scroll = True
        self._setup_ui()

    def _setup_ui(self):
        root = QVBoxLayout(self)

        # ---- 工具栏 ----
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 4, 4, 0)

        # 搜索框
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 搜索日志...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search_changed)
        toolbar.addWidget(self.search_edit, 1)

        # 自动滚动开关
        self.auto_scroll_btn = QPushButton("⏸ 暂停跟随")
        self.auto_scroll_btn.setCheckable(True)
        self.auto_scroll_btn.setChecked(True)
        self.auto_scroll_btn.clicked.connect(self._toggle_auto_scroll)
        self.auto_scroll_btn.setToolTip("关闭后手动往上翻看历史，新日志不再自动拉到底")
        toolbar.addWidget(self.auto_scroll_btn)

        # 自动换行开关
        self.wrap_btn = QPushButton("📜 换行")
        self.wrap_btn.setCheckable(True)
        self.wrap_btn.clicked.connect(self._toggle_wrap)
        self.wrap_btn.setToolTip("切换自动换行/水平滚动")
        toolbar.addWidget(self.wrap_btn)

        toolbar.addSpacing(8)

        # 操作按钮
        copy_btn = QPushButton("📋 复制全部")
        copy_btn.clicked.connect(self._copy_all)
        toolbar.addWidget(copy_btn)

        export_btn = QPushButton("📁 导出")
        export_btn.clicked.connect(self._export_to_file)
        toolbar.addWidget(export_btn)

        clear_btn = QPushButton("🗑 清空")
        clear_btn.clicked.connect(self._clear)
        toolbar.addWidget(clear_btn)

        root.addLayout(toolbar)

        # ---- 日志主体 ----
        from PySide6.QtWidgets import QTextEdit
        from PySide6.QtGui import QTextCursor, QTextDocument
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.log_output.setFont(
            QFont("Menlo", 10)
            if os.name != "nt"
            else QFont("Consolas", 10)
        )
        self.log_output.setStyleSheet(
            "QTextEdit { background-color: #0D1117; color: #E6EDF3; "
            "border: 1px solid #30363D; border-radius: 4px; padding: 6px; }"
        )
        # 用户手动滚动时，根据是否在底部更新自动跟随状态
        sb = self.log_output.verticalScrollBar()
        sb.valueChanged.connect(self._on_scroll_changed)

        root.addWidget(self.log_output, 1)

        # ---- 底部：状态 + 关闭 ----
        footer = QHBoxLayout()
        self.status_label = QLabel("0 行")
        self.status_label.setStyleSheet("color: #8B949E; font-size: 11px;")
        footer.addWidget(self.status_label)
        footer.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        footer.addWidget(btn_close)
        root.addLayout(footer)

    # ---- 着色 ----
    @classmethod
    def _colorize(cls, line: str) -> str:
        """把一行日志包成带颜色的 HTML span。"""
        color = "#E6EDF3"  # 默认：浅色
        for pattern, c in cls._COLOR_RULES:
            if re.search(pattern, line):
                color = c
                break
        # 转义 HTML 特殊字符，但保留原始空格（用 &nbsp; 或 CSS white-space）
        safe = (
            line.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return f'<span style="color:{color};white-space:pre">{safe}</span>'

    # ---- 追加 / 初始化 ----
    def append_log(self, message: str):
        from PySide6.QtGui import QTextCursor
        html_line = self._colorize(message)
        cursor = self.log_output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        # 先显式切一个新段落 block —— 这是 Qt 保证创建新 block 的唯一可靠方式
        # （insertHtml("<p>..") 在已有段落末尾会被降级成 inline 插入）
        cursor.insertBlock()
        cursor.insertHtml(html_line)
        self.log_output.setTextCursor(cursor)
        self._update_status()
        if self._auto_scroll:
            sb = self.log_output.verticalScrollBar()
            sb.setValue(sb.maximum())

    def set_initial_logs(self, logs: str):
        """首次打开时批量载入 —— 每行独立 <p>，Qt 会自动分 block"""
        lines = logs.rstrip("\n").split("\n") if logs.strip() else []
        if not lines:
            self.log_output.clear()
            self._update_status()
            return
        paras = [f"<p>{self._colorize(ln)}</p>" for ln in lines]
        html_doc = (
            '<div style="font-family:Menlo,Consolas,monospace;font-size:12px;'
            'background-color:#0D1117;padding:4px">'
            + "".join(paras)
            + "</div>"
        )
        self.log_output.setHtml(html_doc)
        sb = self.log_output.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._update_status()

    # ---- 工具栏动作 ----
    def _toggle_auto_scroll(self):
        self._auto_scroll = self.auto_scroll_btn.isChecked()
        self.auto_scroll_btn.setText("⏸ 暂停跟随" if self._auto_scroll else "▶ 跟随到底")
        if self._auto_scroll:
            sb = self.log_output.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _on_scroll_changed(self, value: int):
        """用户手动滚动 —— 到底部时恢复自动跟随，否则暂停"""
        sb = self.log_output.verticalScrollBar()
        at_bottom = value >= sb.maximum() - 2
        if at_bottom and not self._auto_scroll:
            self._auto_scroll = True
            self.auto_scroll_btn.setChecked(True)
            self.auto_scroll_btn.setText("⏸ 暂停跟随")
        elif not at_bottom and self._auto_scroll:
            self._auto_scroll = False
            self.auto_scroll_btn.setChecked(False)
            self.auto_scroll_btn.setText("▶ 跟随到底")

    def _toggle_wrap(self):
        from PySide6.QtWidgets import QTextEdit
        wrap = self.wrap_btn.isChecked()
        self.wrap_btn.setText("📜 换行" if wrap else "📜 长行")
        self.log_output.setLineWrapMode(
            QTextEdit.LineWrapMode.WidgetWidth if wrap
            else QTextEdit.LineWrapMode.NoWrap
        )

    def _on_search_changed(self, text: str):
        """搜索：高亮所有匹配行 + 状态栏显示计数"""
        from PySide6.QtGui import QTextCursor, QTextCharFormat
        doc = self.log_output.document()
        highlight_fmt = QTextCharFormat()
        highlight_fmt.setBackground(QColor("#4C2A8F"))
        highlight_fmt.setForeground(QColor("#FFFFFF"))
        default_fmt = QTextCharFormat()
        # 先清掉上次的高亮
        cursor = QTextCursor(doc)
        while True:
            cursor = doc.find(text, cursor)
            if cursor.isNull():
                break
            cursor.mergeCharFormat(default_fmt)
            # 挪到匹配末尾，继续搜
            end = cursor.selectionEnd()
            cursor = QTextCursor(doc)
            cursor.setPosition(end)
        # 重新高亮
        count = 0
        if text.strip():
            cursor = QTextCursor(doc)
            while True:
                cursor = doc.find(text, cursor)
                if cursor.isNull():
                    break
                cursor.mergeCharFormat(highlight_fmt)
                count += 1
                end = cursor.selectionEnd()
                cursor = QTextCursor(doc)
                cursor.setPosition(end)
        if text.strip():
            self.status_label.setText(f"{self._line_count()} 行 · 匹配 {count}")
        else:
            self._update_status()

    def _line_count(self) -> int:
        """blockCount 在 <p> 模式下 = 实际行数 + 1（末尾空 block）"""
        return max(0, self.log_output.document().blockCount() - 1)

    def _copy_all(self):
        QApplication.clipboard().setText(self.log_output.toPlainText())
        self.status_label.setText(f"已复制到剪贴板 · {self._line_count()} 行")

    def _export_to_file(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出任务日志", f"task_{self.task_id}.log", "日志文件 (*.log);;文本文件 (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_output.toPlainText())
            self.status_label.setText(f"已导出到 {path}")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def _clear(self):
        self.log_output.clear()
        self._update_status()

    # ---- 辅助 ----
    def _update_status(self):
        sb = self.log_output.verticalScrollBar()
        bottom = "（跟随中）" if self._auto_scroll else "（已暂停）"
        self.status_label.setText(f"{self._line_count()} 行 {bottom}")


class FormatPreviewWorker(QObject):
    """后台线程：调用 yt-dlp extract_info 解析可用格式"""

    finished = Signal(dict)  # info_dict 或 None（成功）
    error = Signal(str)  # 失败原因

    def __init__(
        self, url: str, proxy: str | None, impersonate: str | None, cookie_browser: str | None
    ) -> None:
        super().__init__()
        self.url = url
        self.proxy = proxy
        self.impersonate = impersonate
        self.cookie_browser = cookie_browser

    @Slot()
    def run(self) -> None:
        try:
            import gc
            import yt_dlp
            from yt_dlp.networking.impersonate import ImpersonateTarget

            opts: dict[str, Any] = {
                "quiet": True,
                "skip_download": True,
                "writesubtitles": False,
                "writeautomaticsub": False,
                "merge_output_format": None,
                "socket_timeout": 30,
                "retries": 3,
                "nocheckcertificate": True,
                # playlist 不要递归深入每个子项（会把所有子项的 formats/thumbnails/description 全拉进来，
                # 一条 B 站 bangumi 可能吃掉几百 MB，十几条就是 GB 级 RSS）。
                # lazy_playlist 只拉目录树（id + url + title），不深挖子项 format。
                "lazy_playlist": True,
                "ignoreerrors": True,
            }
            if self.proxy:
                opts["proxy"] = self.proxy
            if self.cookie_browser:
                opts["cookiesfrombrowser"] = (self.cookie_browser,)
            if self.impersonate:
                try:
                    opts["impersonate"] = ImpersonateTarget.from_str(self.impersonate)
                except Exception:
                    opts["impersonate"] = self.impersonate

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)

            # lazy_playlist=True 后，playlist entries 里没有 formats（只有目录树 id+url+title）。
            # 手动对第一条有 url 的 entry 深入 resolve 一次，把它的 formats 塞回顶层
            # → 底部视频/音频轨道表就有数据了
            if info.get("_type") == "playlist":
                entries = info.get("entries") or []
                # 找一条有效 entry
                probe_url: str | None = None
                for e in entries:
                    if not e:
                        continue
                    if e.get("url"):
                        probe_url = e["url"]
                        break
                    if e.get("_type") == "url" and e.get("webpage_url"):
                        probe_url = e["webpage_url"]
                        break
                if probe_url:
                    try:
                        # 新建一个 YoutubeDL 实例单独 resolve 这一条
                        probe_opts = dict(opts)
                        # resolve 一条时关闭 lazy_playlist
                        probe_opts.pop("lazy_playlist", None)
                        with yt_dlp.YoutubeDL(probe_opts) as pydl:
                            probe_info = pydl.extract_info(probe_url, download=False)
                        # 把 formats 复制回顶层 info（如果顶层还没有）
                        if not info.get("formats") and probe_info.get("formats"):
                            info["formats"] = probe_info["formats"]
                        # 顺手给对应的 entry 也补上 formats（不影响整体 RSS，只有 1 条）
                        for e in entries:
                            if e and str(e.get("id")) == str(probe_info.get("id")):
                                e["formats"] = probe_info.get("formats", [])
                                break
                        _shrink_info(probe_info)
                        probe_info = None
                    except Exception:
                        # probe 失败不影响主流程
                        pass

            # 精简 info：去掉大字段，避免 moveToThread 序列化时占内存
            _shrink_info(info)

            self.finished.emit(info)
            # emit 后立即清掉 worker 里的引用 + 强制 GC，防止 RSS 被重复持有
            info = None
            gc.collect()
        except Exception as e:
            self.error.emit(str(e))


def _format_filesize(n: int | float | None) -> str:
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# 要删掉的大字段（它们不影响我们展示格式/条目列表）
# 注意：formats 字段**保留**——底部视频/音频轨道表就是靠它
_BLOAT_KEYS = {
    "description", "thumbnails", "automatic_captions",
    "subtitles", "heatmap", "comments", "chapters",
    "requested_downloads", "requested_formats", "live_status",
}


def _shrink_info(info: Any) -> None:
    """递归删除 yt-dlp extract_info 返回 dict 里不展示/不需要的大字段，
    避免 moveToThread 序列化时占掉几百 MB 甚至 GB RSS。"""
    if isinstance(info, dict):
        for k in list(info.keys()):
            if k in _BLOAT_KEYS:
                info.pop(k, None)
            else:
                _shrink_info(info[k])
    elif isinstance(info, list):
        for item in info:
            _shrink_info(item)


FORMAT_HELP_HTML: str = """
<div style="font-family:Menlo,Consolas,monospace;font-size:12px;color:#E6EDF3;line-height:1.55;">

<h3 style="margin:0 0 6px 0;color:#58A6FF;">🎬 视频编码对照表（vcodec 列）</h3>

<table cellspacing="0" cellpadding="4" style="width:100%;border-collapse:collapse;margin-bottom:10px;">
<tr style="background:#161B22;">
<td style="border-bottom:1px solid #30363D;color:#FF7B72;"><b>编码</b></td>
<td style="border-bottom:1px solid #30363D;color:#E6EDF3;"><b>全称 / 中文名</b></td>
<td style="border-bottom:1px solid #30363D;color:#E6EDF3;"><b>同画质体积</b></td>
<td style="border-bottom:1px solid #30363D;color:#E6EDF3;"><b>兼容 / 备注</b></td>
<td style="border-bottom:1px solid #30363D;color:#86EFAC;"><b>推荐场景</b></td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;color:#FFA657;">avc1 / h264</td>
<td style="border-bottom:1px solid #21262D;">H.264 / AVC</td>
<td style="border-bottom:1px solid #21262D;">基准 (100%)</td>
<td style="border-bottom:1px solid #21262D;">⭐⭐⭐⭐⭐ 全平台</td>
<td style="border-bottom:1px solid #21262D;">通用、老设备兼容优先</td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;color:#D2A8FF;">hvc1 / hevc</td>
<td style="border-bottom:1px solid #21262D;">H.265 / HEVC</td>
<td style="border-bottom:1px solid #21262D;">↓35%</td>
<td style="border-bottom:1px solid #21262D;">⭐⭐⭐ 主流播放</td>
<td style="border-bottom:1px solid #21262D;">体积更小、画质相当</td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;color:#FFD93D;"><b>dvhe / dvh1</b></td>
<td style="border-bottom:1px solid #21262D;">H.265 + <b>杜比视界</b><br>(Dolby Vision)</td>
<td style="border-bottom:1px solid #21262D;">同 HEVC</td>
<td style="border-bottom:1px solid #21262D;">⭐⭐ 需支持杜比视界的<br>播放设备/软件</td>
<td style="border-bottom:1px solid #21262D;">B 站大会员 4K 常见<br>（轨表带 ★杜比）</td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;color:#A5D6FF;"><b>av01</b></td>
<td style="border-bottom:1px solid #21262D;">AV1（AOMedia Video 1）</td>
<td style="border-bottom:1px solid #21262D;"><b>↓50%</b></td>
<td style="border-bottom:1px solid #21262D;">⭐⭐⭐ Mac 需 M1+ / macOS 13+ 硬解</td>
<td style="border-bottom:1px solid #21262D;"><b>归档首选</b>，体积最小</td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;color:#79C0FF;">vp09</td>
<td style="border-bottom:1px solid #21262D;">VP9</td>
<td style="border-bottom:1px solid #21262D;">↓25%</td>
<td style="border-bottom:1px solid #21262D;">Chrome 系好</td>
<td style="border-bottom:1px solid #21262D;">YouTube 平台</td>
</tr>
</table>

<p style="margin:0 0 10px 0;padding:8px 10px;background:#161B22;border-left:3px solid #FFD93D;border-radius:3px;color:#C9D1D9;">
<b style="color:#FFD93D;">🌟 关于 dvhe / dvh1（杜比视界）</b><br>
本质上仍是 H.265/HEVC 码流，只是额外携带了一层 <b>RPU 动态元数据</b>（逐帧的亮度/色彩映射信息），所以体积和同规格 HEVC 基本一样。<br>
<b>播放环境不支持杜比视界时，RPU 被忽略，画面会按错误色域渲染（发绿 / 发紫 / 泛灰）</b>——这不是文件损坏。没有杜比视界显示设备或把握不准播放环境时，直接选不带 ★杜比 的 hev1 / hvc1 轨即可。
</p>

<h3 style="margin:8px 0 6px 0;color:#58A6FF;">🔊 音频编码（acodec 列）</h3>

<table cellspacing="0" cellpadding="4" style="width:100%;border-collapse:collapse;margin-bottom:10px;">
<tr style="background:#161B22;">
<td style="border-bottom:1px solid #30363D;color:#FF7B72;"><b>编码</b></td>
<td style="border-bottom:1px solid #30363D;color:#E6EDF3;"><b>中文名</b></td>
<td style="border-bottom:1px solid #30363D;color:#E6EDF3;"><b>备注</b></td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;color:#7EE787;"><b>mp4a.40.2</b></td>
<td style="border-bottom:1px solid #21262D;">AAC-LC</td>
<td style="border-bottom:1px solid #21262D;"><b>B 站唯一音轨</b>。128kbps 已是最佳选择，再高听不到区别</td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;color:#C9B3FF;">mp4a.40.5</td>
<td style="border-bottom:1px solid #21262D;">HE-AAC v2</td>
<td style="border-bottom:1px solid #21262D;">高效 AAC，23.9kbps 抵 AAC-LC 64kbps（B 站不用）</td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;color:#E6EDF3;">opus</td>
<td style="border-bottom:1px solid #21262D;">Opus</td>
<td style="border-bottom:1px solid #21262D;">YouTube 常用，低码率音质更好</td>
</tr>
</table>

<h3 style="margin:8px 0 6px 0;color:#58A6FF;">📦 容器 ext（文件后缀）</h3>

<table cellspacing="0" cellpadding="4" style="width:100%;border-collapse:collapse;margin-bottom:10px;">
<tr style="background:#161B22;">
<td style="border-bottom:1px solid #30363D;color:#FF7B72;"><b>ext</b></td>
<td style="border-bottom:1px solid #30363D;color:#E6EDF3;"><b>容器</b></td>
<td style="border-bottom:1px solid #30363D;color:#86EFAC;"><b>说明</b></td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;color:#7EE787;"><b>mp4</b></td>
<td style="border-bottom:1px solid #21262D;">MP4</td>
<td style="border-bottom:1px solid #21262D;"><b>B 站唯一容器</b>。最通用，全设备播放</td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;color:#E6EDF3;">m4a</td>
<td style="border-bottom:1px solid #21262D;">M4A</td>
<td style="border-bottom:1px solid #21262D;">B 站音轨格式；ffmpeg 合成 mp4 时无损 remux 进去</td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;color:#E6EDF3;">webm</td>
<td style="border-bottom:1px solid #21262D;">WebM</td>
<td style="border-bottom:1px solid #21262D;">YouTube 常用，Chrome 原生支持好</td>
</tr>
</table>

<h3 style="margin:8px 0 6px 0;color:#58A6FF;"> 怎么选？推荐组合</h3>

<table cellspacing="0" cellpadding="4" style="width:100%;border-collapse:collapse;">
<tr style="background:#161B22;">
<td style="border-bottom:1px solid #30363D;color:#FF7B72;"><b>需求</b></td>
<td style="border-bottom:1px solid #30363D;color:#E6EDF3;"><b>选 编码</b></td>
<td style="border-bottom:1px solid #30363D;color:#E6EDF3;"><b>选 分辨率</b></td>
<td style="border-bottom:1px solid #30363D;color:#86EFAC;"><b>理由</b></td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;">体积最小</td>
<td style="border-bottom:1px solid #21262D;color:#A5D6FF;"><b>av01</b></td>
<td style="border-bottom:1px solid #21262D;">最高可用</td>
<td style="border-bottom:1px solid #21262D;">体积比 H.264 小一半</td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;">均衡推荐</td>
<td style="border-bottom:1px solid #21262D;color:#D2A8FF;"><b>hvc1</b></td>
<td style="border-bottom:1px solid #21262D;">1080p</td>
<td style="border-bottom:1px solid #21262D;">体积 -35%，画质差不多</td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;">最稳兼容</td>
<td style="border-bottom:1px solid #21262D;color:#FFA657;"><b>avc1</b></td>
<td style="border-bottom:1px solid #21262D;">1080p</td>
<td style="border-bottom:1px solid #21262D;">编码快，全设备无压力</td>
</tr>
<tr>
<td style="border-bottom:1px solid #21262D;">动作流畅</td>
<td style="border-bottom:1px solid #21262D;">任一</td>
<td style="border-bottom:1px solid #21262D;"><b>60fps</b></td>
<td style="border-bottom:1px solid #21262D;">运动/动作番剧观感翻倍</td>
</tr>
</table>
</div>
"""


class FormatPreviewDialog(QDialog):
    """格式预览对话框：播放列表时顶部让用户勾选条目，底部让用户挑视频+音频轨道"""

    def __init__(self, info: dict[str, Any], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择下载格式")
        self.resize(1020, 720)
        self.video_format_id: Optional[str] = None
        self.audio_format_id: Optional[str] = None

        # 合集条目（如果是 playlist 类型）
        self._playlist_entries: list[dict[str, Any]] = []
        self._playlist_items_str: str | None = None  # 用户确认后自动生成

        # 判断是不是合集
        entries = info.get("entries") or []
        if info.get("_type") == "playlist" or entries:
            self._playlist_entries = [e for e in entries if e]

        # 格式源：单视频 → 顶层 formats；playlist → 找 entries 里第一条带 formats 的子项
        self._formats: list[dict[str, Any]] = info.get("formats", [])
        if not self._formats and self._playlist_entries:
            for e in self._playlist_entries:
                if e.get("formats"):
                    self._formats = e["formats"]
                    break

        title = info.get("title", "") or info.get("id", "")
        self._build_ui(title, info)
        self._populate_tables(self._formats)
        if self._playlist_entries:
            self._populate_playlist_table()

    # ---------- 自动分组 ----------
    @staticmethod
    def _detect_group(title: str) -> tuple[str, str]:
        """从条目标题里推断 (组标签, 标签颜色) 。
        返回值用于 UI 上的 pill 徽标。"""
        t = title or ""
        # 匹配中配 / 国语配音 / 译制 / 中配版
        if re.search(r"中配|国语|译制|中配版|普通话", t):
            return ("中配版", "#E6A23C")
        # 匹配日语原版 / 原声 / 日语
        if re.search(r"日语|原声|原版|日版|生肉", t):
            return ("日语版", "#2980B9")
        # 匹配花絮 / 预告 / OP / ED / PV
        if re.search(r"花絮|预告|PV|OP|ED|片头曲|片尾曲", t):
            return ("特典", "#8E44AD")
        # 正片兜底
        return ("正片", "#27AE60")

    # ---------- UI ----------
    def _build_ui(self, title: str, info: dict[str, Any]) -> None:
        layout = QVBoxLayout(self)

        meta = QLabel(
            f"<b>{title}</b>  ·  <span style='color:#888'>{info.get('webpage_url', '')}</span>"
        )
        meta.setWordWrap(True)
        layout.addWidget(meta)

        # ====== 合集条目选择区（playlist 才有）======
        if self._playlist_entries:
            playlist_box = QGroupBox(f"合集条目（共 {len(self._playlist_entries)} 条，可勾选）")
            pl_layout = QVBoxLayout()

            # 工具栏：全选 / 反选 / 关键词过滤
            bar = QHBoxLayout()
            self._pl_filter_input = QLineEdit()
            self._pl_filter_input.setPlaceholderText("关键词过滤（留空显示全部），例如：正片 / 中配 / 波涛")
            self._pl_filter_input.textChanged.connect(self._on_pl_filter)
            bar.addWidget(self._pl_filter_input, 1)

            btn_all = QPushButton("全选")
            btn_all.clicked.connect(lambda: self._pl_check_all(True))
            bar.addWidget(btn_all)
            btn_none = QPushButton("全不选")
            btn_none.clicked.connect(lambda: self._pl_check_all(False))
            bar.addWidget(btn_none)
            btn_invert = QPushButton("反选")
            btn_invert.clicked.connect(self._pl_invert)
            bar.addWidget(btn_invert)
            pl_layout.addLayout(bar)

            self._pl_summary = QLabel("")
            self._pl_summary.setStyleSheet("color:#AAA; font-size:11px;")
            pl_layout.addWidget(self._pl_summary)

            self.playlist_table = self._make_playlist_table()
            self.playlist_table.itemChanged.connect(self._on_pl_item_changed)
            pl_layout.addWidget(self.playlist_table, 1)

            playlist_box.setLayout(pl_layout)
            layout.addWidget(playlist_box, 2)  # playlist 占 2 份，下面视频轨道占 1 份

        # ====== 视频 / 音频轨道 ======
        grid = QGridLayout()
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.addWidget(QLabel("视频轨道"), 0, 0)
        grid.addWidget(QLabel("音频轨道"), 0, 1)
        self.video_table = self._make_format_table("video")
        self.audio_table = self._make_format_table("audio")
        # 防止表格被挤得太窄导致分辨率/采样率列看不见
        self.video_table.setMinimumWidth(340)
        self.audio_table.setMinimumWidth(340)
        grid.addWidget(self.video_table, 1, 0)
        grid.addWidget(self.audio_table, 1, 1)
        layout.addLayout(grid, 1)

        # ====== 格式帮助面板 ======
        # 默认收起，点标题展开；字体稍小 + GitHub Dark 配色跟日志面板呼应
        from PySide6.QtWidgets import QTextBrowser
        from PySide6.QtGui import QFont

        help_toggle = QLabel(
            '<span style="color:#8B949E;">💡 <a href="show_format_help" style="color:#58A6FF;">'
            '看不懂 avc1 / hvc1 / dvhe / av01？展开格式字典 ▾</a></span>'
        )
        help_toggle.setOpenExternalLinks(False)
        help_toggle.linkActivated.connect(lambda _: self._toggle_format_help(True))
        self._help_toggle = help_toggle
        layout.addWidget(help_toggle)

        self._help_browser = QTextBrowser()
        self._help_browser.setOpenExternalLinks(True)
        self._help_browser.setFont(
            QFont("Menlo", 10) if os.name != "nt" else QFont("Consolas", 10)
        )
        self._help_browser.setStyleSheet(
            "QTextBrowser { background-color: #0D1117; color: #E6EDF3; "
            "border: 1px solid #30363D; border-radius: 4px; padding: 8px; }"
        )
        self._help_browser.setHtml(FORMAT_HELP_HTML)
        self._help_browser.setFixedHeight(0)  # 默认收起
        self._help_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self._help_browser)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("确认下载")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._btns = btns
        self._update_ok_enabled()

    def _toggle_format_help(self, show: bool | None = None) -> None:
        """展开/收起格式帮助面板"""
        if show is None:
            show = self._help_browser.height() == 0
        self._help_browser.setFixedHeight(320 if show else 0)
        if show:
            self._help_toggle.setText(
                '<span style="color:#8B949E;">💡 <a href="hide_format_help" style="color:#58A6FF;">'
                '收起格式字典 ▴</a></span>'
            )
            self._help_toggle.linkActivated.connect(lambda _: self._toggle_format_help(False))
        else:
            self._help_toggle.setText(
                '<span style="color:#8B949E;">💡 <a href="show_format_help" style="color:#58A6FF;">'
                '看不懂 avc1 / hvc1 / dvhe / av01？展开格式字典 ▾</a></span>'
            )
            self._help_toggle.linkActivated.connect(lambda _: self._toggle_format_help(True))

    def _make_format_table(self, kind: str = "video") -> QTableWidget:
        """轨道表。kind='video' 显示分辨率/帧率，kind='audio' 显示采样率/码率"""
        if kind == "audio":
            headers = ["格式 ID", "编码", "采样率", "码率", "大小"]
        else:
            headers = ["格式 ID", "分辨率", "帧率", "编码", "大小"]
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setAlternatingRowColors(True)
        # 格式 ID 列：固定宽度 90px（含 DoVi 标记也够），不参与挤压
        t.setColumnWidth(0, 90)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        # 第 2 列（视频=分辨率 / 音频=编码）：Stretch 吃掉剩余空间
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # 其余列：按内容自适应
        t.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        t.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        t.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        return t

    def _make_playlist_table(self) -> QTableWidget:
        """合集条目表（checkable + 分组标签）"""
        t = QTableWidget(0, 4)
        t.setHorizontalHeaderLabels(["", "组", "标题", "ID"])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setAlternatingRowColors(True)
        t.setColumnWidth(0, 36)   # checkbox 列
        t.setColumnWidth(1, 80)   # 组标签列
        t.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        t.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        return t

    # ---------- 填 playlist ----------
    def _populate_playlist_table(self) -> None:
        """把所有条目塞进 playlist_table，默认全选；组标签按自动检测标色"""
        self.playlist_table.blockSignals(True)
        self.playlist_table.clearContents()
        self.playlist_table.setRowCount(0)
        self._pl_all_indices: list[int] = []  # 行号 ↔ entries 下标 映射

        keyword = self._pl_filter_input.text().strip().lower() if hasattr(self, "_pl_filter_input") else ""
        visible = 0
        for i, e in enumerate(self._playlist_entries):
            title = e.get("title") or f"条目 #{i+1}"
            if keyword and keyword not in title.lower():
                continue  # 被关键词过滤掉
            group, color = self._detect_group(title)
            row = self.playlist_table.rowCount()
            self.playlist_table.insertRow(row)

            # 列 0：checkbox（默认全选）
            cb_item = QTableWidgetItem()
            cb_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            cb_item.setCheckState(Qt.CheckState.Checked)
            self.playlist_table.setItem(row, 0, cb_item)

            # 列 1：组标签（带颜色背景）
            group_item = QTableWidgetItem(group)
            group_item.setForeground(QBrush(QColor("white")))
            group_item.setBackground(QBrush(QColor(color)))
            group_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.playlist_table.setItem(row, 1, group_item)

            # 列 2：标题
            self.playlist_table.setItem(row, 2, QTableWidgetItem(title))

            # 列 3：yt-dlp index（注意是 1-based）
            self.playlist_table.setItem(row, 3, QTableWidgetItem(str(i + 1)))

            self._pl_all_indices.append(i)
            visible += 1

        self.playlist_table.blockSignals(False)
        self._update_pl_summary()

    # ---------- playlist 工具栏回调 ----------
    def _on_pl_filter(self, text: str) -> None:
        """关键词变化 → 重新填充（保留勾选逻辑：过滤掉的条目不影响总勾选结果）"""
        self._populate_playlist_table()

    def _pl_check_all(self, checked: bool) -> None:
        self.playlist_table.blockSignals(True)
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for r in range(self.playlist_table.rowCount()):
            item = self.playlist_table.item(r, 0)
            if item:
                item.setCheckState(state)
        self.playlist_table.blockSignals(False)
        self._update_pl_summary()

    def _pl_invert(self) -> None:
        self.playlist_table.blockSignals(True)
        for r in range(self.playlist_table.rowCount()):
            item = self.playlist_table.item(r, 0)
            if item:
                cur = item.checkState()
                item.setCheckState(Qt.CheckState.Unchecked if cur == Qt.CheckState.Checked else Qt.CheckState.Checked)
        self.playlist_table.blockSignals(False)
        self._update_pl_summary()

    def _on_pl_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._update_pl_summary()

    def _update_pl_summary(self) -> None:
        """更新底部文字：已选 X / Y 条"""
        checked = sum(
            1 for r in range(self.playlist_table.rowCount())
            if (self.playlist_table.item(r, 0).checkState() == Qt.CheckState.Checked)
        )
        total = self.playlist_table.rowCount()
        # 顺便把选中的条目序号（1-based）转成 yt-dlp playlist_items 格式
        selected_indices_1based: list[int] = []
        for r in range(total):
            if self.playlist_table.item(r, 0).checkState() == Qt.CheckState.Checked:
                selected_indices_1based.append(r + 1)
        self._pl_summary.setText(
            f"已选 <b>{checked}</b> / {total} 条  |  "
            f"yt-dlp playlist_items: <code>{self._compress_range(selected_indices_1based) or '(全部)'}</code>"
        )

    @staticmethod
    def _compress_range(nums: list[int]) -> str:
        """把 [1,3,4,5,8] 压缩成 '1,3-5,8'"""
        if not nums:
            return ""
        nums = sorted(nums)
        parts: list[str] = []
        start = prev = nums[0]
        for n in nums[1:]:
            if n == prev + 1:
                prev = n
            else:
                parts.append(f"{start}-{prev}" if start != prev else str(start))
                start = prev = n
        parts.append(f"{start}-{prev}" if start != prev else str(start))
        return ",".join(parts)

    @staticmethod
    def _expand_range(s: Optional[str]) -> Optional[set[int]]:
        """把 '1,3-5,8' 展开成 {1,3,4,5,8}；None/空返回 None（表示全部）"""
        if not s:
            return None
        result: set[int] = set()
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, _, b = part.partition("-")
                try:
                    result.update(range(int(a), int(b) + 1))
                except ValueError:
                    continue
            else:
                try:
                    result.add(int(part))
                except ValueError:
                    continue
        return result or None

    @staticmethod
    def extract_playlist_entries(
        url: str,
        proxy: Optional[str] = None,
        impersonate: Optional[str] = None,
        cookie_browser: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """轻量 pre-parse：只拿 playlist 目录树（entries 的 url + title），不拉 formats。
        返回的每个 entry 保证有 'url' 或 'webpage_url'（兜底两个字段）。"""
        import yt_dlp
        from yt_dlp.networking.impersonate import ImpersonateTarget

        opts: dict[str, Any] = {
            "quiet": True,
            "skip_download": True,
            "extract_flat": True,   # 只拉目录树，毫秒级
            "lazy_playlist": False, # extract_flat 需要解析目录，不能 lazy
            "writesubtitles": False,
            "writeautomaticsub": False,
            "socket_timeout": 15,
            "retries": 2,
            "nocheckcertificate": True,
            "ignoreerrors": True,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookie_browser:
            opts["cookiesfrombrowser"] = (cookie_browser,)
        if impersonate:
            try:
                opts["impersonate"] = ImpersonateTarget.from_str(impersonate)
            except Exception:
                opts["impersonate"] = impersonate

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        raw = info.get("entries") or []
        result: list[dict[str, Any]] = []
        for entry in raw:
            if not entry:
                continue
            # extract_flat 模式下 entry 结构：{_type:'url', id, url, webpage_url, title}
            ep_url = entry.get("webpage_url") or entry.get("url") or ""
            if not ep_url and entry.get("_type") == "url":
                # 某些 extractor（BilibiliBangumi）extract_flat 只给 id 不给 url
                # 构造兜底 URL（bangumi 从 media 页面来的话，entry.id 是 ep_id）
                ep_id = entry.get("id", "")
                if ep_id:
                    ep_url = f"{url.split('?')[0]}?ep_id={ep_id}"
            result.append({
                "url": ep_url,
                "title": entry.get("title") or f"条目 #{entry.get('_playlist_index', len(result)+1)}",
                "_playlist_index": entry.get("_playlist_index", len(result) + 1),
            })
        return result

    @staticmethod
    def explode_playlist_to_tasks(
        url: str,
        proxy: Optional[str],
        impersonate: Optional[str],
        cookie_browser: Optional[str],
        playlist_items_str: Optional[str],
        **task_kwargs,
    ) -> list["DownloadTask"]:
        """把一条 playlist URL explode 成多条独立 DownloadTask。

        Args:
            playlist_items_str: '1,3-5,8' → 只建这几条；None → 建全部
            **task_kwargs: 透传给 DownloadTask（save_path, format_preset, ...）
        """
        from .models import DownloadTask

        entries = FormatPreviewDialog.extract_playlist_entries(
            url, proxy=proxy, impersonate=impersonate, cookie_browser=cookie_browser,
        )
        if not entries:
            return []

        # 过滤：_playlist_items_str 是 1-based 下标集合
        selected = FormatPreviewDialog._expand_range(playlist_items_str)
        selected_entries = entries if selected is None else [
            e for e in entries if e["_playlist_index"] in selected
        ]

        tasks: list[DownloadTask] = []
        for e in selected_entries:
            if not e["url"]:
                continue
            task = DownloadTask(
                url=e["url"],
                title=e["title"],  # 预填 title，跳过解析
                download_playlist=False,
                playlist_items=None,
                **task_kwargs,
            )
            tasks.append(task)
        return tasks


    # ---------- 填 format ----------
    def _populate_tables(self, formats: list[dict[str, Any]]) -> None:
        videos = [f for f in formats if (f.get("vcodec") or "") != "none"]
        audios = [
            f
            for f in formats
            if (f.get("acodec") or "") != "none" and (f.get("vcodec") or "") == "none"
        ]

        # 视频表：按 height 降序 + is_dovi 标记
        videos.sort(key=lambda f: int(str(f.get("height") or 0)), reverse=True)
        # 音频表：按 abr 降序
        audios.sort(key=lambda f: float(str(f.get("abr") or 0)), reverse=True)

        def _video_row(table: QTableWidget, f: dict[str, Any]) -> None:
            row = table.rowCount()
            table.insertRow(row)
            dyn = f.get("dynamic_range") or ""
            note = f.get("format_note") or ""
            dovi = dyn == "dv" or "dovi" in note.lower()
            hdr = dyn and dyn != "dv" and dyn != "sdr"
            res = f.get("resolution") or f"{f.get('width', '')}x{f.get('height', '')}"
            fps = f.get("fps")
            codec = f.get("vcodec") or ""
            if dovi:
                codec = codec + " (DoVi)"
            elif hdr:
                codec = codec + f" (HDR {dyn.upper()})"
            size = f.get("filesize") or f.get("filesize_approx")
            table.setItem(
                row, 0, QTableWidgetItem(str(f.get("format_id", "")) + ("  ★杜比" if dovi else ""))
            )
            table.setItem(row, 1, QTableWidgetItem(str(res)))
            table.setItem(row, 2, QTableWidgetItem(f"{fps:g}fps" if fps else "—"))
            table.setItem(row, 3, QTableWidgetItem(str(codec)))
            table.setItem(row, 4, QTableWidgetItem(_format_filesize(size)))

        def _audio_row(table: QTableWidget, f: dict[str, Any]) -> None:
            row = table.rowCount()
            table.insertRow(row)
            acodec = f.get("acodec") or ""
            # 采样率：yt-dlp 给的是 Hz → 转 kHz（44100 → "44.1 kHz"，48000 → "48 kHz"）
            asr = f.get("asr")
            if asr:
                khz = asr / 1000
                asr_khz = f"{khz:.1f}".rstrip("0").rstrip(".") + " kHz"
            else:
                asr_khz = "—"
            # 码率
            abr = f.get("abr")
            abr_str = f"{int(abr)} kbps" if abr else "—"
            size = f.get("filesize") or f.get("filesize_approx")
            table.setItem(row, 0, QTableWidgetItem(str(f.get("format_id", ""))))
            table.setItem(row, 1, QTableWidgetItem(str(acodec)))
            table.setItem(row, 2, QTableWidgetItem(asr_khz))
            table.setItem(row, 3, QTableWidgetItem(abr_str))
            table.setItem(row, 4, QTableWidgetItem(_format_filesize(size)))

        for f in videos:
            _video_row(self.video_table, f)
        for f in audios:
            _audio_row(self.audio_table, f)

        self.video_table.itemSelectionChanged.connect(self._update_ok_enabled)
        self.audio_table.itemSelectionChanged.connect(self._update_ok_enabled)

        # 音轨默认选中最高码率那条（audios 已按 abr 降序，第 0 行即最佳）；
        # 视频轨保持不选，强制用户确认分辨率/DoVi，避免误下超大文件
        if self.audio_table.rowCount() > 0:
            self.audio_table.selectRow(0)

    # ---------- 选行 ----------
    def _selected_format_id(self, table: QTableWidget) -> Optional[str]:
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, 0)
        if not item:
            return None
        return item.text().split()[0]  # 去掉 " ★杜比"

    def _update_ok_enabled(self) -> None:
        fmt_ok = self.video_table.currentRow() >= 0 and self.audio_table.currentRow() >= 0
        # 如果有 playlist，至少得勾一条
        if self._playlist_entries:
            pl_checked = any(
                self.playlist_table.item(r, 0).checkState() == Qt.CheckState.Checked
                for r in range(self.playlist_table.rowCount())
            )
            fmt_ok = fmt_ok and pl_checked
        self._btns.button(QDialogButtonBox.StandardButton.Ok).setEnabled(fmt_ok)

    def _on_ok(self) -> None:
        self.video_format_id = self._selected_format_id(self.video_table)
        self.audio_format_id = self._selected_format_id(self.audio_table)
        if self._playlist_entries:
            # 生成 playlist_items 字符串（yt-dlp 原生支持）
            selected_1based: list[int] = []
            for r in range(self.playlist_table.rowCount()):
                if self.playlist_table.item(r, 0).checkState() == Qt.CheckState.Checked:
                    selected_1based.append(r + 1)
            if 0 < len(selected_1based) < self.playlist_table.rowCount():
                self._playlist_items_str = self._compress_range(selected_1based)
            else:
                self._playlist_items_str = None  # 全部 or 全不选 → 不写，让 yt-dlp 用默认行为
        if self.video_format_id and self.audio_format_id:
            self.accept()

    def get_format_spec(self) -> str:
        """给 yt-dlp format 参数用的字符串，例如 '30080+30280'"""
        if self.video_format_id and self.audio_format_id:
            return f"{self.video_format_id}+{self.audio_format_id}"
        return ""

    def _find_format(self, fid: Optional[str]) -> Optional[dict[str, Any]]:
        """按 format_id 在本次展示的格式列表里找回原始 format dict"""
        if not fid:
            return None
        for f in self._formats:
            if str(f.get("format_id")) == fid:
                return f
        return None

    # 同一编码在不同容器/平台上报的 FourCC 互通家族
    # （selector 正则值含 | 时必须用双引号包裹，见 yt-dlp YoutubeDL.py L2242）
    _CODEC_FAMILY_PATTERNS: dict[str, str] = {
        "hev1": r"(hev1|hvc1)",
        "hvc1": r"(hev1|hvc1)",
        "dvhe": r"(dvhe|dvh1)",
        "dvh1": r"(dvhe|dvh1)",
    }

    def get_playlist_format_spec(self) -> str:
        """playlist 场景的 format spec：保留选轨的编码/分辨率意图，但不硬绑 format_id。

        各分集 format_id 可能不同（甚至缺失），所以按优先级组成 '/' fallback 链：
          1. 用户选的原始 id 对（B站番剧各集 id 通常一致，命中最精确）
          2. 同编码家族 + 同分辨率 + 同容器
          3. 同编码家族 + 同容器
          4. 同编码家族（任意容器）
          5. 尾级兜底：非杜比轨用排除 dvhe/dvh1 的兜底，杜比轨才用通用兜底
        yt-dlp 对 '/' 候选按分集逐个尝试：id 在某集不存在时自动落到特征候选，
        因此选了普通 HEVC（hev1）绝不会误选杜比视界（dvhe/dvh1）。
        """
        id_spec = self.get_format_spec()
        vid = self._find_format(self.video_format_id)
        if not vid:
            return id_spec or DEFAULT_FORMAT
        prefix = str(vid.get("vcodec") or "").split(".")[0].lower()
        family = self._CODEC_FAMILY_PATTERNS.get(prefix, prefix)  # avc1/av01/vp09 直接用
        if not family:
            # 少数预合成流（如 B站番剧预览 format 32）不报 vcodec → 只保留 id + 通用兜底
            return f"{id_spec}/{DEFAULT_FORMAT}" if id_spec else DEFAULT_FORMAT

        is_dovi_choice = prefix in ("dvhe", "dvh1")
        height = vid.get("height")
        vext = vid.get("ext") or "mp4"
        aid = self._find_format(self.audio_format_id)
        aext = (aid.get("ext") if aid else "") or "m4a"

        candidates: list[str] = []
        if id_spec:
            candidates.append(id_spec)
        v_levels: list[str] = []
        if height:
            v_levels.append(f'bestvideo[vcodec~="{family}"][height={height}][ext={vext}]')
        v_levels.append(f'bestvideo[vcodec~="{family}"][ext={vext}]')
        v_levels.append(f'bestvideo[vcodec~="{family}"]')
        # 每级展开两条完整合并候选，避免 'v+bestaudio[ext=x]/bestaudio'
        # 被解析成 (v+bestaudio[ext=x])/bestaudio
        for v in v_levels:
            candidates.append(f"{v}+bestaudio[ext={aext}]")
            candidates.append(f"{v}+bestaudio")
        # 选的是非杜比轨时，尾级兜底也排除杜比视界，防止该编码在某分集缺失时滑到 dvhe
        candidates.append(DEFAULT_FORMAT if is_dovi_choice else FALLBACK_FORMAT_NO_DOVI)
        return "/".join(candidates)

    def get_playlist_items_str(self) -> Optional[str]:
        """给 yt-dlp --playlist-items 用的字符串，例如 '1,3-5,8'；None = 全部"""
        return self._playlist_items_str

    def get_human_label(self) -> str:
        """给用户看的人类可读标签，例如 'DoVi 3840x2160 HEVC / AAC 192kbps'"""
        vid = self._find_format(self.video_format_id)
        aid = self._find_format(self.audio_format_id)
        if not vid or not aid:
            return self.get_format_spec()
        res = vid.get("resolution") or f"{vid.get('width', '')}x{vid.get('height', '')}"
        vcodec = (vid.get("vcodec") or "").split(".")[0].upper()
        acodec = (aid.get("acodec") or "").split(".")[0].upper()
        abr = aid.get("abr") or 0
        dyn = vid.get("dynamic_range") or ""
        dovi = dyn == "dv" or "dovi" in (vid.get("format_note") or "").lower()
        prefix = "DoVi " if dovi else ("HDR " if dyn and dyn != "dv" and dyn != "sdr" else "")
        return f"{prefix}{res} {vcodec} / {acodec} {int(abr) if abr else '?'}kbps"


class AddTaskDialog(QDialog):
    def __init__(self, parent=None, initial_url: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("添加下载任务")
        self.setMinimumWidth(600)
        # 用上次保存目录，没有则回退到系统下载目录
        self.selected_download_path = get_last_save_path()
        self._format_spec: str = ""  # 用户手动选的 format_id+format_id
        self._playlist_format_spec: str = ""  # playlist 用：id 首选 + 编码特征 fallback 链
        self._format_human_label: str = ""  # 显示给用户看的格式描述
        self._playlist_items_str: Optional[str] = None  # FormatPreviewDialog 返回的勾选压缩串
        self._parse_thread: Optional[QThread] = None
        self._parse_worker: Optional[FormatPreviewWorker] = None
        self._setup_ui()
        self._try_autofill_clipboard(initial_url)

    def _get_default_download_path(self) -> str:
        path = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        return path if path and os.path.exists(path) else "."

    def _try_autofill_clipboard(self, initial_url: str | None = None) -> None:
        """如果传入了初始 URL 则使用它，否则尝试从系统剪贴板自动填入有效 URL"""
        if initial_url and initial_url.strip():
            self.url_input.setText(initial_url.strip())
            self.url_input.selectAll()
            return

        clipboard = QApplication.clipboard()
        text = clipboard.text().strip() if clipboard else ""
        if text.startswith(("http://", "https://", "www.")):
            self.url_input.setText(text)
            self.url_input.selectAll()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # URL Input + 解析按钮
        url_row = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("粘贴视频链接...")
        self.url_input.returnPressed.connect(self._on_parse_clicked)
        self.url_input.textChanged.connect(self._on_url_text_changed)
        url_row.addWidget(self.url_input, 1)
        self.parse_btn = QPushButton(qta.icon("fa5s.list-alt", color="#FFFFFF"), "  解析格式")
        self.parse_btn.setMinimumWidth(120)
        self.parse_btn.clicked.connect(self._on_parse_clicked)
        url_row.addWidget(self.parse_btn)
        layout.addWidget(QLabel("视频链接:"))
        layout.addLayout(url_row)

        self.parse_progress = QProgressBar()
        self.parse_progress.setRange(0, 0)
        self.parse_progress.setVisible(False)
        self.parse_progress.setFixedHeight(4)
        self.parse_progress.setTextVisible(False)
        layout.addWidget(self.parse_progress)

        # Download Options
        options_group = QGroupBox("下载选项")
        options_layout = QGridLayout()

        self.format_combo = QComboBox()
        self.format_combo.setView(QListView())
        # 初始只放 preset 选项，解析后如果用户选了具体 format_id 会在头部插入"已选具体格式"项
        # 杜比视界预设仅 B 站链接显示（URL 变化时 _on_url_text_changed 会重建列表）
        self._format_preset_items = self._preset_items_for_url(self.url_input.text())
        self.format_combo.addItems(self._format_preset_items)

        options_layout.addWidget(QLabel("下载格式:"), 0, 0)
        options_layout.addWidget(self.format_combo, 0, 1)

        self.proxy_input = QLineEdit()
        self.proxy_input.setPlaceholderText("例如: http://127.0.0.1:7890")
        options_layout.addWidget(QLabel("HTTP 代理:"), 0, 2)
        options_layout.addWidget(self.proxy_input, 0, 3)

        self.concurrent_input = QLineEdit()
        self.concurrent_input.setPlaceholderText("并发数 (1-16)，默认5")
        options_layout.addWidget(QLabel("并发片段数:"), 1, 0)
        options_layout.addWidget(self.concurrent_input, 1, 1)

        self.impersonate_combo = QComboBox()
        self.impersonate_combo.setView(QListView())
        self.impersonate_combo.addItems(["无", "chrome", "firefox", "edge", "safari"])
        options_layout.addWidget(QLabel("浏览器伪装:"), 1, 2)
        options_layout.addWidget(self.impersonate_combo, 1, 3)

        self.ratelimit_input = QLineEdit()
        self.ratelimit_input.setPlaceholderText("例如: 2M, 500K")
        options_layout.addWidget(QLabel("下载限速:"), 2, 0)
        options_layout.addWidget(self.ratelimit_input, 2, 1)

        # 浏览器 Cookies：从本机浏览器导入
        # 注意：yt-dlp cookiesfrombrowser 只认小写 ("edge" / "chrome")，
        # 这里直接按小写显示，全链路免掉到处 .lower()
        self.cookie_browser_combo = QComboBox()
        self.cookie_browser_combo.setView(QListView())
        self.cookie_browser_combo.addItems(["不导入", "chrome", "edge"])
        options_layout.addWidget(QLabel("导入 Cookies:"), 2, 2)
        options_layout.addWidget(self.cookie_browser_combo, 2, 3)

        # 恢复上次选择的浏览器伪装 / cookies 导入（持久化到 ui_state.json）
        ui_state = load_ui_state()
        last_imp = ui_state.get("last_impersonate", "")
        if last_imp:
            idx = self.impersonate_combo.findText(last_imp)
            if idx >= 0:
                self.impersonate_combo.setCurrentIndex(idx)
        last_cookie = ui_state.get("last_cookie_browser", "")
        if last_cookie:
            idx = self.cookie_browser_combo.findText(last_cookie)
            if idx >= 0:
                self.cookie_browser_combo.setCurrentIndex(idx)
        # 恢复上次选择的下载格式（preset 名）
        last_fmt = ui_state.get("last_format_preset", "")
        if last_fmt and self.format_combo.findText(last_fmt) >= 0:
            self.format_combo.setCurrentText(last_fmt)

        self.write_subs_checkbox = Switch("下载字幕")
        options_layout.addWidget(self.write_subs_checkbox, 3, 0, 1, 2)

        # B 站弹幕下载（其他平台自动忽略）
        self.write_danmaku_checkbox = Switch("下载弹幕")
        self.write_danmaku_checkbox.setToolTip(
            "仅对 B 站 URL 生效；其他平台勾选也不会触发"
        )
        options_layout.addWidget(self.write_danmaku_checkbox, 3, 2, 1, 2)

        # 提示：选了浏览器时显示简要说明
        tip_label = QLabel("选浏览器 → 自动从本机 cookies 库读登录态；不导入 → 匿名抓取")
        tip_label.setStyleSheet("color: #AAA; font-size: 11px;")
        options_layout.addWidget(tip_label, 4, 0, 1, 4)
        options_group.setLayout(options_layout)
        layout.addWidget(options_group)

        # Playlist Options
        playlist_group = QGroupBox("播放列表设置")
        playlist_layout = QGridLayout()

        self.download_playlist_checkbox = Switch("启用")
        # 默认启用 playlist 下载：如果 URL 是 playlist 会 explode 成多条独立任务，
        # 如果 URL 是单视频 → explode 返回空 → fallback 一条单视频任务，安全无副作用
        self.download_playlist_checkbox.setChecked(True)
        playlist_layout.addWidget(QLabel("下载播放列表:"), 0, 0)
        playlist_layout.addWidget(self.download_playlist_checkbox, 0, 1)

        self.playlist_items_input = QLineEdit()
        playlist_layout.addWidget(QLabel("项目范围:"), 0, 2)
        playlist_layout.addWidget(self.playlist_items_input, 0, 3)

        self.max_downloads_input = QLineEdit()
        self.max_downloads_input.setPlaceholderText("例如: 10")
        playlist_layout.addWidget(QLabel("最大下载数:"), 1, 0)
        playlist_layout.addWidget(self.max_downloads_input, 1, 1)

        playlist_group.setLayout(playlist_layout)
        layout.addWidget(playlist_group)

        # Save Directory
        dir_layout = QHBoxLayout()
        self.dir_input = QLineEdit(self.selected_download_path)
        self.dir_input.setReadOnly(True)
        btn_browse = QPushButton("浏览...")
        btn_browse.clicked.connect(self._select_dir)
        dir_layout.addWidget(self.dir_input)
        dir_layout.addWidget(btn_browse)
        layout.addWidget(QLabel("保存目录:"))
        layout.addLayout(dir_layout)

        # Buttons
        btns_layout = QHBoxLayout()
        btn_add = QPushButton("确认添加")
        btn_add.setMinimumHeight(40)
        btn_add.clicked.connect(self._on_confirm_clicked)
        btn_cancel = QPushButton("取消")
        btn_cancel.setMinimumHeight(40)
        btn_cancel.clicked.connect(self.reject)
        btns_layout.addWidget(btn_cancel)
        btns_layout.addWidget(btn_add)
        layout.addLayout(btns_layout)

    def _on_confirm_clicked(self) -> None:
        """确认添加：先持久化当前目录 + 浏览器选择 + 下载格式，再 accept"""
        # 统一小写存（yt-dlp cookiesfrombrowser 只认小写 "edge" / "chrome"）
        imp = self.impersonate_combo.currentText()
        cookie = self.cookie_browser_combo.currentText()
        # 下载格式：若用户选了预览框里的具体格式（带 userData），保存 preset 名即可；
        # 没选的话就是普通 preset 下拉当前值
        current_combo_text = self.format_combo.currentText()
        # "已选：xxx" 这种动态插入项不要存——下次还是 preset 更有用
        fmt_to_save = "" if current_combo_text.startswith("已选") else current_combo_text
        save_ui_state({
            "last_save_path": self.selected_download_path,
            "last_impersonate": imp.lower() if imp and imp != "无" else "",
            "last_cookie_browser": cookie.lower() if cookie and cookie != "不导入" else "",
            "last_format_preset": fmt_to_save,
        })
        self.accept()

    def _select_dir(self):
        directory = QFileDialog.getExistingDirectory(
            self, "选择保存目录", self.selected_download_path
        )
        if directory:
            self.selected_download_path = directory
            self.dir_input.setText(directory)
            save_ui_state({"last_save_path": directory})

    # ---------- 格式预览 ----------
    def _collect_parse_opts(self) -> dict[str, Any]:
        impersonate_val = self.impersonate_combo.currentText()
        cookie_browser_val = self.cookie_browser_combo.currentText().lower()
        return {
            "proxy": self.proxy_input.text().strip() or None,
            "impersonate": impersonate_val if impersonate_val != "无" else None,
            "cookie_browser": cookie_browser_val if cookie_browser_val != "不导入" else None,
        }

    def _on_parse_clicked(self) -> None:
        url = self.url_input.text().strip()
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "提示", "请输入有效的视频链接")
            return
        if self._parse_thread:  # 正在解析
            return

        opts = self._collect_parse_opts()
        self.parse_btn.setEnabled(False)
        self.parse_btn.setText("解析中...")
        self.parse_progress.setVisible(True)

        self._parse_thread = QThread(self)
        self._parse_worker = FormatPreviewWorker(
            url=url,
            proxy=opts["proxy"],
            impersonate=opts["impersonate"],
            cookie_browser=opts["cookie_browser"],
        )
        self._parse_worker.moveToThread(self._parse_thread)
        self._parse_thread.started.connect(self._parse_worker.run)
        self._parse_worker.finished.connect(self._on_parse_success)
        self._parse_worker.error.connect(self._on_parse_error)
        self._parse_worker.finished.connect(self._parse_thread.quit)
        self._parse_worker.error.connect(self._parse_thread.quit)
        self._parse_thread.finished.connect(self._cleanup_parse_thread)
        self._parse_thread.start()

    @Slot(dict)
    def _on_parse_success(self, info: dict[str, Any]) -> None:
        self.parse_btn.setEnabled(True)
        self.parse_btn.setText("  解析格式")
        self.parse_btn.setIcon(qta.icon("fa5s.list-alt", color="#FFFFFF"))
        self.parse_progress.setVisible(False)

        # 判断是否有"可展示的格式"：
        #   1) 顶层有 formats（单视频）→ 直接 OK
        #   2) playlist 类型，entries 里至少一条有 formats（合集）→ OK
        has_formats = bool(info.get("formats"))
        if not has_formats:
            entries = info.get("entries") or []
            has_formats = any(e and (e.get("formats") or e.get("vcodec")) for e in entries)
        if not has_formats:
            QMessageBox.warning(self, "提示", "该视频没有可用的格式")
            return
        if info.get("is_live"):
            QMessageBox.information(self, "直播视频", "这是一个直播视频，不支持格式预览")
            return

        dlg = FormatPreviewDialog(info, parent=self)
        if dlg.exec():
            self._format_spec = dlg.get_format_spec()
            self._playlist_format_spec = dlg.get_playlist_format_spec()
            self._format_human_label = dlg.get_human_label()
            self._last_url_for_parse = info.get("webpage_url") or self.url_input.text()
            # 自动把勾选项转成 playlist_items 字符串塞进文本框
            self._playlist_items_str = dlg.get_playlist_items_str()
            # 关键：FormatPreviewDialog 弹出来 = URL 是 playlist（单视频 URL 不会弹这个对话框）
            # 所以不管用户勾选了几条（全部 / 部分），都要勾 Switch
            self.download_playlist_checkbox.setChecked(True)
            if self._playlist_items_str is not None:
                self.playlist_items_input.setText(self._playlist_items_str)
            # 动态刷新 format_combo：头部插入"已选具体格式"项并自动选中
            self._refresh_format_combo_for_selection()

    def _refresh_format_combo_for_selection(self) -> None:
        """把用户刚从预览框里选的 format 插到下拉框顶部并自动选中"""
        # 先清空再重建（去掉之前插入的"已选具体格式"项，保留 preset）
        self.format_combo.blockSignals(True)
        self.format_combo.clear()
        if self._format_spec:
            label = self._format_human_label or self._format_spec
            display = f"已选：{label}"
            self.format_combo.addItem(display, userData=self._format_spec)
        self.format_combo.addItems(self._format_preset_items)
        self.format_combo.setCurrentIndex(0 if self._format_spec else 1)
        self.format_combo.blockSignals(False)

    @staticmethod
    def _preset_items_for_url(url: str) -> list[str]:
        """按 URL 过滤预设：杜比视界轨只有 B 站提供，其他平台不显示该项"""
        items = list(FORMAT_PRESETS.keys())
        if "bilibili.com" not in url.lower():
            items = [i for i in items if i != DOVI_PRESET_NAME]
        return items

    @Slot(str)
    def _on_url_text_changed(self, _text: str) -> None:
        """URL 变化时重建预设列表（杜比视界项随 B 站链接显隐），尽量保留当前选中"""
        new_items = self._preset_items_for_url(self.url_input.text())
        if new_items == self._format_preset_items:
            return
        # combo 头部可能有解析后插入的"已选：..."项（带 userData），重建时保留
        head: list[tuple[str, Any]] = []
        if self._format_spec and self.format_combo.count() > len(self._format_preset_items):
            head.append((self.format_combo.itemText(0), self.format_combo.itemData(0)))
        prev_text = self.format_combo.currentText()
        self.format_combo.blockSignals(True)
        self.format_combo.clear()
        for text, data in head:
            self.format_combo.addItem(text, userData=data)
        self.format_combo.addItems(new_items)
        self._format_preset_items = new_items
        if head:
            self.format_combo.setCurrentIndex(0)
        else:
            idx = self.format_combo.findText(prev_text)
            self.format_combo.setCurrentIndex(max(idx, 0))
        self.format_combo.blockSignals(False)

    @Slot(str)
    def _on_parse_error(self, err: str) -> None:
        self.parse_btn.setEnabled(True)
        self.parse_btn.setText("  解析格式")
        self.parse_btn.setIcon(qta.icon("fa5s.list-alt", color="#FFFFFF"))
        self.parse_progress.setVisible(False)
        QMessageBox.critical(self, "解析失败", f"无法获取视频信息：\n\n{err}")

    @Slot()
    def _cleanup_parse_thread(self) -> None:
        if self._parse_worker:
            self._parse_worker.deleteLater()
        self._parse_worker = None
        if self._parse_thread:
            self._parse_thread.deleteLater()
        self._parse_thread = None

    def _close_event(self, event) -> None:
        """关窗口时清理后台线程"""
        if self._parse_thread:
            self._parse_thread.quit()
            self._parse_thread.wait(1000)
        super().closeEvent(event)

    def get_task_data(self) -> list[DownloadTask]:
        """返回一条或多条 DownloadTask。

        - 单视频 → 返回 1 条
        - playlist → explode 成多条（每勾选项一条）；explode 失败则 fallback 一条 playlist 任务
        """
        impersonate_val = self.impersonate_combo.currentText()
        if impersonate_val == "无":
            impersonate_val = None

        cookie_browser_val = self.cookie_browser_combo.currentText().lower()
        if cookie_browser_val == "不导入":
            cookie_browser_val = None

        # format 取值优先级：
        #   playlist → 特征 fallback 链（id 首选 + 同编码/分辨率候选），
        #              避免各分集 id 不同被通用 selector 换成别的编码（如选 hev1 却下到 dvhe）
        #   单视频：
        #   1) combo 里通过 userData 塞的"已选：..."项（具体 format_id+format_id）
        #   2) _format_spec 字段（预览框返回）
        #   3) preset 下拉名字（走 config.py 里的 FORMAT_PRESETS 映射）
        is_playlist = self.download_playlist_checkbox.isChecked()
        if is_playlist and self._playlist_format_spec:
            format_preset = self._playlist_format_spec
        else:
            fmt_data = self.format_combo.currentData()
            if isinstance(fmt_data, str) and "+" in fmt_data:
                format_preset = fmt_data
            elif self._format_spec:
                format_preset = self._format_spec
            else:
                preset_name = self.format_combo.currentText()
                format_preset = FORMAT_PRESETS.get(preset_name, "best")

        base_kwargs = {
            "save_path": self.dir_input.text(),
            "format_preset": format_preset,
            "proxy": self.proxy_input.text().strip() or None,
            "concurrent_fragments": int(self.concurrent_input.text())
            if self.concurrent_input.text().isdigit()
            else None,
            "write_subs": self.write_subs_checkbox.isChecked(),
            "write_danmaku": self.write_danmaku_checkbox.isChecked(),
            "max_downloads": int(self.max_downloads_input.text())
            if self.max_downloads_input.text().isdigit()
            else None,
            "impersonate": impersonate_val,
            "cookie_browser": cookie_browser_val,
            "ratelimit": self.ratelimit_input.text().strip() or None,
        }

        url = self.url_input.text().strip()
        # playlist_items_str：FormatPreviewDialog 返回的（优先）> 用户手动输入的
        playlist_items_str = self._playlist_items_str or (
            self.playlist_items_input.text().strip() or None
        )

        if not is_playlist:
            # 单视频：一条 task
            print(f"[get_task_data] 单视频模式: url={url}")
            return [DownloadTask(url=url, **base_kwargs)]

        # playlist → 尝试 explode
        try:
            # explode_playlist_to_tasks 的显式参数是 url/proxy/impersonate/cookie_browser/playlist_items_str，
            # base_kwargs 里已经有 proxy/impersonate/cookie_browser 三个键
            # → 全 pop 出来作为显式参数，避免重复传
            explode_kwargs = dict(base_kwargs)
            e_proxy = explode_kwargs.pop("proxy", None)
            e_impersonate = explode_kwargs.pop("impersonate", None)
            e_cookie_browser = explode_kwargs.pop("cookie_browser", None)
            tasks = FormatPreviewDialog.explode_playlist_to_tasks(
                url=url,
                proxy=e_proxy,
                impersonate=e_impersonate,
                cookie_browser=e_cookie_browser,
                playlist_items_str=playlist_items_str,
                **explode_kwargs,
            )
            if tasks:
                print(f"[get_task_data] playlist explode 成功: {len(tasks)} 条独立 task, "
                      f"playlist_items_str={playlist_items_str!r}")
                return tasks
            # explode 返回空 → fallback 一条 playlist 任务
            print(f"[get_task_data] explode 返回空，fallback 单条 playlist 任务")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[get_task_data] explode 抛异常: {e!r}，fallback 单条 playlist 任务")
            # pre-parse 失败也没关系，fallback 单条 playlist 任务

        # fallback：让 yt-dlp 自己串行跑整个 playlist
        print(f"[get_task_data] fallback 单条 playlist 任务: url={url} playlist_items={playlist_items_str!r}")
        return [
            DownloadTask(
                url=url,
                download_playlist=True,
                playlist_items=playlist_items_str,
                **base_kwargs,
            )
        ]


GITHUB_URL = "https://github.com/twn39/yt-dlp-qt-gui"


class AboutDialog(QDialog):
    """现代化「关于」对话框，包含项目信息和 GitHub 链接"""

    def __init__(self, version: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("关于 Yt-dlp GUI")
        self.setFixedSize(380, 330)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self._setup_ui(version)

    def _setup_ui(self, version: str) -> None:
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(32, 24, 32, 20)

        # 应用图标 —— scale_factor 避免字形左右边界被裁剪
        icon_container = QLabel()
        icon_container.setFixedSize(64, 64)
        icon_container.setPixmap(
            qta.icon("fa5s.cloud-download-alt", color="#FFFFFF", scale_factor=0.85).pixmap(56, 56)
        )
        icon_container.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_container, alignment=Qt.AlignmentFlag.AlignHCenter)

        # 应用名称（与应用主字号协调：13pt，粗体）
        name_label = QLabel("Yt-dlp GUI")
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_label.setStyleSheet("font-size: 15pt; font-weight: bold; color: #E0E0E0;")
        layout.addWidget(name_label)

        # 副标题 + 版本
        try:
            import yt_dlp

            yt_version = getattr(getattr(yt_dlp, "version", None), "__version__", "")
        except Exception:
            yt_version = ""

        ver_info = f"v{version}" + (f" (yt-dlp {yt_version})" if yt_version else "")
        subtitle_label = QLabel(f"{ver_info}  ·  现代化视频下载管理器")
        subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle_label.setStyleSheet("font-size: 9pt; color: #888888;")
        layout.addWidget(subtitle_label)

        # 分隔线（QFrame HLine 用 background-color 而非 color）
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #2A2A2A; border: none;")
        layout.addWidget(sep)
        layout.addSpacing(2)

        # 简介
        desc_label = QLabel(
            "基于 yt-dlp 构建的开源视频下载工具\n支持 YouTube、Bilibili、Vimeo 等数千个视频平台"
        )
        desc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("font-size: 9pt; color: #AAAAAA; line-height: 160%;")
        layout.addWidget(desc_label)

        layout.addSpacing(4)

        # GitHub 链接按钮 —— 白色图标与白色文字
        github_btn = QPushButton(
            qta.icon("fa5b.github", color="#FFFFFF"),
            f"  {GITHUB_URL.removeprefix('https://')}",
        )
        github_btn.setStyleSheet(
            "QPushButton { color: #FFFFFF; font-size: 9pt; }"
            "QPushButton:hover { color: #E0E0E0; border-color: #555555; }"
        )
        github_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        github_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(GITHUB_URL)))
        layout.addWidget(github_btn)

        # 关闭按钮行（居中，固定宽度）
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.setMinimumHeight(32)
        btn_close.setFixedWidth(100)
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        btn_row.addStretch()
        layout.addLayout(btn_row)


class DialogManager:
    """对话框管理器，用于解耦 MainWindow 对具体对话框类的直接实例化依赖"""

    def __init__(self, parent_window: QWidget) -> None:
        self.parent = parent_window

    def show_about(self, version: str) -> None:
        """显示关于对话框"""
        dialog = AboutDialog(version=version, parent=self.parent)
        dialog.exec()

    def show_add_task(
        self, initial_url: str | None = None
    ) -> Optional[list["DownloadTask"]]:
        """显示添加任务对话框，返回一条或多条 DownloadTask（playlist 会 explode）。
        用户取消返回 None。"""
        dialog = AddTaskDialog(parent=self.parent, initial_url=initial_url)
        if dialog.exec():
            return dialog.get_task_data()
        return None

    def show_log(
        self, task_id: int, title: str, logs: str, on_finished: Callable[[], None]
    ) -> QDialog:
        """显示非模态的任务日志对话框"""
        dialog = LogDialog(task_id=task_id, title=title, parent=self.parent)
        dialog.set_initial_logs(logs)
        dialog.finished.connect(on_finished)
        dialog.show()
        return dialog
