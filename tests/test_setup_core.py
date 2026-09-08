# -*- mode: python ; coding: utf-8 -*-
"""安装器核心逻辑集成测试（小体积 zip 验证解压/激活链路）。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from license_utils import generate_key, verify_key
from setup_main import InstallWorker, write_activation


@pytest.fixture
def mini_zip(tmp_path: Path) -> Path:
    """构造迷你安装资源：素材准备站.exe + _internal 文件 + 使用说明。"""
    zip_path = tmp_path / "resources.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("素材准备站.exe", b"MZ\x90\x00")
        zf.writestr("_internal/platform_adapter/readme.txt", "适配器占位")
        zf.writestr("runtime/node/node.exe", b"MZ")
        zf.writestr("使用说明.txt", "说明")
    return zip_path


def test_install_worker_extracts(mini_zip: Path, tmp_path: Path) -> None:
    """InstallWorker 完整解压迷你资源包。"""
    target = tmp_path / "installed"
    events: list[tuple] = []

    def progress(done: int, total: int, current: str) -> None:
        events.append((done, total, current))

    worker = InstallWorker(mini_zip, target, progress)
    worker.run()
    assert not worker.error
    assert (target / "素材准备站.exe").is_file()
    assert (target / "_internal" / "platform_adapter" / "readme.txt").is_file()
    assert (target / "runtime" / "node" / "node.exe").is_file()
    assert (target / "使用说明.txt").is_file()
    assert events and events[-1][0] == events[-1][1]  # 最终进度打满


def test_write_activation_verifiable(tmp_path: Path) -> None:
    """写激活后：license.dat 设备码+密钥可被 verify_key 校验通过。"""
    target = tmp_path / "app"
    from license_utils import get_device_code

    device = get_device_code()
    key = generate_key(device)
    write_activation(target, device, key)
    license_file = target / "data" / "license.dat"
    assert license_file.is_file()
    assert (target / "data" / "licensed_mode.flag").is_file()
    saved = json.loads(license_file.read_text(encoding="utf-8"))
    assert saved["device_code"] == device
    assert verify_key(saved["device_code"], saved["key"])
