# -*- mode: python ; coding: utf-8 -*-
"""「先批量提交三端别名，最后统一审核」的回归测试。

背景：旧逻辑每个任务提交三端后原地轮询审核（可能数小时），
后续任务全部被堵住。新逻辑：
  1. 提交阶段（--submit-only）：提交完即走，任务进入统一审核队列（STATUS_SUBMITTED）
  2. 主队列清空后自动转入审核阶段，逐个轮询（STATUS_REVIEWING → 通过/失败）
"""

from __future__ import annotations

from pathlib import Path
import json
import threading
import time

import pytest

from station_core import (
    StationCore,
    STATUS_APPLYING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_REVIEWING,
    STATUS_SUBMITTED,
    STATUS_WAITING_MANUAL,
)

BOOK_ID = "7670000000000000001"
BOOK_ID_2 = "7670000000000000002"


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


def _write_task_file(task_dir: Path, book_id: str, status: str = "waiting_review",
                     approved_alias: str = "") -> Path:
    info_dir = task_dir / "剧目信息"
    info_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "title": "示例剧",
        "book_id": book_id,
        "status": status,
        "candidate_rows": [{"order": 1, "alias": "知夏藏锋", "status": "waiting_review"}],
        "candidates": ["知夏藏锋"],
        "approved_alias": approved_alias,
    }
    task_file = info_dir / "三端别名任务.json"
    task_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return task_file


def _add_submitted_task(core: StationCore, book_id: str) -> None:
    """构造一个已提交待审核任务：直接入 _tasks + _review_queue，不依赖 mjs。"""
    task = core.add_book_id(book_id)
    task.status = STATUS_SUBMITTED
    task.task_file = str(_write_task_file(
        core.data_root / "选剧文件夹" / "原剧视频" / "示例剧", book_id))
    if book_id in core._queue:
        core._queue.remove(book_id)
    if book_id not in core._review_queue:
        core._review_queue.append(book_id)
    core._save_state()


def test_apply_submit_only_waiting_review_goes_to_review_queue(core: StationCore) -> None:
    """提交模式：mjs 返回 0 且任务文件为 waiting_review → 状态 SUBMITTED 并进入审核队列。"""
    task = core.add_book_id(BOOK_ID)
    task.task_file = str(_write_task_file(
        core.data_root / "选剧文件夹" / "原剧视频" / "示例剧", BOOK_ID))
    task.status = STATUS_APPLYING
    core._run_process = lambda *a, **k: (0, "")  # type: ignore[method-assign]
    core._apply(task, submit_only=True)
    assert task.status == STATUS_SUBMITTED
    assert "待统一审核" in task.detail
    assert BOOK_ID in core._review_queue


def test_apply_submit_only_approved_marks_done(core: StationCore) -> None:
    """提交模式：若平台已审核通过 → 直接完成，不进审核队列。"""
    task = core.add_book_id(BOOK_ID)
    task.task_file = str(_write_task_file(
        core.data_root / "选剧文件夹" / "原剧视频" / "示例剧", BOOK_ID,
        status="completed", approved_alias="知夏藏锋"))
    task.status = STATUS_APPLYING
    core._run_process = lambda *a, **k: (0, "")  # type: ignore[method-assign]
    core._apply(task, submit_only=True)
    assert task.status == STATUS_DONE
    assert task.approved_alias == "知夏藏锋"
    assert BOOK_ID not in core._review_queue


@pytest.mark.parametrize("exit_code,expected", [
    (29, STATUS_WAITING_MANUAL),
    (30, STATUS_WAITING_MANUAL),
    (25, STATUS_WAITING_MANUAL),
    (99, STATUS_FAILED),
])
def test_apply_submit_only_exit_codes_not_submitted(
        core: StationCore, exit_code: int, expected: str) -> None:
    """提交模式异常退出：保持原有映射，不误进审核队列。"""
    task = core.add_book_id(BOOK_ID)
    task.task_file = str(_write_task_file(
        core.data_root / "选剧文件夹" / "原剧视频" / "示例剧", BOOK_ID))
    task.status = STATUS_APPLYING
    log_dir = core.data_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = core._log_file(task, "alias")
    log_file.write_text("mjs 输出\n", encoding="utf-8")
    core._run_process = lambda *a, **k: (exit_code, "")  # type: ignore[method-assign]
    core._apply(task, submit_only=True)
    assert task.status == expected
    assert BOOK_ID not in core._review_queue


