# -*- mode: python ; coding: utf-8 -*-
"""一键下载到表格功能的回归测试。"""

from __future__ import annotations

from pathlib import Path
import csv
import json

import pytest

from station_main import alias_summary, export_tasks_to_csv


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
