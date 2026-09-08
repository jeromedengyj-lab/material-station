# -*- mode: python ; coding: utf-8 -*-
"""授权模块（设备码/密钥）回归测试。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from license_utils import (
    generate_key, get_device_code, is_valid_device_code, key_expiry,
    key_status, verify_key,
)


def test_device_code_format() -> None:
    code = get_device_code()
    assert is_valid_device_code(code), code


def test_device_code_stable() -> None:
    assert get_device_code() == get_device_code()


def test_generate_key_deterministic() -> None:
    code = get_device_code()
    assert generate_key(code) == generate_key(code)
    assert is_valid_device_code(generate_key(code))


def test_verify_ok() -> None:
    code = get_device_code()
    key = generate_key(code)
    assert verify_key(code, key)


def test_verify_lowercase_and_dashes_ok() -> None:
    """密钥大小写/连字符不影响校验。"""
    code = get_device_code()
    key = generate_key(code).lower().replace("-", "")
    assert verify_key(code, key)


def test_verify_wrong_device_fails() -> None:
    """错误设备码的密钥不能通过（跨设备签发的密钥不可互用）。"""
    code = get_device_code()
    other = "ABCD-1234-EF56-7890"  # 另一台设备的码
    wrong_key = generate_key(other)
    assert not verify_key(code, wrong_key)


def test_verify_empty_fails() -> None:
    assert not verify_key("", "")
    assert not verify_key(get_device_code(), "")
    assert not verify_key("", generate_key(get_device_code()))


def test_invalid_device_code_rejected() -> None:
    assert not is_valid_device_code("abc")
    assert not is_valid_device_code("1234-1234-1234")
    assert is_valid_device_code("abcd-ef12-3456-7890")


# ---------- 授权时长密钥 ----------

def test_timed_key_format_and_expiry() -> None:
    code = get_device_code()
    key = generate_key(code, days=30)
    parts = key.split("-")
    assert len(parts) == 5, key
    assert len(parts[0]) == 8
    expiry = key_expiry(key)
    assert expiry == date.today() + timedelta(days=30)


def test_timed_key_verifies() -> None:
    code = get_device_code()
    key = generate_key(code, days=7)
    assert verify_key(code, key)
    assert key_status(code, key)[0] == "valid"


def test_timed_key_tampered_date_invalid() -> None:
    """篡改到期日（如改远）导致签名不匹配 → invalid。"""
    code = get_device_code()
    key = generate_key(code, days=7)
    tampered = "20990101" + key[key.index("-") + 1:]
    assert key_status(code, tampered)[0] == "invalid"


def test_timed_key_tampered_sig_invalid() -> None:
    code = get_device_code()
    key = generate_key(code, days=30)
    tampered = key[:9] + ("0" if key[9] != "0" else "1") + key[10:]
    assert key_status(code, tampered)[0] == "invalid"


def test_expired_key_status() -> None:
    """过去日期的限时密钥 → expired（构造：用过去到期日签发）。"""
    code = get_device_code()
    past = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
    from license_utils import _normalize_code, _sig16
    sig = _sig16(f"{_normalize_code(code)}|{past}")
    expired_key = f"{past}-{sig[:4]}-{sig[4:8]}-{sig[8:12]}-{sig[12:16]}"
    status, expiry = key_status(code, expired_key)
    assert status == "expired"
    assert expiry == date.today() - timedelta(days=10)
    assert not verify_key(code, expired_key)


def test_permanent_key_still_valid_with_new_verify() -> None:
    """旧格式永久密钥在新校验逻辑下仍有效。"""
    code = get_device_code()
    key = generate_key(code)  # days=None → 4 段永久
    assert key_expiry(key) is None
    assert verify_key(code, key)
