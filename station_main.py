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
import sys
import time

from pathlib import Path

STATION_VERSION = "v1.0"

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

def _tool_root() -> Path:
    configured = str(os.environ.get("MANJU_TOOL_ROOT", "") or "").strip()
    if configured:
        return Path(configured).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


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
        STATUS_APPLYING, STATUS_WAITING_MANUAL, STATUS_DONE, STATUS_FAILED,
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
            STATUS_APPLYING: "申请三端别名",
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

            manual_row = QHBoxLayout()
            manual_row.addWidget(QLabel("手动别名："))
            self.manual_alias_input = QLineEdit()
            self.manual_alias_input.setPlaceholderText("输入别名，多个用逗号分隔，如：年年炉灶,年年红砖")
            manual_row.addWidget(self.manual_alias_input, 1)
            manual_apply_btn = QPushButton("申请手动别名")
            manual_apply_btn.clicked.connect(self._apply_manual_alias)
            manual_row.addWidget(manual_apply_btn)
            manual_row.addWidget(QLabel("（需先有剧目信息，跳过AI生成直接申请三端）"))
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

        def _add_task(self) -> None:
            value = self.book_input.text().strip()
            if not value:
                return
            try:
                task = core.add_task(value)
                self.book_input.clear()
                label = f"BookID {task.input_value}" if task.input_type == "book_id" else f"剧名「{task.input_value}」"
                self._append_log(f"已添加 {label}")
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

        def _export_to_table(self) -> None:
            """一键把当前任务列表导出为表格（CSV，Excel 可直接打开）。

            路径由用户选择；只读导出，不清除界面任务信息。
            """
            tasks = core.tasks
            if not tasks:
                QMessageBox.information(self, "下载到表格", "当前没有任务可导出")
                return
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
