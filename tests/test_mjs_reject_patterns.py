# -*- mode: python ; coding: utf-8 -*-
"""回归测试：平台拒绝文案识别（reject-patterns.mjs）。

背景：mjs 提交别名后只识别“创建成功”与“重复申请”，未识别
“与热门作品/作者/主演角色名相似度高”类红字拒绝提示，导致被拒
候选被误判为提交成功，任务卡在等待审核，最终报“平台控件故障”。

本测试用真实 node 运行 reject-patterns.mjs，确保：
1. 相似度高/侵权类提示被识别为拒绝；
2. 原有重复申请类提示仍被识别；
3. 正常文案（成功/排队/等待）不被误判。
"""

from __future__ import annotations

from pathlib import Path
import json
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PATTERNS_MJS = REPO_ROOT / "platform_adapter" / "reject-patterns.mjs"

REJECT_SAMPLES = [
    # 本次修复重点：相似度过高/侵权类
    "与热门作品/作者/主演角色名相似度高",
    "与热门作品、作者或主演角色名相似度高，请更换后重试",
    "该别名与已有作品相似度高",
    "相似度过高，涉嫌侵权风险",
    "涉嫌侵权，请更换别名",
    # 原有重复申请类（防回归）
    "你已申请此别名，请勿重复申请",
    "已有相同书名存在",
    "别名已存在",
    "该别名已被使用",
    "该别名已被他人申请",
    "已被他人申请",
    "他人已申请",
    "此别名已被占用",
    "请勿重复申请",
]

NORMAL_SAMPLES = [
    "别名创建成功",
    "已提交，等待审核",
    "正在等待三端审核；15秒后检查",
    "现有候选均未能三端通过，需要生成新的候选别名后继续",
    "等待10秒提交按钮仍未启用",
    "当前弹窗找不到发文类型选择框",
    "检测到任务台滑块/安全验证",
    "三端全部审核通过：知夏藏锋",
]


def _match(text: str) -> str | None:
    """调用 node 运行 reject-patterns.mjs 的 matchRejectText。"""
    uri = PATTERNS_MJS.as_uri()  # Windows 反斜杠路径不能直接用于 ESM import
    script = (
        f"import {{ matchRejectText }} from {json.dumps(uri)};"
        f"const r = matchRejectText({json.dumps(text)});"
        "console.log(r ? 'REJECT:' + r.source : 'OK');"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert proc.returncode == 0, f"node 执行失败: {proc.stderr}"
    out = proc.stdout.strip().splitlines()[-1]
    return out if out.startswith("REJECT:") else None


def test_patterns_mjs_exists() -> None:
    assert PATTERNS_MJS.is_file(), "缺少 platform_adapter/reject-patterns.mjs"


@pytest.mark.parametrize("text", REJECT_SAMPLES)
def test_reject_samples_are_detected(text: str) -> None:
    """平台拒绝提示必须被识别为拒绝。"""
    assert _match(text) is not None, f"未识别拒绝文案: {text}"


@pytest.mark.parametrize("text", NORMAL_SAMPLES)
def test_normal_samples_not_misjudged(text: str) -> None:
    """正常文案不得被误判为拒绝。"""
    assert _match(text) is None, f"误判为拒绝: {text}"
