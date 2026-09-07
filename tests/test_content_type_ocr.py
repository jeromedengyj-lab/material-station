# -*- mode: python ; coding: utf-8 -*-
"""内容类型（网文/漫剧/短剧）+ 期望集数 + 识图（OCR 解析）回归测试。

需求背景：
  1. 平台搜索同名剧时返回多种内容类型（网文/漫剧/短剧），需按标签精确匹配；
  2. 同名同标签（如「破译起手！从卡bug到诸天禁忌」剧情68集 vs 科幻末世115集）需按集数区分；
  3. 新增「识图添加」入口：识别图中剧名/集数/标签，自动填入（可修正）。
约束：不破坏现有 BookID/剧名/txt 批量输入功能（默认空类型=旧行为，mjs 兜底漫剧）。
"""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from station_core import StationCore


BOOK_ID = "7670000000000000001"


@pytest.fixture
def core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StationCore:
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "task-platform-download.mjs").write_text("// stub", encoding="utf-8")
    (adapter_dir / "run-alias-task.mjs").write_text("// stub", encoding="utf-8")
    (adapter_dir / "ocr.ps1").write_text("# stub", encoding="utf-8")
    node_file = tmp_path / "node.exe"
    node_file.write_bytes(b"MZ")
    value = StationCore(
        tool_root=tmp_path,
        data_root=tmp_path / "data",
        adapter_dir=adapter_dir,
        node_command=str(node_file),
    )
    monkeypatch.setattr(value, "_ensure_model_service", lambda: True)
    return value


def test_default_type_and_episodes_empty(core: StationCore) -> None:
    """默认：类型/集数均不限制（旧行为），mjs 兜底漫剧。"""
    assert core.content_type == ""
    assert core.expected_episodes == 0
    task = core.add_book_id(BOOK_ID)
    assert task.content_type == ""
    assert task.expected_episodes == 0


def test_set_content_type_valid(core: StationCore) -> None:
    core.set_content_type("wangwen")
    assert core.content_type == "wangwen"


def test_set_content_type_invalid(core: StationCore) -> None:
    with pytest.raises(ValueError, match="只支持"):
        core.set_content_type("漫画")


def test_set_expected_episodes_valid(core: StationCore) -> None:
    core.set_expected_episodes(115)
    assert core.expected_episodes == 115


def test_set_expected_episodes_invalid(core: StationCore) -> None:
    with pytest.raises(ValueError, match="整数"):
        core.set_expected_episodes("abc")


def test_add_task_inherits_global_config(core: StationCore) -> None:
    """添加任务时继承当前全局类型与集数（UI 设置后添加生效）。"""
    core.set_content_type("manju")
    core.set_expected_episodes(115)
    task = core.add_book_id(BOOK_ID)
    assert task.content_type == "manju"
    assert task.expected_episodes == 115


def test_download_passes_type_and_episodes(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """_download 向 mjs 透传 --content-type 与 --expected-episodes。"""
    core.set_content_type("manju")
    core.set_expected_episodes(115)
    task = core.add_book_id(BOOK_ID)
    captured: dict = {}
    monkeypatch.setattr(
        core, "_run_process",
        lambda cmd, env, log_path: captured.update(cmd=cmd) or (0, "ok"),
    )
    monkeypatch.setattr(core, "_locate_info", lambda *a: None)  # 仅构造 cmd，不继续
    core._download(task)
    cmd = captured["cmd"]
    assert "--content-type" in cmd and cmd[cmd.index("--content-type") + 1] == "manju"
    assert "--expected-episodes" in cmd and cmd[cmd.index("--expected-episodes") + 1] == "115"


def test_download_defaults_to_manju(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """未设置类型时仍传 manju（mjs 旧行为兜底）。"""
    task = core.add_book_id(BOOK_ID)
    captured: dict = {}
    monkeypatch.setattr(
        core, "_run_process",
        lambda cmd, env, log_path: captured.update(cmd=cmd) or (0, "ok"),
    )
    monkeypatch.setattr(core, "_locate_info", lambda *a: None)
    core._download(task)
    cmd = captured["cmd"]
    assert cmd[cmd.index("--content-type") + 1] == "manju"
    assert "--expected-episodes" not in cmd


def test_parse_ocr_meta_extracts_all(core: StationCore) -> None:
    """识图解析：剧名（最长行）+ 集数（68集）+ 标签（漫剧）；OCR 中文间空格被清理。"""
    lines = [
        {"line": 1, "text": "破 译 起 手 从 卡 bug 到 诸 天 禁 忌"},
        {"line": 2, "text": "漫剧"},
        {"line": 3, "text": "剧情·68集"},
    ]
    meta = core.parse_ocr_meta(lines)
    # 中文间空格被清理；英文词间空格保留（mjs 搜索归一化会去掉全部空白，不影响匹配）
    assert meta["title"] == "破译起手从卡 bug 到诸天禁忌"
    assert meta["episodes"] == 68
    assert meta["content_type"] == "manju"


def test_parse_ocr_meta_fallback(core: StationCore) -> None:
    """识别不到集数/标签时返回空，不报错（交由用户手动补）。"""
    lines = [{"line": 1, "text": "只有剧名的一行"}]
    meta = core.parse_ocr_meta(lines)
    assert meta["title"] == "只有剧名的一行"
    assert meta["episodes"] == 0
    assert meta["content_type"] == ""


def test_ocr_image_missing_file(core: StationCore) -> None:
    with pytest.raises(FileNotFoundError):
        core.ocr_image(tmp_path := core.data_root / "不存在.png")
