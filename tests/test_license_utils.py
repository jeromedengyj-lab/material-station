# -*- mode: python ; coding: utf-8 -*-
"""授权模块（设备码/密钥）回归测试。"""

from __future__ import annotations

import pytest

from license_utils import (
    generate_key, get_device_code, is_valid_device_code, verify_key,
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
