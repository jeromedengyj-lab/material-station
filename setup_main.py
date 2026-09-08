# -*- mode: python ; coding: utf-8 -*-
"""素材准备站 · 安装程序（目标设备使用）。

流程：
  1) 欢迎页：自动提取本机设备码（一键复制），输入授权密钥并校验；
  2) 选择安装位置（默认 D:\\素材准备站）；
  3) 解压 resources.zip 安装（进度显示，几分钟）；
  4) 创建桌面/开始菜单快捷方式 + 写入激活信息 + 安装标记；
  5) 完成，可立即启动。

资源要求：与 setup.exe 同目录放置 resources.zip（绿色版内容，不含 data）。
安装版激活：程序启动时校验本机设备码与激活文件密钥；绿色版无安装标记则直接运行。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import zipfile
from pathlib import Path

from license_utils import get_device_code, key_status

APP_NAME = "素材准备站"
DEFAULT_INSTALL_DIR = Path("D:/素材准备站")
RESOURCE_ZIP_NAME = "resources.zip"


def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resources_zip() -> Path:
    return _bundle_dir() / RESOURCE_ZIP_NAME


def _total_uncompressed(zf: zipfile.ZipFile) -> int:
    return sum(info.file_size for info in zf.infolist())


class InstallWorker(threading.Thread):
    """后台解压安装：向进度回调报告 (done, total, current_file)。"""

    def __init__(self, zip_path: Path, target: Path, progress):
        super().__init__(daemon=True)
        self.zip_path = zip_path
        self.target = target
        self.progress = progress
        self.error: str = ""

    def run(self) -> None:  # noqa: C901
        try:
            with zipfile.ZipFile(self.zip_path, "r") as zf:
                total = _total_uncompressed(zf)
                done = 0
                for info in zf.infolist():
                    out = self.target / info.filename
                    if info.is_dir():
                        out.mkdir(parents=True, exist_ok=True)
                        continue
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(out, "wb") as dst:
                        while True:
                            chunk = src.read(1 << 20)
                            if not chunk:
                                break
                            dst.write(chunk)
                            done += len(chunk)
                            self.progress(done, total, info.filename)
                self.progress(total, total, "完成")
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.progress(-1, -1, self.error)


def write_activation(target: Path, device_code: str, key: str) -> None:
    """写入激活文件 + 安装标记（程序启动据此进入激活校验模式）。"""
    data_dir = target / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    license_file = data_dir / "license.dat"
    license_file.write_text(
        json.dumps({"device_code": device_code, "key": key}, ensure_ascii=False),
        encoding="utf-8",
    )
    (data_dir / "licensed_mode.flag").write_text("1", encoding="utf-8")


def create_shortcuts(target: Path) -> None:
    """桌面 + 开始菜单快捷方式（指向 exe）。"""
    exe = target / f"{APP_NAME}.exe"
    if not exe.is_file():
        return
    try:
        import winshell  # noqa: F401  # pywin32
    except Exception:  # noqa: BLE001
        return  # 无 pywin32 时跳过快捷方式（不影响安装）
    try:
        from win32com.client import Dispatch
    except Exception:  # noqa: BLE001
        return
    import winshell

    targets: list[Path] = []
    try:
        desktop = Path(winshell.desktop())
        targets.append(desktop)
    except Exception:  # noqa: BLE001
        pass
    try:
        menu = Path(winshell.programs_directory()) / APP_NAME
        menu.mkdir(parents=True, exist_ok=True)
        targets.append(menu)
    except Exception:  # noqa: BLE001
        pass
    for folder in targets:
        try:
            link = folder / f"{APP_NAME}.lnk"
            shell = Dispatch("WScript.Shell")
            shortcut = shell.CreateShortCut(str(link))
            shortcut.Targetpath = str(exe)
            shortcut.WorkingDirectory = str(target)
            shortcut.save()
        except Exception:  # noqa: BLE001
            pass


def _run_gui() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QLineEdit, QPushButton, QProgressBar, QStackedWidget,
        QFileDialog, QMessageBox, QCheckBox,
    )

    app = QApplication(sys.argv)
    win = QMainWindow()
    win.setWindowTitle(f"{APP_NAME} · 安装程序")
    win.resize(640, 460)
    stack = QStackedWidget()
    win.setCentralWidget(stack)

    device_code = get_device_code()
    install_dir = Path(os.environ.get("USERPROFILE", "C:/")) / APP_NAME
    install_dir = DEFAULT_INSTALL_DIR if DEFAULT_INSTALL_DIR.exists() else install_dir

    # ---------- 第 1 页：设备码 + 密钥 ----------
    page1 = QWidget()
    p1 = QVBoxLayout(page1)
    p1.addWidget(QLabel(f"欢迎安装 {APP_NAME}（安装版需授权密钥才能使用）"))
    p1.addSpacing(12)
    p1.addWidget(QLabel("① 本机设备码（点击复制，发给授权方生成密钥）："))
    code_row = QHBoxLayout()
    code_edit = QLineEdit(device_code)
    code_edit.setReadOnly(True)
    code_edit.setFixedWidth(280)
    copy_btn = QPushButton("一键复制设备码")
    code_row.addWidget(code_edit)
    code_row.addWidget(copy_btn)
    code_row.addStretch(1)
    p1.addLayout(code_row)
    p1.addSpacing(12)
    p1.addWidget(QLabel("② 输入授权密钥（由密钥生成器根据上面设备码生成）："))
    key_row = QHBoxLayout()
    key_edit = QLineEdit()
    key_edit.setPlaceholderText("XXXX-XXXX-XXXX-XXXX")
    key_edit.setFixedWidth(280)
    check_btn = QPushButton("校验密钥")
    key_row.addWidget(key_edit)
    key_row.addWidget(check_btn)
    key_row.addStretch(1)
    p1.addLayout(key_row)
    p1.addSpacing(8)
    key_state = QLabel("")
    p1.addWidget(key_state)
    p1.addStretch(1)
    next1 = QPushButton("下一步：选择安装位置")
    next1.setEnabled(False)
    p1.addWidget(next1)
    stack.addWidget(page1)

    # ---------- 第 2 页：安装位置 ----------
    page2 = QWidget()
    p2 = QVBoxLayout(page2)
    p2.addWidget(QLabel("选择安装位置（建议默认，需约 8GB 空间）："))
    dir_row = QHBoxLayout()
    dir_edit = QLineEdit(str(install_dir))
    browse_btn = QPushButton("浏览…")
    dir_row.addWidget(dir_edit)
    dir_row.addWidget(browse_btn)
    p2.addLayout(dir_row)
    p2.addSpacing(8)
    license_check = QCheckBox("我确认已获得本机设备码对应的授权密钥")
    license_check.setChecked(True)
    p2.addWidget(license_check)
    p2.addStretch(1)
    nav_row2 = QHBoxLayout()
    back2 = QPushButton("上一步")
    install_btn = QPushButton("开始安装")
    nav_row2.addWidget(back2)
    nav_row2.addStretch(1)
    nav_row2.addWidget(install_btn)
    p2.addLayout(nav_row2)
    stack.addWidget(page2)

    # ---------- 第 3 页：安装进度 ----------
    page3 = QWidget()
    p3 = QVBoxLayout(page3)
    p3.addWidget(QLabel("正在安装，请稍候（约 8GB，需几分钟）…"))
    progress = QProgressBar()
    progress.setRange(0, 1000)
    p3.addWidget(progress)
    file_label = QLabel("")
    p3.addWidget(file_label)
    p3.addStretch(1)
    stack.addWidget(page3)

    # ---------- 第 4 页：完成 ----------
    page4 = QWidget()
    p4 = QVBoxLayout(page4)
    done_label = QLabel("")
    p4.addWidget(done_label)
    p4.addStretch(1)
    nav4 = QHBoxLayout()
    finish_btn = QPushButton("完成")
    launch_btn = QPushButton("立即启动")
    nav4.addStretch(1)
    nav4.addWidget(launch_btn)
    nav4.addWidget(finish_btn)
    p4.addLayout(nav4)
    stack.addWidget(page4)

    def copy_code() -> None:
        app.clipboard().setText(device_code)
        QMessageBox.information(win, "已复制", f"设备码已复制：\n{device_code}")

    def check_key() -> None:
        key = key_edit.text().strip()
        status, expiry = key_status(device_code, key)
        if status == "valid":
            key_state.setText("✅ 密钥有效" + (f"（{expiry} 到期）" if expiry else "（永久）"))
            key_state.setStyleSheet("color: #1a7f37;")
            next1.setEnabled(True)
        elif status == "expired":
            key_state.setText(f"❌ 密钥已过期（{expiry} 到期），请向授权方获取新密钥")
            key_state.setStyleSheet("color: #c62828;")
            next1.setEnabled(False)
        else:
            key_state.setText("❌ 密钥无效，请检查设备码与密钥是否对应")
            key_state.setStyleSheet("color: #c62828;")
            next1.setEnabled(False)

    copy_btn.clicked.connect(copy_code)
    check_btn.clicked.connect(check_key)
    key_edit.returnPressed.connect(check_key)
    next1.clicked.connect(lambda: stack.setCurrentIndex(1))
    back2.clicked.connect(lambda: stack.setCurrentIndex(0))
    browse_btn.clicked.connect(lambda: dir_edit.setText(
        QFileDialog.getExistingDirectory(win, "选择安装位置", str(dir_edit.text())) or str(dir_edit.text())
    ))

    worker_ref: dict = {"worker": None}

    def on_progress(done: int, total: int, current: str) -> None:
        if done < 0:
            QMessageBox.critical(win, "安装失败", f"解压安装出错：{current}")
            stack.setCurrentIndex(1)
            install_btn.setEnabled(True)
            return
        ratio = min(1.0, done / max(1, total))
        progress.setValue(int(ratio * 1000))
        file_label.setText(f"已安装 {done // (1 << 20)} MB / {total // (1 << 20)} MB · {os.path.basename(str(current))}")

    def start_install() -> None:
        target = Path(str(dir_edit.text()).strip())
        if not target:
            QMessageBox.warning(win, "路径无效", "请选择安装位置")
            return
        if target.exists() and any(target.iterdir()):
            answer = QMessageBox.question(
                win, "目录非空",
                f"目录 {target} 已存在且非空，继续将覆盖其中文件。是否继续？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        zip_path = _resources_zip()
        if not zip_path.is_file():
            QMessageBox.critical(win, "缺少安装资源", f"未找到 {zip_path.name}，请将其与安装程序放在同一目录。")
            return
        target.mkdir(parents=True, exist_ok=True)
        install_btn.setEnabled(False)
        stack.setCurrentIndex(2)
        worker = InstallWorker(zip_path, target, on_progress)
        worker_ref["worker"] = worker
        worker.start()
        threading.Thread(target=wait_install, args=(target,), daemon=True).start()

    def wait_install(target: Path) -> None:
        worker = worker_ref["worker"]
        worker.join()
        if worker.error:
            return  # on_progress 已提示
        try:
            write_activation(target, device_code, key_edit.text().strip())
            create_shortcuts(target)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(win, "提示", f"安装完成，但写入激活信息失败：{exc}")
        done_label.setText(
            f"✅ {APP_NAME} 安装完成\n\n安装位置：{target}\n"
            f"本机设备码：{device_code}\n\n"
            "桌面/开始菜单已创建快捷方式，点击「立即启动」开始使用。"
        )
        launch_btn.setProperty("target", str(target))
        stack.setCurrentIndex(3)

    install_btn.clicked.connect(start_install)

    def finish() -> None:
        win.close()

    def launch() -> None:
        target = Path(launch_btn.property("target"))
        exe = target / f"{APP_NAME}.exe"
        if exe.is_file():
            os.startfile(str(exe))  # noqa: S606
        win.close()

    finish_btn.clicked.connect(finish)
    launch_btn.clicked.connect(launch)

    win.show()
    app.exec()
    return 0


def main() -> int:
    if not _resources_zip().is_file():
        print(f"警告：未找到 {RESOURCE_ZIP_NAME}（与安装程序同目录），安装将无法进行。")
    return _run_gui()


if __name__ == "__main__":
    sys.exit(main())
