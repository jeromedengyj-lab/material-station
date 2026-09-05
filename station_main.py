"""素材准备站 - 独立软件入口（PySide6 精简界面）。

把「下载原剧视频 / 下载封面 / 申请三端关键词别名」从漫剧自动任务中心剥离，
以手动 BookID 为任务来源，绿色文件夹部署，内置 Node / Ollama / 模型。

运行方式：
    开发：python station_main.py
    打包：build_station.ps1 产出绿色文件夹后双击 素材准备站.exe
"""

from __future__ import annotations

import os
import sys
import time

from pathlib import Path

STATION_VERSION = "v1.0"

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
    # 开发环境：material_station/platform_adapter
    return Path(__file__).resolve().parent / "platform_adapter"


def main() -> int:
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QTableWidget, QTableWidgetItem, QPushButton, QLineEdit, QLabel,
        QPlainTextEdit, QHeaderView, QMessageBox,
    )
    from PySide6.QtCore import Qt, QTimer

    # 打包（PyInstaller pathex=src）用包路径；开发直接跑脚本时回退同目录导入
    try:
        from material_station.station_core import (
            StationCore, STATUS_QUEUED, STATUS_DOWNLOADING, STATUS_GENERATING,
            STATUS_APPLYING, STATUS_WAITING_MANUAL, STATUS_DONE, STATUS_FAILED,
        )
    except ImportError:
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
            retry_btn = QPushButton("重试选中")
            retry_btn.clicked.connect(self._retry_selected)
            top.addWidget(retry_btn)
            remove_btn = QPushButton("移除选中")
            remove_btn.clicked.connect(self._remove_selected)
            top.addWidget(remove_btn)
            open_dir_btn = QPushButton("打开产出目录")
            open_dir_btn.clicked.connect(self._open_output)
            top.addWidget(open_dir_btn)
            layout.addLayout(top)

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

        # ---------- 更新 ----------
        def _on_progress(self, book_id: str, status: str, detail: str) -> None:
            self._append_log(f"[{book_id}] {self._STATUS_TEXT.get(status, status)}：{detail}")
            self._refresh_tasks()

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
