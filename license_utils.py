# -*- mode: python ; coding: utf-8 -*-
"""素材准备站授权模块：设备码提取 + 密钥生成/校验（离线）。

- 设备码：基于本机稳定硬件标识（主板序列号/机器UUID/CPU ID）哈希生成，
  同一台设备多次提取结果一致；安装器界面「一键提取」使用。
- 密钥：HMAC-SHA256 派生，由「密钥生成器」离线生成，
  授权方只需目标设备的设备码即可签发。
  支持授权时长：
    · 永久密钥：XXXX-XXXX-XXXX-XXXX（与旧格式兼容）
    · 限时密钥：YYYYMMDD-XXXX-XXXX-XXXX-XXXX（前 8 位为到期日，
      后 16 位为 设备码+到期日 的 HMAC 签名，日期被篡改即校验失败）
- 校验：verify_key 核对签名且未到期；程序启动时核对 本机设备码 + 激活
  文件密钥 均匹配才放行，过期会提示重新激活。

安全说明：本方案为离线本地授权（面向自有工具分发），密钥算法内置在
安装包中可被逆向，不适用于强安全要求的商业授权场景。
"""

from __future__ import annotations

import hashlib
import hmac
import platform
import re
import subprocess
import uuid as _uuid
from datetime import date, datetime, timedelta

# 授权密钥（签发密钥）。需要换授权规则时修改此常量并重新签发。
_LICENSE_SECRET = "mjs-station-license-v1"

_DEVICE_CACHE: str | None = None

_KEY_PERMANENT_LEN = 16      # XXXX-XXXX-XXXX-XXXX
_KEY_TIMED_LEN = 24          # YYYYMMDD + 16 位签名


# ---------- 设备码 ----------

def _powershell_text(script: str) -> str:
    """运行 PowerShell 取一行文本；失败返回空串（不抛异常）。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        value = (proc.stdout or "").strip()
        return value if value and value.lower() not in ("", "none", "null", "to be filled by o.e.m.") else ""
    except Exception:  # noqa: BLE001
        return ""


def _hardware_signature() -> str:
    """稳定硬件指纹：主板序列号 + 机器UUID + CPU ID（任一缺失用其他补）。"""
    parts: list[str] = []
    for script, name in (
        (r"(Get-CimInstance Win32_BaseBoard).SerialNumber", "board"),
        (r"(Get-CimInstance Win32_ComputerSystemProduct).UUID", "uuid"),
        (r"(Get-CimInstance Win32_Processor).ProcessorId", "cpu"),
    ):
        value = _powershell_text(script)
        if value:
            parts.append(f"{name}:{value}")
    if not parts:  # 非 Windows / 无权限：退化为稳定节点标识
        parts = [f"node:{platform.node()}", f"mac:{_uuid.getnode()}"]
    return "|".join(parts)


def _digest_to_code(digest: bytes) -> str:
    """16 字节摘要 → XXXX-XXXX-XXXX-XXXX（大写十六进制，便于口述/抄写）。"""
    text = digest.hex().upper()
    return "-".join(text[i:i + 4] for i in range(0, 16, 4))


def get_device_code() -> str:
    """本机设备码（同一台设备稳定不变）。"""
    global _DEVICE_CACHE
    if _DEVICE_CACHE:
        return _DEVICE_CACHE
    signature = _hardware_signature()
    digest = hashlib.sha256(signature.encode("utf-8")).digest()
    _DEVICE_CACHE = _digest_to_code(digest[:16])
    return _DEVICE_CACHE


# ---------- 密钥 ----------

def _normalize_key(key: str) -> str:
    """密钥归一：去空白/连字符，转大写。"""
    return re.sub(r"[^0-9A-Za-z]", "", str(key or "")).upper()


def _normalize_code(device_code: str) -> str:
    return _normalize_key(device_code)


def _sig16(payload: str) -> str:
    """payload 的 HMAC 前 16 位十六进制（大写）。"""
    digest = hmac.new(_LICENSE_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return digest.hex().upper()[:16]


def generate_key(device_code: str, days: int | None = None) -> str:
    """由设备码生成授权密钥（离线，确定性）。

    days 为 None/<=0 时生成永久密钥（4 段，与旧格式兼容）；
    days>0 时生成限时密钥（YYYYMMDD-XXXX-XXXX-XXXX-XXXX），
    到期日为签发日 + days 天。
    """
    code = _normalize_code(device_code)
    if days is None or days <= 0:
        return _digest_to_code(hmac.new(_LICENSE_SECRET.encode("utf-8"), code.encode("utf-8"), hashlib.sha256).digest()[:16])
    expiry = date.today() + timedelta(days=int(days))
    date_str = expiry.strftime("%Y%m%d")
    sig = _sig16(f"{code}|{date_str}")
    return f"{date_str}-{sig[:4]}-{sig[4:8]}-{sig[8:12]}-{sig[12:16]}"


def key_expiry(key: str) -> date | None:
    """解析密钥到期日：永久密钥返回 None；无法解析返回 None。"""
    norm = _normalize_key(key)
    if len(norm) == _KEY_TIMED_LEN:
        try:
            return datetime.strptime(norm[:_KEY_TIMED_LEN - 16], "%Y%m%d").date()
        except ValueError:
            return None
    return None


def verify_key(device_code: str, key: str) -> bool:
    """校验 设备码+密钥：签名匹配 且 未到期（永久密钥只看签名）。"""
    status, _ = key_status(device_code, key)
    return status == "valid"


def key_status(device_code: str, key: str) -> tuple[str, date | None]:
    """返回 (状态, 到期日)：valid / invalid / expired；到期日永久为 None。"""
    code = _normalize_code(device_code)
    norm = _normalize_key(key)
    if not code or not norm:
        return "invalid", None
    if len(norm) == _KEY_PERMANENT_LEN:
        expected = generate_key(device_code)
        return ("valid", None) if norm == _normalize_key(expected) else ("invalid", None)
    if len(norm) == _KEY_TIMED_LEN:
        date_str = norm[:8]
        try:
            expiry = datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            return "invalid", None
        if _sig16(f"{code}|{date_str}") != norm[8:]:
            return "invalid", expiry
        if date.today() > expiry:
            return "expired", expiry
        return "valid", expiry
    return "invalid", None


def is_valid_device_code(code: str) -> bool:
    """设备码格式：4 组 4 位大写十六进制。"""
    return bool(re.fullmatch(r"[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}", str(code or "").strip().upper()))
