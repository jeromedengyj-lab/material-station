# -*- mode: python ; coding: utf-8 -*-
"""素材准备站独立别名生成模块（纯标准库，零第三方依赖）。

从主程序 manju_editor/material_workflow.py 精简而来，只保留
generate_alias_candidates / write_alias_task 及其依赖的辅助函数，
去掉 cover_workflow / hook_text / alias_lookup 等重依赖
（faster-whisper / rapidocr / cv2 / onnxruntime / PIL 均不再需要）。

台账（已用别名）读取改为基于传入的 shared_root（素材准备站自己的 data 目录），
不再依赖主程序的 漫剧别名状态表.xlsx。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import time
import urllib.request

# 模型名单（与主程序 hook_text.py 一致；素材准备站内置 qwen3:4b-instruct / qwen3:8b）
OLLAMA_MODEL = "qwen3:4b-instruct"
QUALITY_MODEL = "qwen3.5:9b"
LEGACY_QUALITY_MODEL = "qwen3:8b"
_MODEL_CANDIDATES = (QUALITY_MODEL, LEGACY_QUALITY_MODEL, OLLAMA_MODEL)

# 禁止使用的泛化万能词（与主程序保持一致）
_FORBIDDEN_GENERIC = (
    "逆袭、归来、破局、藏锋、翻盘、扬眉、觉醒、重生、战神、狂婿、霸总、封神、无双、至尊、龙帅、天下、苍穹、乾坤、风云、传说"
)


def _query_installed_models() -> list[str]:
    """可靠读取 Ollama 模型列表；服务繁忙时允许一次较长重试。"""
    for timeout in (4, 12):
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=timeout) as response:
                return [item.get("name", "") for item in json.load(response).get("models", [])]
        except Exception:
            continue
    return []


def normalize_alias_prefix(value: object = "知夏") -> str:
    prefix = re.sub(r"[^\u4e00-\u9fff]", "", str(value or "").strip())
    if not re.fullmatch(r"[\u4e00-\u9fff]{2}", prefix):
        raise ValueError("别名前缀必须恰好是2个中文汉字")
    return prefix


def valid_alias_candidates(values: list[str], used: set[str] | None = None,
                           prefix: str = "知夏", mode: str = "prefix") -> list[str]:
    used = used or set()
    if prefix:
        affix = normalize_alias_prefix(prefix)
        pattern = (re.escape(affix) + r"[\u4e00-\u9fff]{2}") if mode == "prefix" else (r"[\u4e00-\u9fff]{2}" + re.escape(affix))
    else:
        pattern = r"[\u4e00-\u9fff]{4}"  # 不限制前缀/后缀，只要求4个中文字
    result: list[str] = []
    for value in values:
        alias = re.sub(r"[《》\s，,、；;。.!！?？]", "", str(value or ""))
        if re.fullmatch(pattern, alias) and alias not in used and alias not in result:
            result.append(alias)
    return result


def _reserved_aliases(shared_root: Path) -> set[str]:
    """读取素材准备站自己的别名全局账本（mjs 端 ALIAS_LEDGER 写入）。

    账本格式：jsonl，每行 {"alias": "..."}。
    """
    ledger = shared_root / "选剧文件夹" / "工作流状态" / "别名全局账本.jsonl"
    reserved: set[str] = set()
    try:
        lines = ledger.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return reserved
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        alias = str(row.get("alias", "") or "").strip().strip("《》") if isinstance(row, dict) else ""
        if alias:
            reserved.add(alias)
    return reserved


def generate_alias_candidates(title: str, intro: str, count: int = 3,
                              excluded: set[str] | None = None,
                              prefix: str = "知夏", mode: str = "prefix",
                              shared_root: str | Path | None = None) -> list[str]:
    """Generate a complete batch, retrying with exclusions instead of blocking the UI.
    mode: 'prefix' = 前缀+两字（默认），'suffix' = 两字+后缀。
    shared_root: 素材准备站 data 目录，用于读取已用别名账本；为 None 时跳过台账过滤。
    """
    prefix = normalize_alias_prefix(prefix) if prefix else ""
    installed = _query_installed_models()
    model = next((name for name in _MODEL_CANDIDATES
                  if any(item.startswith(name) for item in installed)), "")
    if not model:
        raise RuntimeError("本地文字模型未运行，无法生成三端别名候选")
    used = set()
    if shared_root is not None:
        used |= _reserved_aliases(Path(shared_root))
    used |= {str(value).strip() for value in (excluded or set()) if str(value).strip()}
    schema = {"type": "object", "properties": {"aliases": {"type": "array", "items": {"type": "string"}}},
              "required": ["aliases"]}
    aliases: list[str] = []
    angles = ("身份伪装、隐藏实力与反差", "冲突、翻盘与人物行动", "情绪、命运与故事结果", "换用全新意象")
    for attempt, angle in enumerate(angles):
        forbidden = sorted(used | set(aliases))
        request_count = max(6, (count - len(aliases)) * 3)
        if prefix:
            fixed_pos = "前两个字" if mode == "prefix" else "后两个字"
            free_pos = "后两个字" if mode == "prefix" else "前两个字"
            example_alias = f"{prefix}后两字" if mode == "prefix" else f"后两字{prefix}"
            fixed_rule = f"{fixed_pos}固定为“{prefix}”；{free_pos}必须从剧情中提取具体意象——人物身份、职业工种、核心动作、关键物品、场景地点、食物道具等，必须让人一看就联想到这部剧的具体内容"
        else:
            example_alias = "四字别名"
            fixed_rule = "四个字必须从剧情中提取具体意象——人物身份、职业工种、核心动作、关键物品、场景地点、食物道具等，必须让人一看就联想到这部剧的具体内容；不要固定前缀或后缀"
        prompt = f"""你在为一部中国漫剧生成推广别名。