def _wait_until_idle(core: StationCore, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with core._lock:
            if core._running is None:
                return
        time.sleep(0.05)
    raise AssertionError("调度未在预期时间内回到空闲状态")


def test_tick_submits_all_then_reviews(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """完整调度：任务1提交完（SUBMITTED）→ 任务2继续提交 → 主队列空 → 自动统一审核任务1。"""
    # 任务1：手动别名提交（走到 _apply submit_only 即停）
    t1 = core.add_book_id(BOOK_ID)
    t1.manual_alias = True
    t1.task_file = str(_write_task_file(
        core.data_root / "选剧文件夹" / "原剧视频" / "示例剧", BOOK_ID))
    # 任务2：正常排队
    t2 = core.add_book_id(BOOK_ID_2)
    t2.manual_alias = True
    t2.task_file = str(_write_task_file(
        core.data_root / "选剧文件夹" / "原剧视频" / "示例剧", BOOK_ID_2))

    calls = []

    def fake_apply(task, submit_only=False):
        calls.append((task.book_id, submit_only))
        task.status = STATUS_SUBMITTED if submit_only else STATUS_DONE
        task.task_file = task.task_file or ""
        if submit_only:
            if task.book_id not in core._review_queue:
                core._review_queue.append(task.book_id)

    monkeypatch.setattr(core, "_apply", fake_apply)

    # 第一轮 tick：任务1开始提交
    core.tick()
    _wait_until_idle(core)
    assert calls == [(BOOK_ID, True)]
    assert t1.status == STATUS_SUBMITTED
    assert BOOK_ID in core._review_queue

    # 第二轮 tick：任务2开始提交
    core.tick()
    _wait_until_idle(core)
    assert calls[-1] == (BOOK_ID_2, True)

    # 第三轮 tick：主队列空 → 进入统一审核，审核任务1（watch 模式）
    core.tick()
    _wait_until_idle(core)
    assert calls[-1] == (BOOK_ID, False)  # 审核任务1用非提交模式
    assert t1.status == STATUS_DONE
    assert BOOK_ID not in core._review_queue

    # 第四轮 tick：审核任务2
    core.tick()
    _wait_until_idle(core)
    assert calls[-1] == (BOOK_ID_2, False)
    assert t2.status == STATUS_DONE
    assert BOOK_ID_2 not in core._review_queue


def test_review_marks_reviewing_then_done(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """统一审核：_review 先置 REVIEWING，审核通过后置 DONE 并移出审核队列。"""
    _add_submitted_task(core, BOOK_ID)
    task = core._tasks[BOOK_ID]
    seen = []

    def fake_apply(t, submit_only=False):
        seen.append((t.status, submit_only))
        t.status = STATUS_DONE
        t.detail = "三端全部审核通过：知夏藏锋"

    monkeypatch.setattr(core, "_apply", fake_apply)
    core._review(task)
    assert seen == [(STATUS_REVIEWING, False)]
    assert task.status == STATUS_DONE
    # 注意：从审核队列移除由 _run_review_async 的 finally 负责（见 tick 测试）


def test_review_queue_persisted_across_reload(core: StationCore, tmp_path: Path) -> None:
    """审核队列必须持久化：重建核心后 SUBMITTED 任务回到审核队列。"""
    _add_submitted_task(core, BOOK_ID)
    adapter_dir = tmp_path / "adapter2"
    adapter_dir.mkdir()
    (adapter_dir / "task-platform-download.mjs").write_text("// stub", encoding="utf-8")
    (adapter_dir / "run-alias-task.mjs").write_text("// stub", encoding="utf-8")
    node_file = tmp_path / "node2.exe"
    node_file.write_bytes(b"MZ")
    core2 = StationCore(
        tool_root=tmp_path,
        data_root=tmp_path / "data",
        adapter_dir=adapter_dir,
        node_command=str(node_file),
    )
    assert core2._tasks[BOOK_ID].status == STATUS_SUBMITTED
    assert BOOK_ID in core2._review_queue


def test_retry_submitted_task_goes_to_review_queue(core: StationCore) -> None:
    """重试已提交任务：应回到统一审核队列（不重复提交）。"""
    _add_submitted_task(core, BOOK_ID)
    core.retry_task(BOOK_ID)
    assert core._tasks[BOOK_ID].status == STATUS_QUEUED
    assert BOOK_ID in core._review_queue
    assert BOOK_ID not in core._queue


def test_remove_task_also_removes_from_review_queue(core: StationCore) -> None:
    _add_submitted_task(core, BOOK_ID)
    core.remove_task(BOOK_ID)
    assert core._tasks.get(BOOK_ID) is None
    assert BOOK_ID not in core._review_queue
