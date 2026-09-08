# -*- mode: python ; coding: utf-8 -*-
"""密钥生成器回归测试。"""

from __future__ import annotations

from license_utils import generate_key, verify_key

from keygen_main import batch_generate


def test_batch_generate_single() -> None:
    rows = batch_generate("1A2B-3C4D-5E6F-7A8B")
    assert len(rows) == 1
    code, key = rows[0]
    assert verify_key(code, key)


def test_batch_generate_multi_skips_invalid() -> None:
    text = "1A2B-3C4D-5E6F-7A8B\n\n不是设备码\n9C0D-1E2F-3A4B-5C6D"
    rows = batch_generate(text)
    assert len(rows) == 3
    assert rows[0][0] == "1A2B-3C4D-5E6F-7A8B"
    assert "无效" in rows[1][1]
    assert rows[2][0] == "9C0D-1E2F-3A4B-5C6D"
    assert verify_key(rows[2][0], rows[2][1])


def test_batch_generate_empty() -> None:
    assert batch_generate("") == []
    assert batch_generate("\n  \n") == []
