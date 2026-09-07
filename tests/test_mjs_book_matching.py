# -*- mode: python ; coding: utf-8 -*-
"""剧目定位（剧名+内容类型标签+集数）匹配纯函数 book-matching.mjs 回归测试。

背景：平台搜索同名剧时返回多种内容类型（网文/漫剧/短剧）或同名同标签
不同集数（如「破译起手！从卡bug到诸天禁忌」剧情68集 vs 科幻末世115集），
旧逻辑不区分类型导致“发现N个同名结果已停止”。新逻辑：
  1. 剧名精确/模糊命中；
  2. 按内容类型标签过滤（网文/漫剧/短剧）；
  3. 按集数精确过滤；
  4. 仍不唯一则交给调用方报错（禁止猜选）。
"""

from __future__ import annotations

from pathlib import Path
import json
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[1]
MATCHING_MJS = REPO_ROOT / "platform_adapter" / "book-matching.mjs"


def _pick(books: list[dict], title: str, opts: dict | None = None) -> dict:
    script = (
        f"import {{ pickUniqueBook }} from {json.dumps(MATCHING_MJS.as_uri())};"
        f"console.log(JSON.stringify(pickUniqueBook({json.dumps(books)}, {json.dumps(title)}, "
        f"{json.dumps(opts or {})})));"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert proc.returncode == 0, f"node 执行失败: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _book(name: str, book_id: str, episodes: int, type_name: str = "漫剧") -> dict:
    return {
        "book_name": name, "book_id": book_id,
        "chapter_num": episodes, "content_type_name": type_name,
    }


SAME_NAME = "破译起手！从卡bug到诸天禁忌"
MIXED = [
    _book(SAME_NAME, "1", 68, "漫剧"),
    _book(SAME_NAME, "2", 115, "漫剧"),
    _book(SAME_NAME, "3", 999, "网文"),
    _book("另一本书", "4", 50, "漫剧"),
]


def test_type_filter_picks_manju_only() -> None:
    """同名多类型：选漫剧类型后只剩漫剧候选。"""
    r = _pick(MIXED, SAME_NAME, {"contentType": "manju"})
    ids = [c["book_id"] for c in r["filtered"]]
    assert ids == ["1", "2"]


def test_type_filter_picks_wangwen() -> None:
    """同名多类型：选网文类型后只剩网文候选。"""
    r = _pick(MIXED, SAME_NAME, {"contentType": "wangwen"})
    assert [c["book_id"] for c in r["filtered"]] == ["3"]


def test_type_plus_episodes_picks_unique() -> None:
    """同名同标签不同集数：类型+集数精确定位到唯一——本次需求核心。"""
    r = _pick(MIXED, SAME_NAME, {"contentType": "manju", "expectedEpisodes": 115})
    assert [c["book_id"] for c in r["filtered"]] == ["2"]


def test_episodes_alone_filters_same_type() -> None:
    """同类型下仅集数过滤。"""
    r = _pick(MIXED, SAME_NAME, {"expectedEpisodes": 68})
    assert [c["book_id"] for c in r["filtered"]] == ["1"]


def test_no_type_no_episodes_keeps_all_same_name() -> None:
    """不带任何筛选：同名全部保留（交由调用方报错，不猜选）。"""
    r = _pick(MIXED, SAME_NAME, {})
    assert len(r["byTitle"]) == 3
    assert len(r["filtered"]) == 3


def test_strict_name_match_preferred() -> None:
    """精确剧名优先于去标点模糊匹配。"""
    r = _pick(MIXED, SAME_NAME, {})
    assert r["byTitle"][0]["book_id"] == "1"


def test_unknown_type_not_blocking() -> None:
    """未知类型 key 不拦截（兼容旧行为）。"""
    r = _pick(MIXED, SAME_NAME, {"contentType": "manhua"})
    assert len(r["filtered"]) == 3


def test_empty_type_not_blocking() -> None:
    """空类型（不限）不拦截——默认值兜底回归：不选类型=不按类型过滤。"""
    r = _pick(MIXED, SAME_NAME, {"contentType": ""})
    assert len(r["filtered"]) == 3
