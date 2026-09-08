# -*- mode: python ; coding: utf-8 -*-
"""素材准备站 · 密钥生成器（授权方专用，运行在授权电脑上）。

输入目标设备的设备码（支持多行批量）→ 生成授权密钥。
设备码由目标设备在「安装程序」界面一键提取后发给你。

用法：
  图形界面：双击运行 keygen.exe
  命令行：  keygen.exe --device XXXX-XXXX-XXXX-XXXX     （单台）
            keygen.exe --file devices.txt               （批量，每行一个设备码）
"""

from __future__ import annotations

import argparse
import sys

from license_utils import generate_key, is_valid_device_code


def batch_generate(text: str) -> list[tuple[str, str]]:
    """按行解析设备码并生成密钥；跳过空行/非法行（返回时标注原因）。"""
    rows: list[tuple[str, str]] = []
    for raw in text.splitlines():
        code = str(raw or "").strip()
        if not code:
            continue
        if is_valid_device_code(code):
            rows.append((code, generate_key(code)))
        else:
            rows.append((code, "⚠ 设备码格式无效"))
    return rows


def _run_cli(device: str | None, file: str | None) -> int:
    if file:
        with open(file, "r", encoding="utf-8-sig") as fh:
            source = fh.read()
    elif device:
        source = device
    else:
        source = ""
    if not source.strip():
        print("未提供设备码。用法：--device XXXX-XXXX-XXXX-XXXX 或 --file 设备码清单.txt")
        return 2
    rows = batch_generate(source)
    for code, key in rows:
        if is_valid_device_code(code):
            print(f"{code}  →  {key}")
        else:
            print(f"{code}  →  {key}", file=sys.stderr)
    return 0


def _run_gui() -> int:
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QPlainTextEdit, QPushButton, QLabel, QMessageBox,
    )
    from PySide6.QtCore import Qt

    app = QApplication(sys.argv)
    win = QMainWindow()
    win.setWindowTitle("素材准备站 · 密钥生成器")
    win.resize(560, 460)
    root = QWidget()
    layout = QVBoxLayout(root)

    layout.addWidget(QLabel("目标设备码（每行一个，可批量）："))
    input_box = QPlainTextEdit()
    input_box.setPlaceholderText("例如：\n1A2B-3C4D-5E6F-7A8B\n9C0D-1E2F-3A4B-5C6D")
    layout.addWidget(input_box, 3)

    row = QHBoxLayout()
    gen_btn = QPushButton("生成密钥")
    clear_btn = QPushButton("清空")
    copy_btn = QPushButton("复制全部结果")
    row.addWidget(gen_btn)
    row.addWidget(clear_btn)
    row.addStretch(1)
    row.addWidget(copy_btn)
    layout.addLayout(row)

    layout.addWidget(QLabel("生成的密钥（发给目标设备，安装时输入）："))
    output_box = QPlainTextEdit()
    output_box.setReadOnly(True)
    output_box.setPlaceholderText("生成结果…")
    layout.addWidget(output_box, 3)

    status_label = QLabel("")
    layout.addWidget(status_label)

    def generate() -> None:
        rows = batch_generate(input_box.toPlainText())
        if not rows:
            status_label.setText("请先输入设备码")
            return
        lines = [f"{code}  →  {key}" for code, key in rows]
        output_box.setPlainText("\n".join(lines))
        ok = sum(1 for _, k in rows if k.startswith("⚠") == False)  # noqa: E712
        bad = len(rows) - ok
        status_label.setText(f"生成 {ok} 个密钥" + (f"，{bad} 个格式无效" if bad else ""))

    def copy_all() -> None:
        text = output_box.toPlainText().strip()
        if not text:
            return
        app.clipboard().setText(text)
        QMessageBox.information(win, "已复制", "全部结果已复制到剪贴板")

    gen_btn.clicked.connect(generate)
    clear_btn.clicked.connect(lambda: (input_box.clear(), output_box.clear(), status_label.setText("")))
    copy_btn.clicked.connect(copy_all)
    win.setCentralWidget(root)
    win.show()
    app.exec()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="素材准备站密钥生成器")
    parser.add_argument("--device", help="单台设备码")
    parser.add_argument("--file", help="设备码清单文件（每行一个）")
    args = parser.parse_args()
    if args.device or args.file:
        return _run_cli(args.device, args.file)
    return _run_gui()


if __name__ == "__main__":
    sys.exit(main())
