# -*- mode: python ; coding: utf-8 -*-
"""手动别名（文档/界面直接提供完整别名）不受前缀/后缀模式限制 的回归测试。

背景：用户提供完整别名（如「年年逃荒」「年年甜意」）时，应原样逐个申请，
不应受界面「固定前/后两字」开关与别名模式（prefix/suffix）影响。
此前 _generate 对手动候选强制“同批固定字一致”校验，导致后缀模式下
文档提供的不同后缀候选直接 raise 失败。
修复后：手动候选只校验“恰好4个中文字 + 互不重复”，并写入 manual 标志，
mjs 侧据此跳过固定字校验。非4字/重复候选仍拒绝（平台硬性要求，防回归）。
"""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from station_core import (
    StationCore,
    STATUS_GENERATING,
)

BOOK_ID = "7670000000000000001"


@pytest.fixture
def core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StationCore:
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "task-platform-download.mjs").write_text("// stub", encoding="utf-8")
    (adapter_dir / "run-alias-task.mjs").write_text("// stub", encoding="utf-8")
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


def _prepare_manual_task(core: StationCore, aliases: list[str]) -> None:
    info_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧" / "剧目信息"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "剧目信息.json").write_text(
        json.dumps({"title": "示例剧", "book_id": BOOK_ID}, ensure_ascii=False), encoding="utf-8",
    )
    task = core.add_book_id(BOOK_ID)
    task.manual_alias = True
    task.manual_alias_names = aliases
    task.info_file = str(info_dir / "剧目信息.json")
    core._save_state()


def test_manual_aliases_ignored_suffix_mode(core: StationCore) -> None:
    """后缀模式下，文档提供不同后缀的完整别名：不 raise、原样写入、带 manual 标志。"""
    core.set_alias_mode("suffix")
    _prepare_manual_task(core, ["年年逃荒", "年年甜意"])
    task = core._tasks[BOOK_ID]
    core._generate(task)
    assert task.status == STATUS_GENERATING
    task_file = Path(task.task_file)
    payload = json.loads(task_file.read_text(encoding="utf-8"))
    assert payload["candidates"] == ["年年逃荒", "年年甜意"]
    assert payload.get("manual") is True


def test_manual_aliases_ignored_prefix_mode(core: StationCore) -> None:
    """前缀模式下同样不受影响。"""
    core.set_alias_mode("prefix")
    _prepare_manual_task(core, ["言言神算", "言言天算"])
    task = core._tasks[BOOK_ID]
    core._generate(task)
    payload = json.loads(Path(task.task_file).read_text(encoding="utf-8"))
    assert payload["candidates"] == ["言言神算", "言言天算"]
    assert payload.get("manual") is True


def test_manual_not_4_chars_still_rejected(core: StationCore) -> None:
    """手动候选非4字仍拒绝（平台硬性要求，防回归）。"""
    core.set_alias_mode("suffix")
    _prepare_manual_task(core, ["年年崽", "年年"])
    task = core._tasks[BOOK_ID]
    with pytest.raises(ValueError, match="4个中文汉字"):
        core._generate(task)


def test_manual_duplicates_still_rejected(core: StationCore) -> None:
    """手动候选重复仍拒绝（防回归）。"""
    _prepare_manual_task(core, ["年年逃荒", "年年逃荒"])
    task = core._tasks[BOOK_ID]
    with pytest.raises(ValueError, match="不可重复"):
        core._generate(task)