原剧名：{title}
平台简介：{intro or '暂无简介，只能根据剧名提炼'}
本轮角度：{angle}
规则：只返回JSON；生成{request_count}个彼此不同的候选；每个必须恰好四个中文汉字；{fixed_rule}；严禁使用“{_FORBIDDEN_GENERIC}”这类泛化万能词；不要照抄原剧名，不要使用生僻乱码。
严禁返回这些已占用或已尝试词：{'、'.join(forbidden[-120:]) or '无'}
返回格式：{{"aliases":["{example_alias}","{example_alias}","{example_alias}"]}}
/no_think"""
        payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "think": False,
                              "format": schema,
                              "options": {"temperature": min(.92, .62 + attempt * .08), "num_predict": 500}},
                             ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=payload,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
        except Exception:
            if attempt == len(angles) - 1 and not aliases:
                raise
            continue
        raw = result.get("response", "") or result.get("thinking", "")
        try:
            values = json.loads(raw).get("aliases", [])
        except (ValueError, AttributeError):
            if prefix:
                extract_pattern = (re.escape(prefix) + r"[\u4e00-\u9fff]{2}") if mode == "prefix" else (r"[\u4e00-\u9fff]{2}" + re.escape(prefix))
            else:
                extract_pattern = r"[\u4e00-\u9fff]{4}"
            values = re.findall(extract_pattern, raw)
        for alias in valid_alias_candidates(list(values), used | set(aliases), prefix, mode=mode):
            aliases.append(alias)
            if len(aliases) == count:
                return aliases
    raise RuntimeError(f"本地模型经过{len(angles)}轮仍只生成{len(aliases)}个合规且未占用的别名")


def read_alias_state(folder: str | Path) -> dict:
    path = Path(folder) / "剧目信息" / "三端别名任务.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_dir_title(value) -> str:
    """把剧名清洗成 Windows 合法目录名，规则与 mjs safe() 完全一致：
    [<>:"/\\|?*] 及控制字符 → _；去掉结尾 . 和空格；超 120 截断；空则用占位名。
    保证 Python 侧按 title 拼路径时能命中 mjs 下载阶段实际创建的目录
    （如「末日病宠:丧尸前任赖上我了」→「末日病宠_丧尸前任赖上我了」）。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "").strip())
    name = re.sub(r"[. ]+$", "", name)
    return name[:120] or "未命名"


