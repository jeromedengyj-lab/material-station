"""素材准备站 - 独立软件入口（PySide6 精简界面）。

把「下载原剧视频 / 下载封面 / 申请三端关键词别名」从漫剧自动任务中心剥离，
以手动 BookID 为任务来源，绿色文件夹部署，内置 Node / Ollama / 模型。

运行方式：
    开发：python station_main.py
    打包：build_station.ps1 产出绿色文件夹后双击 素材准备站.exe
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import time

from pathlib import Path

STATION_VERSION = "v1.0"


def build_uninstall_bat(exe_dir: str, keep_data: bool = True) -> str:
    """生成一键卸载批处理：先杀进程，再删除程序本体（exe、_internal、runtime、logs）。

    keep_data=True：保留 data（任务记录与已下载剧目），适合更新前清理或暂时停用；
    keep_data=False：连 data 一起删并清空整个运行目录（彻底卸载，不可恢复）。
    返回的脚本用 GBK 编码写入（Windows 中文系统 cmd 默认代码页），自身随执行自动删除。
    """
    lines = [
        "@echo off",
        'taskkill /IM "素材准备站.exe" /F >nul 2>&1',
        "timeout /t 2 /nobreak >nul",
        f'rd /s /q "{exe_dir}\\_internal" >nul 2>&1',
        f'rd /s /q "{exe_dir}\\runtime" >nul 2>&1',
        f'rd /s /q "{exe_dir}\\logs" >nul 2>&1',
        f'del /q "{exe_dir}\\素材准备站.exe" >nul 2>&1',
    ]
    if not keep_data:
        lines.append(f'rd /s /q "{exe_dir}\\data" >nul 2>&1')
        lines.append(f'rd /s /q "{exe_dir}" >nul 2>&1')
        # 浏览器登录态缓存（%LOCALAPPDATA%\\素材准备站）也一并清掉，彻底卸载
        lines.append('rd /s /q "%LOCALAPPDATA%\\素材准备站" >nul 2>&1')
    lines.append('del /q "%~f0" >nul 2>&1')
    return "\r\n".join(lines) + "\r\n"

# 候选别名的申请结果 → 表格展示文案
_ALIAS_RESULT_LABELS = {
    "approved": "成功",
    "rejected": "失败-被拒",
    "global_duplicate": "失败-重复",
    "failed": "失败",
    "waiting_manual_verification": "待人工核验",
    "waiting_review": "待审核",
    "waiting_retry": "待重试",
    "running": "进行中",
    "queued": "未申请",
}


def alias_summary(task_file: str | Path | None) -> tuple[str, str]:
    """从三端别名任务.json 汇总每个候选别名的申请结果。

    返回 (采用的别名, 候选结果摘要)。摘要按候选顺序列出，如：
      "知夏断案=成功；知夏守名=失败；知夏拒婚=失败"
    文件缺失或无法解析时返回 ("", "（任务文件缺失）")。
    """
    if not task_file:
        return "", ""
    try:
        data = json.loads(Path(task_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", "（任务文件缺失）"
    if not isinstance(data, dict):
        return "", "（任务文件无效）"
    approved = str(data.get("approved_alias") or "").strip()
    rows = data.get("candidate_rows")
    if isinstance(rows, list) and rows:
        parts = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            alias = str(row.get("alias") or "").strip()
            status = str(row.get("status") or "")
            label = _ALIAS_RESULT_LABELS.get(status, status or "未知")
            parts.append(f"{alias}={label}")
        return approved, "；".join(parts)
    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates:
        parts = [f"{str(x).strip()}=未申请" for x in candidates if str(x).strip()]
        return approved, "；".join(parts)
    return approved, ""


def export_tasks_to_csv(tasks: list, path: str | Path) -> int:
    """把任务列表导出为 CSV（UTF-8 BOM，Excel 可直接打开，中文不乱码）。

    只读导出：不修改任何任务状态，不清除界面信息。
    tasks: 每项含 input_value / book_id / title / status_text / detail /
           alias（采用的别名）/ alias_result（候选别名申请结果摘要）。
    返回导出的行数。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["序号", "输入", "剧名", "状态", "详情", "别名", "别名结果", "时间"])
        for row, task in enumerate(tasks, start=1):
            display_input = str(task.get("input_value") or task.get("book_id") or "")
            writer.writerow([
                row,
                display_input,
                task.get("title") or "-",
                task.get("status_text") or task.get("status") or "",
                task.get("detail") or "",
                task.get("alias") or "",
                task.get("alias_result") or "",
                task.get("time_text") or "",
            ])
    return len(tasks)


