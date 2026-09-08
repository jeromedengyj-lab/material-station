# -*- mode: python ; coding: utf-8 -*-
"""激活必要性判断（绿色版豁免 / 强制授权版）回归测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

import _build_config
from station_main import _license_required


def test_green_free_without_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """绿色版（无 flag、无环境变量、_build_config=False）：不要求激活。"""
    monkeypatch.delenv("MANJU_FORCE_LICENSE", raising=False)
    assert not _license_required(tmp_path)


def test_installed_flag_requires(tmp_path: Path) -> None:
    """安装/激活后（有 licensed_mode.flag）：要求激活校验。"""
    (tmp_path / "licensed_mode.flag").write_text("1", encoding="utf-8")
    assert _license_required(tmp_path)


def test_env_force_requires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量 MANJU_FORCE_LICENSE=1：无 flag 也强制激活。"""
    monkeypatch.setenv("MANJU_FORCE_LICENSE", "1")
    assert _license_required(tmp_path)


def test_build_config_force_requires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构建常量 FORCE_LICENSE=True（安装包内嵌资源）：强制激活。"""
    monkeypatch.delenv("MANJU_FORCE_LICENSE", raising=False)
    monkeypatch.setattr(_build_config, "FORCE_LICENSE", True)
    assert _license_required(tmp_path)


def test_env_empty_string_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量为空串：按构建常量（默认绿色版豁免）。"""
    monkeypatch.setenv("MANJU_FORCE_LICENSE", "")
    monkeypatch.setattr(_build_config, "FORCE_LICENSE", False)
    assert not _license_required(tmp_path)
