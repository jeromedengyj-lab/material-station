# -*- mode: python ; coding: utf-8 -*-
"""一键下载到表格功能的回归测试。"""

from __future__ import annotations

from pathlib import Path
import csv
import json

import pytest

from station_main import alias_summary, export_tasks_to_csv, merge_rows_into_csv


def test_export_tasks_to_csv_writes_all_rows(tmp_path: Path) -> None:
    tasks = [
        {
            "input_value": "7680496498710154265",
            "book_id": "7680496498710154265",
            "title": "拒绝挖骨退婚，我以混沌重瞳证道",
            "status_text": "完成",
            "detail": "三端全部审核通过：证道言言",
            "alias": "证道言言",
            "alias_result": "证道言言=成功",
            "time_text": "2026-09-07 00:38:00",
        },
        {
            "input_value": "官宦有崽，杳杳弯到亲爱的喵",
            "book_id": "title:官宦有崽，杳杳弯到亲爱的喵",
            "title": "官宦有崽，杳杳弯到亲爱的喵",
            "status_text": "需人工处理",
            "detail": "现有候选均未三端通过，需要重新生成候选后重试",
            "alias": "",
            "alias_result": "知夏断案=失败；知夏守名=失败；知夏拒婚=失败",
            "time_text": "2026-09-07 00:41:00",
        },
    ]
    target = tmp_path / "导出" / "任务表.csv"
    count = export_tasks_to_csv(tasks, target)
    assert count == 2
    assert target.is_file()
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) == 3  # 表头 + 2 行
    assert rows[0] == ["序号", "输入", "剧名", "状态", "详情", "别名", "别名结果", "时间"]
    assert rows[1][1] == "7680496498710154265"
    assert rows[1][3] == "完成"
    assert rows[1][4] == "三端全部审核通过：证道言言"
    assert rows[1][5] == "证道言言"
    assert rows[1][6] == "证道言言=成功"
    assert rows[2][2] == "官宦有崽，杳杳弯到亲爱的喵"
    assert rows[2][6] == "知夏断案=失败；知夏守名=失败；知夏拒婚=失败"


def test_export_tasks_to_csv_empty(tmp_path: Path) -> None:
    target = tmp_path / "empty.csv"
    count = export_tasks_to_csv([], target)
    assert count == 0
    assert target.is_file()
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows == [["序号", "输入", "剧名", "状态", "详情", "别名", "别名结果", "时间"]]


def test_export_tasks_to_csv_creates_parent_dir(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c" / "t.csv"
    count = export_tasks_to_csv([{"input_value": "1", "title": "t", "status": "queued"}], target)
    assert count == 1
    assert target.is_file()


def _write_alias_task(tmp_path: Path, payload: dict) -> Path:
    target = tmp_path / "三端别名任务.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def test_alias_summary_approved_and_failed(tmp_path: Path) -> None:
    target = _write_alias_task(tmp_path, {
        "approved_alias": "知夏断案",
        "candidate_rows": [
            {"order": 1, "alias": "知夏断案", "status": "approved", "platforms": {}},
            {"order": 2, "alias": "知夏守名", "status": "rejected", "platforms": {}},
            {"order": 3, "alias": "知夏拒婚", "status": "failed", "platforms": {}},
        ],
    })
    approved, summary = alias_summary(target)
    assert approved == "知夏断案"
    assert summary == "知夏断案=成功；知夏守名=失败-被拒；知夏拒婚=失败"


def test_alias_summary_no_rows_uses_candidates(tmp_path: Path) -> None:
    target = _write_alias_task(tmp_path, {
        "approved_alias": "",
        "candidates": ["年年炉灶", "年年红砖"],
        "candidate_rows": [],
    })
    approved, summary = alias_summary(target)
    assert approved == ""
    assert summary == "年年炉灶=未申请；年年红砖=未申请"


def test_alias_summary_missing_file(tmp_path: Path) -> None:
    approved, summary = alias_summary(tmp_path / "不存在.json")
    assert approved == ""
    assert summary == "（任务文件缺失）"


def test_alias_summary_none() -> None:
    approved, summary = alias_summary(None)
    assert approved == ""
    assert summary == ""


# ---------- 始终同一表格：累积合并 ----------

def _row(input_value: str, title: str, status: str = "完成", alias: str = "") -> dict:
    return {
        "input_value": input_value,
        "book_id": input_value,
        "title": title,
        "status_text": status,
        "detail": "",
        "alias": alias,
        "alias_result": f"{alias}=成功" if alias else "",
        "time_text": "2026-09-08 10:00:00",
    }


def _read_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def test_merge_rows_into_csv_creates_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "汇总.csv"
    added, updated = merge_rows_into_csv([_row("a", "剧A", alias="年年甲"), _row("b", "剧B")], target)
    assert (added, updated) == (2, 0)
    rows = _read_rows(target)
    assert rows[0] == ["序号", "输入", "剧名", "状态", "详情", "别名", "别名结果", "时间"]
    assert [r[1] for r in rows[1:]] == ["a", "b"]
    assert rows[1][5] == "年年甲"


def test_merge_rows_into_csv_updates_existing(tmp_path: Path) -> None:
    target = tmp_path / "汇总.csv"
    merge_rows_into_csv([_row("a", "剧A", alias="年年甲")], target)
    # 第二批：a 状态/别名更新 + 新任务 c
    added, updated = merge_rows_into_csv(
        [_row("a", "剧A", status="需人工处理", alias="年年乙"), _row("c", "剧C")], target
    )
    assert (added, updated) == (1, 1)
    rows = _read_rows(target)
    assert len(rows) == 3  # 表头 + 2 行（不重复）
    assert rows[1][3] == "需人工处理"
    assert rows[1][5] == "年年乙"
    assert rows[2][1] == "c"
    assert rows[1][0] == "1" and rows[2][0] == "2"  # 序号连续


def test_merge_rows_into_csv_appends_without_touching_others(tmp_path: Path) -> None:
    target = tmp_path / "汇总.csv"
    merge_rows_into_csv([_row("a", "剧A"), _row("b", "剧B")], target)
    added, updated = merge_rows_into_csv([_row("d", "剧D")], target)
    assert (added, updated) == (1, 0)
    rows = _read_rows(target)
    assert [r[1] for r in rows[1:]] == ["a", "b", "d"]
    assert rows[3][2] == "剧D"


def test_merge_rows_into_csv_preserves_existing_file_when_unreadable(tmp_path: Path) -> None:
    target = tmp_path / "汇总.csv"
    target.write_text("\xff\xfe\x00\x01\x02", encoding="latin-1")  # 非法内容
    added, updated = merge_rows_into_csv([_row("a", "剧A")], target)
    assert (added, updated) == (1, 0)
    rows = _read_rows(target)
    assert rows[1][1] == "a"