def merge_rows_into_csv(rows: list, path: str | Path) -> tuple[int, int]:
    """把任务行累积合并进已有 CSV 表格（「始终同一表格」模式）。

    已存在表格：按「输入」列去重——同一输入（同 BookID/剧名）更新该行内容，
    新输入追加到末尾；表头保持不变，序号重新连续编号。
    文件不存在时直接创建（等同 export_tasks_to_csv）。
    rows: 与 export_tasks_to_csv 相同的任务行 dict。
    返回 (新增行数, 更新行数)。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["序号", "输入", "剧名", "状态", "详情", "别名", "别名结果", "时间"]
    existing: dict[str, list[str]] = {}  # 输入 → 原行（不含序号）
    order: list[str] = []                # 保持原行顺序
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle)
                first = next(reader, None)
                for raw in reader:
                    if not raw or not any(cell.strip() for cell in raw):
                        continue
                    key = raw[1] if len(raw) > 1 else ""
                    if key not in existing:
                        order.append(key)
                    existing[key] = (raw + [""] * (len(header) - len(raw)))[1:len(header)]
        except (OSError, UnicodeDecodeError):
            existing, order = {}, []

    added = updated = 0
    for task in rows:
        display_input = str(task.get("input_value") or task.get("book_id") or "")
        values = [
            display_input,
            task.get("title") or "-",
            task.get("status_text") or task.get("status") or "",
            task.get("detail") or "",
            task.get("alias") or "",
            task.get("alias_result") or "",
            task.get("time_text") or "",
        ]
        if display_input in existing:
            existing[display_input] = values
            updated += 1
        else:
            existing[display_input] = values
            order.append(display_input)
            added += 1

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for number, key in enumerate(order, start=1):
            writer.writerow([number, *existing[key]])
    return added, updated

def _tool_root() -> Path:
    configured = str(os.environ.get("MANJU_TOOL_ROOT", "") or "").strip()
    if configured:
        return Path(configured).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _license_required(data_dir: Path) -> bool:
    """是否必须激活才能使用。

    优先级：data\\licensed_mode.flag（已安装/已激活）→ 环境变量 MANJU_FORCE_LICENSE
    → 构建常量 _build_config.FORCE_LICENSE（安装包资源为 True，绿色版为 False）。
    """
    if (data_dir / "licensed_mode.flag").is_file():
        return True
    env = str(os.environ.get("MANJU_FORCE_LICENSE", "") or "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    try:
        from _build_config import FORCE_LICENSE  # type: ignore[import-not-found]
        return bool(FORCE_LICENSE)
    except Exception:  # noqa: BLE001
        return False


def _ensure_license(data_dir: Path) -> bool:
    """安装版激活校验：核对 本机设备码 + 激活文件密钥，不匹配弹激活窗口。

    绿色版（无 licensed_mode.flag）不调用本函数，保持原行为。
    """
    import json as _json

    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
        QMessageBox,
    )
    from license_utils import get_device_code, key_status

    device_code = get_device_code()
    license_file = data_dir / "license.dat"
    saved: dict = {}
    try:
        saved = _json.loads(license_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        saved = {}
    saved_status, saved_expiry = key_status(device_code, str(saved.get("key", "")))
    if str(saved.get("device_code", "")).upper() == device_code and saved_status == "valid":
        return True

    dialog = QDialog()
    dialog.setWindowTitle("素材准备站 · 激活")
    dialog.resize(560, 220)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("本机设备码（复制后向授权方获取密钥）："))
    code_row = QHBoxLayout()
    code_edit = QLineEdit(device_code)
    code_edit.setReadOnly(True)
    code_edit.setFixedWidth(300)
    copy_btn = QPushButton("复制设备码")
    code_row.addWidget(code_edit)
    code_row.addWidget(copy_btn)
    code_row.addStretch(1)
    layout.addLayout(code_row)
    layout.addWidget(QLabel("授权密钥："))
    key_edit = QLineEdit()
    key_edit.setPlaceholderText("XXXX-XXXX-XXXX-XXXX")
    layout.addWidget(key_edit)
    state_label = QLabel("")
    layout.addWidget(state_label)

    def copy_code() -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(device_code)

    def try_activate() -> None:
        key = key_edit.text().strip()
        status, expiry = key_status(device_code, key)
        if status == "invalid":
            state_label.setText("❌ 密钥无效，请核对设备码与密钥是否对应")
            return
        if status == "expired":
            state_label.setText(f"❌ 密钥已过期（{expiry} 到期），请向授权方获取新密钥")
            return
        try:
            license_file.write_text(
                _json.dumps({"device_code": device_code, "key": key}, ensure_ascii=False),
                encoding="utf-8",
            )
            (data_dir / "licensed_mode.flag").write_text("1", encoding="utf-8")
        except OSError as error:
            QMessageBox.critical(dialog, "写入失败", f"无法写入激活信息：{error}")
            return
        dialog.accept()

    copy_btn.clicked.connect(copy_code)
    ok_btn = QPushButton("激活并启动")
    ok_btn.clicked.connect(try_activate)
    layout.addWidget(ok_btn)
    return dialog.exec() == QDialog.Accepted


def _adapter_dir(tool_root: Path) -> Path:
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", ""))
        if bundle.is_dir() and (bundle / "platform_adapter" / "task-platform-download.mjs").is_file():
            return bundle / "platform_adapter"
    candidate = tool_root / "platform_adapter"
    if (candidate / "task-platform-download.mjs").is_file():
        return candidate
    # 开发环境：仓库根/platform_adapter
    return Path(__file__).resolve().parent / "platform_adapter"


def main() -> int:
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QTableWidget, QTableWidgetItem, QPushButton, QLineEdit, QLabel,
        QPlainTextEdit, QHeaderView, QMessageBox, QComboBox, QCheckBox,
        QFileDialog,
    )
    from PySide6.QtCore import Qt, QTimer

    # 独立仓库：station_core.py 与 station_main.py 同目录（开发/打包一致）
    from station_core import (
        StationCore, STATUS_QUEUED, STATUS_DOWNLOADING, STATUS_GENERATING,
        STATUS_APPLYING, STATUS_SUBMITTED, STATUS_REVIEWING,
        STATUS_WAITING_MANUAL, STATUS_DONE, STATUS_FAILED,
        platform_count,
    )

    tool_root = _tool_root()
    core = StationCore(
        tool_root=tool_root,
        data_root=tool_root / "data",
        adapter_dir=_adapter_dir(tool_root),
    )

    app = QApplication(sys.argv)
    app.setApplicationName(f"素材准备站{STATION_VERSION}")

    class StationWindow(QMainWindow):
        _STATUS_TEXT = {
            STATUS_QUEUED: "排队中",
            STATUS_DOWNLOADING: "下载原剧+封面",
            STATUS_GENERATING: "生成候选别名",
            STATUS_APPLYING: f"申请{platform_count(tool_root)}端别名",
            STATUS_SUBMITTED: "已提交待审核",
            STATUS_REVIEWING: "统一审核中",
            STATUS_WAITING_MANUAL: "需人工处理",
            STATUS_DONE: "完成",
            STATUS_FAILED: "失败",
        }

        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle(f"素材准备站{STATION_VERSION} - 下载原剧/封面/申请关键词")
            self.resize(1080, 640)
            self._build_ui()
            self._log_lines: list[str] = []
            self._last_export_dir: str | None = None
            self._tick_timer = QTimer(self)
            self._tick_timer.timeout.connect(self._on_tick)
            self._tick_timer.start(2000)
            core.set_progress_callback(self._on_progress)
            self._refresh_tasks()
            self._append_log(f"素材准备站{STATION_VERSION} 已启动")
            self._append_log(f"数据目录：{core.data_root}")
            self._append_log(f"适配器目录：{core.adapter_dir}")

        def _build_ui(self) -> None:
            root = QWidget(self)
            layout = QVBoxLayout(root)
            layout.setContentsMargins(10, 10, 10, 10)

            top = QHBoxLayout()
            top.addWidget(QLabel("BookID / 剧名："))
            self.book_input = QLineEdit()
            self.book_input.setPlaceholderText("输入 BookID（16~20位数字）或剧名，回车添加")
            self.book_input.returnPressed.connect(self._add_task)
            self._pending_episodes = 0  # 单张识图暂存的集数（跟随该任务，作为识别辅助）
            top.addWidget(self.book_input, 1)
            add_btn = QPushButton("添加任务")
            add_btn.clicked.connect(self._add_task)
            top.addWidget(add_btn)
            import_btn = QPushButton("批量导入txt")
            import_btn.clicked.connect(self._import_from_file)
            top.addWidget(import_btn)
            retry_btn = QPushButton("重试选中")
            retry_btn.clicked.connect(self._retry_selected)
            top.addWidget(retry_btn)
            remove_btn = QPushButton("移除选中")
            remove_btn.clicked.connect(self._remove_selected)
            top.addWidget(remove_btn)
            open_dir_btn = QPushButton("打开产出目录")
            open_dir_btn.clicked.connect(self._open_output)
            top.addWidget(open_dir_btn)
            export_btn = QPushButton("下载到表格")
            export_btn.clicked.connect(self._export_to_table)
            top.addWidget(export_btn)
            self.table_mode_combo = QComboBox()
            self.table_mode_combo.addItems(["始终同一表格", "每批一个新表格"])
            self.table_mode_combo.setCurrentIndex(0 if core.table_mode == "single" else 1)
            self.table_mode_combo.currentIndexChanged.connect(self._on_table_mode_changed)
            self.table_mode_combo.setToolTip(
                "始终同一表格：所有批次累积进一个 CSV（同 BookID/剧名自动更新该行）\n"
                "每批一个新表格：每次导出生成带时间戳的新文件"
            )
            top.addWidget(self.table_mode_combo)
            top.addSpacing(12)
            top.addWidget(QLabel("内容类型："))
            self.type_combo = QComboBox()
            self.type_combo.addItems(["不限", "漫剧", "网文", "短剧"])
            # 记住上次选择：按持久化的全局 content_type 恢复下拉（不硬编码回"不限"）
            self.type_combo.setCurrentIndex({"manju": 1, "wangwen": 2, "duanju": 3}.get(core.content_type, 0))
            self.type_combo.currentIndexChanged.connect(self._on_type_changed)
            top.addWidget(self.type_combo)
            ocr_btn = QPushButton("识图添加")
            ocr_btn.setToolTip("选择剧图，识别图中的剧名/集数/标签后填入上方（可修正），再点「添加任务」")
            ocr_btn.clicked.connect(self._ocr_add)
            top.addWidget(ocr_btn)
            uninstall_btn = QPushButton("卸载软件")
            uninstall_btn.setToolTip("一键卸载：关闭进程并删除程序本体（可保留任务数据）。更新版本前可先用它清理")
            uninstall_btn.setStyleSheet("color:#C0392B;")
            uninstall_btn.clicked.connect(self._uninstall)
            top.addWidget(uninstall_btn)
            layout.addLayout(top)

            prefix_row = QHBoxLayout()
            self.fix_affix_check = QCheckBox("固定前/后两字")
            self.fix_affix_check.setChecked(core.fix_affix)
            self.fix_affix_check.stateChanged.connect(self._on_fix_affix_changed)
            prefix_row.addWidget(self.fix_affix_check)
            prefix_row.addWidget(QLabel("别名固定字："))
            self.prefix_input = QLineEdit()
            self.prefix_input.setPlaceholderText("选上「固定前/后两字」后在此输入，例如：知夏、年年")
            self.prefix_input.setText(core.alias_prefix)
            self.prefix_input.setEnabled(core.fix_affix)
            self.prefix_input.editingFinished.connect(self._on_prefix_changed)
            prefix_row.addWidget(self.prefix_input, 1)
            prefix_row.addWidget(QLabel("（不选=AI自由生成4个字；选上=必须指定固定字）"))
            layout.addLayout(prefix_row)

            options_row = QHBoxLayout()
            options_row.addWidget(QLabel("别名格式："))
            self.mode_combo = QComboBox()
            self.mode_combo.addItems(["前缀+两字（如 知夏藏锋）", "两字+后缀（如 藏锋知夏）"])
            self.mode_combo.setCurrentIndex(0 if core.alias_mode == "prefix" else 1)
            self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
            options_row.addWidget(self.mode_combo)
            options_row.addSpacing(20)
            self.download_only_check = QCheckBox("只下载（不申请别名）")
            self.download_only_check.setChecked(core.download_only)
            self.download_only_check.stateChanged.connect(self._on_download_only_changed)
            options_row.addWidget(self.download_only_check)
            self.alias_only_check = QCheckBox("只申请别名（跳过下载）")
            self.alias_only_check.setChecked(core.alias_only)
            self.alias_only_check.stateChanged.connect(self._on_alias_only_changed)
            options_row.addWidget(self.alias_only_check)
            self.manual_mode_check = QCheckBox("手动别名模式（导入后不自动执行）")
            self.manual_mode_check.setChecked(core.manual_mode)
            self.manual_mode_check.stateChanged.connect(self._on_manual_mode_changed)
            options_row.addWidget(self.manual_mode_check)
            options_row.addStretch(1)
            layout.addLayout(options_row)

            post_row = QHBoxLayout()
            post_row.addWidget(QLabel("发文类型："))
            self.post_type_combo = QComboBox()
            self.post_type_combo.setEditable(True)
            self.post_type_combo.addItem(core.post_type or "解说混剪")
            self.post_type_combo.setCurrentText(core.post_type or "解说混剪")
            self.post_type_combo.setToolTip(
                "平台弹窗「请选择计划发文的素材类型」选的值；\n"
                "可直接输入平台里存在的类型，选好后下次打开保持"
            )
            self.post_type_combo.editTextChanged.connect(self._on_post_type_changed)
            post_row.addWidget(self.post_type_combo, 1)
            post_row.addWidget(QLabel("（弹窗发文类型，默认解说混剪；选/填好后下次打开保持）"))
            layout.addLayout(post_row)

            manual_row = QHBoxLayout()
            manual_row.addWidget(QLabel("手动别名："))
            self.manual_alias_input = QLineEdit()
            self.manual_alias_input.setPlaceholderText("输入别名，多个用逗号分隔，如：年年炉灶,年年红砖")
            manual_row.addWidget(self.manual_alias_input, 1)
            manual_apply_btn = QPushButton("申请手动别名")
            manual_apply_btn.clicked.connect(self._apply_manual_alias)
            manual_row.addWidget(manual_apply_btn)
            manual_row.addWidget(QLabel(f"（需先有剧目信息，跳过AI生成直接申请{platform_count(tool_root)}端）"))
            layout.addLayout(manual_row)

            self.table = QTableWidget(0, 5)
            self.table.setHorizontalHeaderLabels(["输入", "剧名", "状态", "详情", "时间"])
            header = self.table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.Stretch)
            header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
            self.table.setSelectionBehavior(QTableWidget.SelectRows)
            layout.addWidget(self.table, 3)

            self.log_view = QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setMaximumBlockCount(2000)
            layout.addWidget(self.log_view, 2)
            self.setCentralWidget(root)

        # ---------- UI 动作 ----------
        def _on_fix_affix_changed(self, state: int) -> None:
            enabled = state == 2  # Qt.Checked
            core.set_fix_affix(enabled)
            self.prefix_input.setEnabled(enabled)
            self._append_log(f"固定前/后两字：{'已开启' if enabled else '已关闭（AI自由生成4个字）'}")

        def _on_prefix_changed(self) -> None:
            value = self.prefix_input.text().strip()
            core.set_alias_prefix(value)
            self._append_log(f"别名前缀已设置：{value or '（留空，自动生成）'}")

        def _on_mode_changed(self, index: int) -> None:
            mode = "prefix" if index == 0 else "suffix"
            core.set_alias_mode(mode)
            label = "前缀+两字" if mode == "prefix" else "两字+后缀"
            self._append_log(f"别名格式已切换：{label}")

        def _on_download_only_changed(self, state: int) -> None:
            enabled = state == 2  # Qt.Checked
            core.set_download_only(enabled)
            if enabled:
                self.alias_only_check.blockSignals(True)
                self.alias_only_check.setChecked(False)
                self.alias_only_check.blockSignals(False)
            self._append_log(f"只下载模式：{'开启' if enabled else '关闭'}")

        def _on_alias_only_changed(self, state: int) -> None:
            enabled = state == 2  # Qt.Checked
            core.set_alias_only(enabled)
            if enabled:
                self.download_only_check.blockSignals(True)
                self.download_only_check.setChecked(False)
                self.download_only_check.blockSignals(False)
            self._append_log(f"只申请别名模式：{'开启' if enabled else '关闭'}（需已有剧目信息）")

        def _on_manual_mode_changed(self, state: int) -> None:
            enabled = state == 2  # Qt.Checked
            core.set_manual_mode(enabled)
            self._append_log(f"手动别名模式：{'开启' if enabled else '关闭'}（导入后不自动执行，等手动输入别名）")

        def _on_type_changed(self, index: int) -> None:
            key = ["", "manju", "wangwen", "duanju"][index]
            core.set_content_type(key)
            self._append_log(f"内容类型：{['不限', '漫剧', '网文', '短剧'][index]}（同名时按此标签精确匹配；不限=不按类型过滤）")

        def _on_post_type_changed(self, value: str) -> None:
            core.set_post_type(value)
            self._append_log(f"发文类型已设置：{core.post_type}")

        def _ocr_add(self) -> None:
            file_paths, _ = QFileDialog.getOpenFileNames(
                self, "选择剧图（支持多选批量识别添加）",
                "", "图片 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*.*)"
            )
            if not file_paths:
                return
            # 单张：回填到输入框供确认/修正后手动添加
            if len(file_paths) == 1:
                file_path = file_paths[0]
                try:
                    self._append_log(f"正在识别图片：{os.path.basename(file_path)}")
                    lines = core.ocr_image(file_path)
                    meta = core.parse_ocr_meta(lines)
                    title = meta["title"]
                    if not title or core._ocr_text_quality(title) < 0.45:
                        QMessageBox.warning(self, "未识别到有效剧名", "图片文字不清晰或为乱码，请改用 BookID 或剧名手动输入")
                        return
                    self.book_input.setText(title)
                    # 集数暂存（跟着这个识图任务走，添加任务时写入任务级；不设全局输入框）
                    self._pending_episodes = meta["episodes"] or 0
                    # 内容类型不回填：它是用户主动选择（识图/搜索的定位条件），避免识别词覆盖
                    tip = f"识图完成：剧名「{title}」"
                    if meta["episodes"]:
                        tip += f"，{meta['episodes']} 集"
                    if meta.get("tags"):
                        tip += f"，标签 {'/'.join(meta['tags'])}"
                    tip += "（识别结果可修改，确认后点「添加任务」）"
                    self._append_log(tip)
                except Exception as error:  # noqa: BLE001
                    QMessageBox.critical(self, "识图失败", str(error))
                return
            # 多张：逐个识别并直接批量添加任务（识别不出/乱码/重复的跳过并汇报）
            self._append_log(f"批量识图开始：共 {len(file_paths)} 张")
            try:
                # 内容类型取当前下拉值（用户主动选择，应用到每张图的任务）
                type_key = ["", "manju", "wangwen", "duanju"][self.type_combo.currentIndex()]
                result = core.add_tasks_from_images(file_paths, content_type=type_key)
            except Exception as error:  # noqa: BLE001
                QMessageBox.critical(self, "批量识图失败", str(error))
                return
            for title, meta in result["added"]:
                tip = f"  已添加：{title}"
                if meta.get("episodes"):
                    tip += f"（{meta['episodes']}集）"
                if type_key:
                    tip += f"（{type_key}）"
                self._append_log(tip)
            for label, reason in result["skipped"]:
                self._append_log(f"  跳过：{os.path.basename(str(label))} → {reason}")
            self._append_log(f"批量识图完成：成功 {len(result['added'])} 张，跳过 {len(result['skipped'])} 张")
            self._refresh_tasks()
            QMessageBox.information(
                self, "批量识图结果",
                f"成功添加：{len(result['added'])} 张\n跳过：{len(result['skipped'])} 张\n\n跳过明细见日志"
            )

        def _add_task(self) -> None:
            value = self.book_input.text().strip()
            if not value:
                return
            try:
                # 内容类型取当前下拉值（用户主动选择）；集数用识图暂存值（跟着识图任务走，不设输入框）
                type_key = ["", "manju", "wangwen", "duanju"][self.type_combo.currentIndex()]
                episodes = self._pending_episodes or None
                task = core.add_task(value, content_type=type_key, expected_episodes=episodes)
                self.book_input.clear()
                self._pending_episodes = 0
                label = f"BookID {task.input_value}" if task.input_type == "book_id" else f"剧名「{task.input_value}」"
                extra = []
                if task.content_type:
                    extra.append({"manju": "漫剧", "wangwen": "网文", "duanju": "短剧"}.get(task.content_type, task.content_type))
                if task.expected_episodes:
                    extra.append(f"{task.expected_episodes}集")
                self._append_log(f"已添加 {label}" + (f"（{'、'.join(extra)}）" if extra else ""))
                self._refresh_tasks()
            except ValueError as error:
                QMessageBox.warning(self, "无法添加", str(error))

        def _import_from_file(self) -> None:
            file_path, _ = QFileDialog.getOpenFileName(
                self, "选择批量导入的 txt 文件", "", "文本文件 (*.txt);;所有文件 (*.*)"
            )
            if not file_path:
                return
            try:
                result = core.add_tasks_from_file(file_path)
            except Exception as error:  # noqa: BLE001
                QMessageBox.critical(self, "导入失败", str(error))
                return
            added = result["added"]
            skipped = result["skipped"]
            failed = result["failed"]
            self._append_log(f"批量导入完成：成功 {len(added)} 条，跳过 {len(skipped)} 条，失败 {len(failed)} 条")
            if added:
                self._append_log(f"  已添加：{'、'.join(added[:10])}{'...' if len(added) > 10 else ''}")
            if failed:
                for line, msg in failed[:5]:
                    self._append_log(f"  失败：{line} → {msg}")
            self._refresh_tasks()
            QMessageBox.information(
                self, "批量导入结果",
                f"成功：{len(added)} 条\n跳过（已存在）：{len(skipped)} 条\n失败：{len(failed)} 条"
            )

        def _apply_manual_alias(self) -> None:
            identifier = self.book_input.text().strip()
            aliases = self.manual_alias_input.text().strip()
            if not identifier:
                QMessageBox.warning(self, "无法申请", "请先在上方输入 BookID 或剧名")
                return
            if not aliases:
                QMessageBox.warning(self, "无法申请", "请输入手动别名")
                return
            try:
                task = core.apply_manual_alias(identifier, aliases)
                self.manual_alias_input.clear()
                self._append_log(f"手动别名已提交：{task.detail}")
                self._refresh_tasks()
            except ValueError as error:
                QMessageBox.warning(self, "无法申请", str(error))
            except Exception as error:  # noqa: BLE001
                QMessageBox.critical(self, "申请失败", str(error))

        def _selected_task_keys(self) -> list[str]:
            rows = {index.row() for index in self.table.selectionModel().selectedRows()}
            tasks = core.tasks
            return [tasks[row].book_id for row in sorted(rows) if 0 <= row < len(tasks)]

        def _retry_selected(self) -> None:
            for task_key in self._selected_task_keys():
                core.retry_task(task_key)
            self._refresh_tasks()

        def _remove_selected(self) -> None:
            for task_key in self._selected_task_keys():
                try:
                    core.remove_task(task_key)
                except Exception as error:  # noqa: BLE001
                    QMessageBox.warning(self, "无法移除", str(error))
            self._refresh_tasks()

        def _open_output(self) -> None:
            path = core.data_root / "选剧文件夹" / "原剧视频"
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # noqa: S606

        def _uninstall(self) -> None:
            """一键卸载：生成并启动卸载批处理，软件随即退出。

            默认保留 data（任务记录与已下载剧目），可选彻底删除。
            卸载脚本运行在系统临时目录，删除程序本体后自删，不残留。
            """
            if (
                QMessageBox.question(
                    self,
                    "卸载软件",
                    "确定卸载「素材准备站」吗？\n\n"
                    "将关闭进程并删除程序本体（exe、_internal、runtime、logs）。\n"
                    "任务数据（data：已下载剧目、任务记录）默认保留。",
                )
                != QMessageBox.Yes
            ):
                return
            keep_data = (
                QMessageBox.question(
                    self,
                    "卸载软件",
                    "是否同时删除任务数据？\n"
                    "「否」= 保留 data（推荐，以后重装可继续使用）\n"
                    "「是」= 彻底删除 data（含已下载剧目，不可恢复）",
                )
                != QMessageBox.Yes
            )
            try:
                exe_dir = str(Path(sys.executable).resolve().parent)
                bat = Path(tempfile.gettempdir()) / f"uninstall_station_{os.getpid()}.bat"
                bat.write_text(build_uninstall_bat(exe_dir, keep_data=keep_data), encoding="gbk")
                subprocess.Popen(
                    ["cmd", "/c", str(bat)],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as error:  # noqa: BLE001
                QMessageBox.warning(self, "卸载失败", f"无法启动卸载脚本：{error}")
                return
            self._append_log("正在卸载程序本体…")
            QApplication.quit()
            os._exit(0)

        def _on_table_mode_changed(self, index: int) -> None:
            core.table_mode = "single" if index == 0 else "per_batch"
            core.save_state()
            self._append_log(f"表格导出模式：{'始终同一表格' if index == 0 else '每批一个新表格'}")

        def _export_to_table(self) -> None:
            """一键把当前任务列表导出为表格（CSV，Excel 可直接打开）。

            两种模式（顶部下拉选择）：
            - 始终同一表格：首次选择保存路径后记住，之后每次导出累积追加去重
              （同一 BookID/剧名更新该行，新任务追加）；删除表格文件后下次导出重新选路径。
            - 每批一个新表格：每次弹窗选择路径，默认带时间戳新文件。
            只读导出，不清除界面任务信息。
            """
            tasks = core.tasks
            if not tasks:
                QMessageBox.information(self, "下载到表格", "当前没有任务可导出")
                return
            rows = [
                {
                    "input_value": task.input_value or "",
                    "book_id": task.book_id,
                    "title": task.title or "",
                    "status_text": self._STATUS_TEXT.get(task.status, task.status),
                    "detail": task.detail or "",
                    "alias": "",
                    "alias_result": "",
                    "time_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.updated_at)),
                }
                for task in tasks
            ]
            # 从每个任务的三端别名任务.json 汇总候选别名申请结果
            for task, row in zip(tasks, rows):
                approved, summary = alias_summary(task.task_file if task.task_file else None)
                row["alias"] = approved or (task.approved_alias or "")
                row["alias_result"] = summary

            if core.table_mode == "per_batch":
                default_name = time.strftime("素材准备站任务_%Y%m%d_%H%M%S.csv")
                default_dir = self._last_export_dir or str(core.data_root)
                file_path, _ = QFileDialog.getSaveFileName(
                    self, "保存任务表格", str(Path(default_dir) / default_name),
                    "CSV 表格 (*.csv);;所有文件 (*.*)",
                )
                if not file_path:
                    return
                if not file_path.lower().endswith(".csv"):
                    file_path += ".csv"
                try:
                    count = export_tasks_to_csv(rows, file_path)
                except Exception as error:  # noqa: BLE001
                    QMessageBox.critical(self, "导出失败", f"写入表格失败：{error}")
                    return
                self._last_export_dir = str(Path(file_path).resolve().parent)
                self._append_log(f"已导出 {count} 条任务到表格：{file_path}")
                QMessageBox.information(
                    self, "下载到表格",
                    f"已导出 {count} 条任务\n保存位置：{file_path}\n（界面任务信息未做任何改动）",
                )
                return

            # ---- 始终同一表格：累积追加去重 ----
            file_path = core.table_path
            if not file_path or not Path(file_path).is_file():
                default_dir = self._last_export_dir or str(core.data_root)
                file_path, _ = QFileDialog.getSaveFileName(
                    self, "选择汇总表格（首次）", str(Path(default_dir) / "素材准备站任务汇总.csv"),
                    "CSV 表格 (*.csv);;所有文件 (*.*)",
                )
                if not file_path:
                    return
                if not file_path.lower().endswith(".csv"):
                    file_path += ".csv"
                core.table_path = file_path
                core.save_state()
            try:
                added, updated = merge_rows_into_csv(rows, file_path)
            except Exception as error:  # noqa: BLE001
                QMessageBox.critical(self, "导出失败", f"写入表格失败：{error}")
                return
            self._last_export_dir = str(Path(file_path).resolve().parent)
            self._append_log(f"已合并到汇总表格：新增 {added} 条、更新 {updated} 条 → {file_path}")
            QMessageBox.information(
                self, "下载到表格",
                f"已合并到汇总表格：新增 {added} 条、更新 {updated} 条\n"
                f"表格文件：{file_path}\n（界面任务信息未做任何改动）",
            )

        # ---------- 更新 ----------
        def _on_progress(self, book_id: str, status: str, detail: str) -> None:
            # 回调在后台线程执行，UI 操作需 marshal 到主线程
            text = f"[{book_id}] {self._STATUS_TEXT.get(status, status)}：{detail}"
            QTimer.singleShot(0, lambda: (self._append_log(text), self._refresh_tasks()))

        def _refresh_tasks(self) -> None:
            tasks = core.tasks
            self.table.setRowCount(len(tasks))
            for row, task in enumerate(tasks):
                display_input = task.input_value or task.book_id
                self.table.setItem(row, 0, QTableWidgetItem(display_input))
                self.table.setItem(row, 1, QTableWidgetItem(task.title or "-"))
                self.table.setItem(row, 2, QTableWidgetItem(self._STATUS_TEXT.get(task.status, task.status)))
                self.table.setItem(row, 3, QTableWidgetItem(task.detail or ""))
                self.table.setItem(row, 4, QTableWidgetItem(
                    time.strftime("%m-%d %H:%M", time.localtime(task.updated_at))
                ))

        def _append_log(self, message: str) -> None:
            line = f"[{time.strftime('%H:%M:%S')}] {message}"
            self._log_lines.append(line)
            self.log_view.appendPlainText(line)

        def _on_tick(self) -> None:
            try:
                core.tick()
            except Exception as error:  # noqa: BLE001
                self._append_log(f"调度异常：{error}")
            self._refresh_tasks()

        def closeEvent(self, event) -> None:  # noqa: N802
            core.request_close()
            event.accept()

    window = StationWindow()
    # 激活校验：安装版标记 或 强制授权构建（安装包内嵌的强制版）→ 必须激活；
    # 绿色版（默认构建）无标记直接运行，不受影响
    if _license_required(tool_root / "data") and not _ensure_license(tool_root / "data"):
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        try:
            log_dir = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
            (log_dir / "startup_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise
