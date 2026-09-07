# -*- mode: python ; coding: utf-8 -*-
"""批量识图添加任务 + 识图元信息智能提取 回归测试。

背景：
  1. 播放页截图 OCR 结果包含大量 UI 噪声行（第1集/选集/全屏观看/分享/作者声明…），
     旧逻辑「取最长行」会选到剧情描述而不是剧名；
  2. Windows OCR 对海报/艺术字可能输出乱码，批量添加前必须过滤低质量文本；
  3. ocr.ps1 输出需强制 UTF-8（否则 Python 按 utf-8 解码 GBK 输出得到乱码）。
"""

from __future__ import annotations

from pathlib import Path

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


def _ocr_line(text: str, line: int = 1, words: list | None = None) -> dict:
    return {"line": line, "text": text, "words": [{"text": w, "conf": 0} for w in words] if words else []}


def test_parse_ocr_meta_picks_title_not_description(core: StationCore) -> None:
    """播放页截图：UI 噪声行被过滤，剧名行胜出（旧逻辑会误选剧情描述行）。"""
    lines = [
        _ocr_line("< 第 1 集", 2),
        _ocr_line("Ø 倍速", 3),
        _ocr_line("天桥乞讨，我拉二胡成顶流是首富", 4),
        _ocr_line("第 1 集 | 落魄少年陆青雨偶遇黑珍珠娱乐经．「展开」", 5),
        _ocr_line("作者声明：内容由 AI 生成", 6),
        _ocr_line("选集 · 全 120 集 · 免费观看", 7),
    ]
    meta = core.parse_ocr_meta(lines)
    assert "天桥乞讨" in meta["title"]
    assert "落魄少年" not in meta["title"]
    assert meta["episodes"] == 120


def test_parse_ocr_meta_filters_artifacts(core: StationCore) -> None:
    """剧名行首尾的播放器装饰符号被清理。"""
    lines = [_ocr_line("〈 天桥乞讨，我拉二胡成顶流是首富 〉", 1)]
    meta = core.parse_ocr_meta(lines)
    assert meta["title"].startswith("天桥乞讨")
    assert "〈" not in meta["title"]


def test_ocr_quality_rejects_garbage(core: StationCore) -> None:
    """乱码文本（上次 UI 出现的 ●♪ż 类）被判为低质量。"""
    garbage = "●●1 ●♪|♪●●●●●●●●●●½ ż ◆●◆"
    assert core._ocr_text_quality(garbage) < 0.4
    good = "天桥乞讨，我拉二胡成顶流是首富"
    assert core._ocr_text_quality(good) > 0.6


def test_parse_ocr_meta_state_tags(core: StationCore) -> None:
    """状态标签提取：完整词 + OCR 短行模糊还原（"新0"/"新囗"→"新剧"）。"""
    lines = [
        _ocr_line("新0", 1),          # "新剧"红标被 OCR 误读
        _ocr_line("热剧", 2),
        _ocr_line("天桥乞讨，我拉二胡成顶流是首富", 3),
    ]
    meta = core.parse_ocr_meta(lines)
    assert "新剧" in meta["tags"]
    assert "热剧" in meta["tags"]
    assert "天桥乞讨" in meta["title"]


def test_parse_ocr_meta_tags_not_false_positive(core: StationCore) -> None:
    """长行不做模糊还原，避免剧名中的"新"字误报为"新剧"。"""
    lines = [_ocr_line("新的开始之天桥乞讨，我拉二胡成顶流是首富", 1)]
    meta = core.parse_ocr_meta(lines)
    assert "新剧" not in meta["tags"]
    assert "新" in meta["title"]


def test_parse_ocr_meta_strips_leading_tag(core: StationCore) -> None:
    """绿色过滤后「新剧」角标与剧名连成一行，OCR 独立词「新剧」在开头 → 剧名去掉标签词。"""
    lines = [
        _ocr_line("新剧天桥乞讨，我靠拉二胡成顶流是首富", 1, words=["新剧", "天桥乞讨，我靠拉二胡成顶流是首富"]),
        _ocr_line("选集·全120集·免费观看", 2),
    ]
    meta = core.parse_ocr_meta(lines)
    assert meta["title"] == "天桥乞讨，我靠拉二胡成顶流是首富"
    assert "新剧" in meta["tags"]
    assert meta["episodes"] == 120


def test_parse_ocr_meta_keeps_tag_in_real_title(core: StationCore) -> None:
    """剧名本身包含标签词时（如「新剧情」），OCR 无独立标签词 → 不误删。"""
    lines = [_ocr_line("新剧情缘之天桥乞讨", 1, words=["新剧情缘之天桥乞讨"])]
    meta = core.parse_ocr_meta(lines)
    assert meta["title"] == "新剧情缘之天桥乞讨"


def test_add_tasks_from_images_batch(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """批量识图：识别出的任务自动添加（剧名+集数+类型），低质量图片跳过。"""
    ocr_results = {
        "a.png": [
            _ocr_line("天命神算", 1),
            _ocr_line("漫剧", 2),
            _ocr_line("全集 68 集", 3),
        ],
        "b.jpg": [
            _ocr_line("破译起手！从卡bug到诸天禁忌", 1),
            _ocr_line("科幻末世 · 115 集", 2),
        ],
        "c.png": [_ocr_line("●●1 ●♪|♪●●●●●●●●●●½ ż ◆●◆", 1)],  # 乱码，应跳过
    }
    monkeypatch.setattr(
        core, "ocr_image",
        lambda p: ocr_results.get(Path(p).name, []),
    )
    result = core.add_tasks_from_images(["a.png", "b.jpg", "c.png"])
    assert len(result["added"]) == 2
    assert len(result["skipped"]) == 1
    # a.png：剧名+68集+漫剧
    task_a = core._tasks["title:天命神算"]
    assert task_a.expected_episodes == 68
    assert task_a.content_type == "manju"
    # b.jpg：剧名+115集（无标签，保持全局/空）
    task_b = core._tasks["title:破译起手！从卡bug到诸天禁忌"]
    assert task_b.expected_episodes == 115


def test_add_tasks_from_images_duplicate_skipped(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """批量识图：重复剧名不报错，计入跳过。"""
    monkeypatch.setattr(
        core, "ocr_image",
        lambda p: [_ocr_line("天命神算", 1)],
    )
    core.add_tasks_from_images(["a.png"])
    result = core.add_tasks_from_images(["b.png"])
    assert len(result["added"]) == 0
    assert len(result["skipped"]) == 1
    assert "已在任务列表" in result["skipped"][0][1]
