# -*- mode: python ; coding: utf-8 -*-
"""回归测试：三端别名候选校验（candidate-validation.mjs）。

背景：手动别名（文档/界面直接提供完整别名）不应受前缀/后缀模式限制，
但此前 mjs 强制校验“候选必须匹配固定字”，导致文档提供“年年逃荒”等
完整别名在后缀模式下被拦下失败。本测试确保：
1. 手动候选（manual=True）只校验“4个中文字 + 互不重复”，不同后缀也通过；
2. 自动候选（manual=False）仍强制匹配固定字（防回归）；
3. 非4字/重复候选无论手动与否都拒绝（平台硬性要求）。
"""

from __future__ import annotations

from pathlib import Path
import json
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATION_MJS = REPO_ROOT / "platform_adapter" / "candidate-validation.mjs"


def _validate(aliases: list[str], prefix: str, mode: str, manual: bool) -> tuple[bool, str]:
    script = (
        f"import {{ validateCandidates }} from {json.dumps(VALIDATION_MJS.as_uri())};"
        f"const r = validateCandidates({json.dumps(aliases)}, {json.dumps(prefix)}, "
        f"{json.dumps(mode)}, {json.dumps(manual)});"
        "console.log(JSON.stringify(r));"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert proc.returncode == 0, f"node 执行失败: {proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    return bool(result.get("ok")), str(result.get("message", ""))


def test_manual_aliases_ignored_affix_prefix_mode() -> None:
    """手动候选：前缀模式下不同后缀也通过（不受模式限制）。"""
    ok, _ = _validate(["年年逃荒", "年年甜意"], "言言", "prefix", True)
    assert ok is True


def test_manual_aliases_ignored_affix_suffix_mode() -> None:
    """手动候选：后缀模式下不同后缀也通过——本次修复核心。"""
    ok, _ = _validate(["年年逃荒", "年年甜意"], "逃荒", "suffix", True)
    assert ok is True


def test_manual_aliases_ignored_ui_fixed_affix() -> None:
    """手动候选：即使界面固定字与候选前缀不同也不受影响。"""
    ok, _ = _validate(["言言神算", "言言天算"], "年年", "prefix", True)
    assert ok is True


def test_auto_aliases_still_require_affix_prefix() -> None:
    """自动候选：前缀模式仍强制匹配固定字（防回归）。"""
    ok, message = _validate(["言言神算", "年年甜意"], "言言", "prefix", False)
    assert ok is False
    assert "必须" in message


def test_auto_aliases_still_require_affix_suffix() -> None:
    """自动候选：后缀模式仍强制匹配固定字（防回归）。"""
    ok, message = _validate(["逃荒言言", "甜意年年"], "言言", "suffix", False)
    assert ok is False
    assert "必须" in message


def test_manual_not_4_chars_rejected() -> None:
    """手动候选：非4字仍拒绝（平台硬性要求）。"""
    ok, message = _validate(["年年崽", "年年"], "年年", "prefix", True)
    assert ok is False
    assert "4个中文汉字" in message


def test_manual_duplicates_rejected() -> None:
    """手动候选：重复仍拒绝。"""
    ok, _ = _validate(["年年逃荒", "年年逃荒"], "年年", "prefix", True)
    assert ok is False