def alias_task_path(project, preferred_series_root: str | Path | None = None) -> Path:
    """Return the single durable alias-review manifest for a project."""
    folder_value = str(getattr(project, "series_folder", "") or "").strip()
    if folder_value:
        folder = Path(folder_value)
    else:
        root = Path(preferred_series_root) if preferred_series_root else Path.cwd()
        title = str(getattr(project, "platform_title", "") or getattr(project, "title", "")).strip()
        if not title:
            raise ValueError("缺少剧名，无法建立三端别名任务表")
        folder = root / _safe_dir_title(title)
    return folder / "剧目信息" / "三端别名任务.json"


def write_alias_task(project, values: list[str], preferred_series_root: str | Path | None = None,
                     prefix: str = "知夏", mode: str = "prefix") -> Path:
    """Persist candidates before launching review; the file is the runtime source of truth."""
    if prefix:
        prefix = normalize_alias_prefix(prefix)
    candidates = valid_alias_candidates(values, prefix=prefix, mode=mode)
    if len(candidates) != 3 or len(set(candidates)) != 3:
        raise ValueError("三端别名任务必须包含3个互不重复的合规候选")
    target = alias_task_path(project, preferred_series_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    old = read_alias_state(target.parent.parent)
    title = str(getattr(project, "platform_title", "") or getattr(project, "title", "")).strip()
    old_rows = old.get("candidate_rows", []) if isinstance(old.get("candidate_rows"), list) else []
    old_by_alias = {str(row.get("alias", "")): row for row in old_rows if isinstance(row, dict)}
    old_names = [str(row.get("alias", "")) for row in old_rows if isinstance(row, dict)]
    if not old_names:
        old_names = [str(value) for value in old.get("candidates", []) if isinstance(value, str)]
    old_prefix = str(old.get("alias_prefix", "") or (old_names[0][:2] if old_names else ""))
    same_identity = (str(old.get("title", "")) == title
                     and str(old.get("book_id", "") or "") == str(getattr(project, "book_id", "") or ""))
    same_prefix = old_prefix == prefix
    same_task = same_identity and same_prefix and old_names == candidates
    replenishing = same_identity and same_prefix and old.get("status") == "needs_more_candidates"
    if replenishing:
        combined = list(old_names)
        combined.extend(alias for alias in candidates if alias not in combined)
        candidates = combined
    rows = []
    for order, alias in enumerate(candidates, 1):
        previous = old_by_alias.get(alias, {})
        rows.append({
            "order": order,
            "alias": alias,
            "status": str(previous.get("status", "queued") or "queued"),
            "platforms": previous.get("platforms", {}) if isinstance(previous.get("platforms"), dict) else {},
        })
    preserve_progress = same_task or replenishing
    payload = {
        "version": 2,
        "task_id": str(getattr(project, "task_id", "") or ""),
        "batch_id": str(getattr(project, "workflow_batch_id", "") or ""),
        "title": title,
        "book_id": str(getattr(project, "book_id", "") or ""),
        "alias_prefix": prefix,
        "alias_mode": mode,
        "status": "queued" if replenishing else (str(old.get("status", "queued")) if same_task else "queued"),
        "current_index": int(old.get("current_index", 0) or 0) if preserve_progress else 0,
        "candidate_rows": rows,
        # Read-only compatibility for older tools; v2 consumers use candidate_rows.
        "candidates": candidates,
        "history": old.get("history", []) if preserve_progress and isinstance(old.get("history"), list) else [],
        "platforms": old.get("platforms", {}) if preserve_progress and isinstance(old.get("platforms"), dict) else {},
        "approved_alias": str(old.get("approved_alias", "")) if preserve_progress else "",
        "created_at": old.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return target
