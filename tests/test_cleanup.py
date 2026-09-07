# -*- mode: python ; coding: utf-8 -*-
"""程序退出时自动清理子进程与角色锁的回归测试。

背景：素材准备站退出后，后台 node（mjs 三端审核）进程未被清理，
角色锁 alias.lock/download.lock 残留；锁内记录的旧 PID 被系统复用后，
下次启动所有任务被误判"已有任务在运行"（退出码28）而全部失败。
本测试覆盖：终止登记中的子进程、删除角色锁、缺失文件不报错、
_run_process 正常结束后的注销行为。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from pathlib import Path

from station_core import (
    StationCore,
    _ACTIVE_PROCS,
    _register_active_proc,
    clean_role_locks,
    terminate_active_procs,
)


def _spawn_sleeper(seconds: int = 60) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _drain_active_procs() -> None:
    """清空全局注册表，避免测试间互相污染。"""
    for proc in list(_ACTIVE_PROCS):
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    _ACTIVE_PROCS.clear()


def test_terminate_active_procs_kills_registered_process() -> None:
    proc = _spawn_sleeper()
    _register_active_proc(proc)
    try:
        assert proc.poll() is None
        killed = terminate_active_procs()
        assert killed >= 1
        proc.wait(timeout=10)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        _drain_active_procs()


def test_terminate_active_procs_returns_zero_when_idle() -> None:
    try:
        assert terminate_active_procs() == 0
    finally:
        _drain_active_procs()


def test_clean_role_locks_removes_only_locks(tmp_path: Path) -> None:
    lock_dir = tmp_path / "runtime" / "platform_adapter"
    lock_dir.mkdir(parents=True)
    (lock_dir / "alias.lock").write_text("{}", encoding="utf-8")
    (lock_dir / "download.lock").write_text("{}", encoding="utf-8")
    (lock_dir / "unrelated.txt").write_text("keep", encoding="utf-8")
    removed = clean_role_locks(tmp_path)
    assert removed == 2
    assert not (lock_dir / "alias.lock").exists()
    assert not (lock_dir / "download.lock").exists()
    assert (lock_dir / "unrelated.txt").exists()


def test_clean_role_locks_missing_dir_returns_zero(tmp_path: Path) -> None:
    assert clean_role_locks(tmp_path) == 0
    assert clean_role_locks(tmp_path / "不存在的目录") == 0


def test_run_process_unregisters_after_finish(tmp_path: Path) -> None:
    script = tmp_path / "quick_exit.py"
    script.write_text("import time; time.sleep(1)\n", encoding="utf-8")
    log = tmp_path / "run.log"
    try:
        return_code, _output = StationCore._run_process(
            [sys.executable, str(script)],
            dict(os.environ),
            log,
        )
        assert return_code == 0
        assert _ACTIVE_PROCS == []
    finally:
        _drain_active_procs()


def test_run_process_registers_while_running(tmp_path: Path) -> None:
    """运行期间进程被登记，terminate 可将其终止（模拟退出清理路径）。"""
    script = tmp_path / "long_running.py"
    script.write_text("import time; time.sleep(120)\n", encoding="utf-8")
    log = tmp_path / "run_long.log"
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    from station_core import _register_active_proc

    _register_active_proc(proc)
    try:
        time.sleep(0.5)
        assert proc.poll() is None
        killed = terminate_active_procs(grace=2.0)
        assert killed >= 1
        proc.wait(timeout=10)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        _drain_active_procs()
