"""素材准备站核心调度器。

三功能（下载原剧视频 / 下载封面 / 申请三端关键词别名）从漫剧自动任务中心剥离后，
在本独立软件中以「BookID 手动输入」为任务来源串行执行：

    添加 BookID → 下载原剧+封面（task-platform-download.mjs --book-id）
              → 生成候选别名（本地 Ollama qwen3:4b-instruct）
              → 申请三端别名（run-alias-task.mjs --task-file）

绿色文件夹部署：整个文件夹拷到另一台设备即可运行，Node / Ollama / 模型均内置，
唯一外部依赖是系统安装的 Chrome 或 Edge（平台登录需首次在目标设备手动完成一次）。

本模块不依赖 PySide6，便于用 pytest 做回归测试。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

# 三端别名候选模型优先级（与主程序 hook_text.py 常量一致；独立软件打包只带最小模型）
_QUALITY_MODEL = "qwen3.5:9b"
_LEGACY_QUALITY_MODEL = "qwen3:8b"
_OLLAMA_MODEL = "qwen3:4b-instruct"

_ALIAS_PREFIX = "知夏"
_BOOK_ID_PATTERN = r"^\d{16,20}$"

# 任务状态
STATUS_QUEUED = "queued"          # 排队等待
STATUS_PENDING_MANUAL = "pending_manual"  # 等待手动输入别名（手动模式下导入的任务不自动执行）
STATUS_DOWNLOADING = "downloading"  # 下载原剧+封面中
STATUS_GENERATING = "generating"    # 生成候选别名中
STATUS_APPLYING = "applying"        # 申请三端别名中
STATUS_WAITING_MANUAL = "waiting_manual"  # 需要人工处理（滑块验证/平台故障）
STATUS_DONE = "done"              # 三端全部通过
STATUS_FAILED = "failed"          # 失败（可重试）

# mjs 别名流程退出码（与 task-platform-download.mjs 保持一致）
_ALIAS_EXIT_MANUAL = 29   # 人工滑块验证
_ALIAS_EXIT_RETRY = 30    # 平台控件故障，稍后重试
_ALIAS_EXIT_NEED_MORE = 25  # 候选均未通过，需要生成新候选


def _is_book_id(value: str) -> bool:
    import re
    return bool(re.fullmatch(_BOOK_ID_PATTERN, str(value or "").strip()))


def _normalize_title(value: str) -> str:
    """剧名归一化：NFKC + 去全部空白，与 mjs 端 normalizeTitle 对齐。"""
    import unicodedata
    return "".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _safe_filename(value: str) -> str:
    """替换 Windows 文件名非法字符，用于日志文件名。"""
    import re
    return re.sub(r'[\\/:*?"<>|]', "_", str(value or ""))


@dataclass
class StationTask:
    """一个任务的完整流程：下载 → 候选 → 三端别名。

    book_id 字段兼作任务字典 key：
      - BookID 模式（input_type="book_id"）：book_id = 真实平台 BookID
      - 剧名模式（input_type="title"）：book_id = "title:<剧名>" 临时 key，
        下载完成后真实 BookID 回填到 resolved_book_id
    """

    book_id: str
    status: str = STATUS_QUEUED
    title: str = ""
    detail: str = ""
    error: str = ""
    task_dir: str = ""       # {data}/选剧文件夹/原剧视频/{title}
    cover_file: str = ""
    info_file: str = ""      # 剧目信息/剧目信息.json
    task_file: str = ""      # 剧目信息/三端别名任务.json
    approved_alias: str = ""
    input_type: str = "book_id"   # "book_id" 或 "title"
    input_value: str = ""          # 用户输入原始值（BookID 或剧名）
    resolved_book_id: str = ""     # 剧名模式下载后回填的真实平台 BookID
    manual_alias: bool = False     # True=手动别名申请，跳过下载和生成直接进入三端申请
    manual_alias_name: str = ""     # 手动指定的别名（从txt解析或UI输入，兼容单个别名）
    manual_alias_names: list = field(default_factory=list)  # 手动指定的多个别名（作为候选依次尝试）
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "StationTask":
        keep = {k: v for k, v in value.items() if k in cls.__dataclass_fields__}
        return cls(**keep)


class StationCore:
    """调度核心：串行执行 BookID 任务队列，状态持久化在数据根目录。"""

    def __init__(
        self,
        tool_root: str | Path,
        data_root: str | Path | None = None,
        adapter_dir: str | Path | None = None,
        node_command: str | Path | None = None,
        model_prefix: str = _ALIAS_PREFIX,
        model_candidates: tuple[str, ...] = (_QUALITY_MODEL, _LEGACY_QUALITY_MODEL, _OLLAMA_MODEL),
    ):
        self.tool_root = Path(tool_root).resolve()
        self.data_root = Path(data_root).resolve() if data_root else self.tool_root / "data"
        self.adapter_dir = Path(adapter_dir).resolve() if adapter_dir else self.tool_root / "platform_adapter"
        self.node_command = str(node_command) if node_command else self._find_node()
        self.model_prefix = model_prefix
        self.model_candidates = list(model_candidates)
        self._tasks: dict[str, StationTask] = {}
        self._queue: list[str] = []
        self._running: StationTask | None = None
        self._close_requested = False
        self._log: list[str] = []
        self._on_progress = None  # 回调(book_id, status, detail)
        self._lock = threading.Lock()
        self.alias_prefix = ""  # 用户手动指定的别名前缀（前两字），空则用默认
        self.alias_mode = "prefix"  # prefix=前缀+两字，suffix=两字+后缀
        self.fix_affix = False  # True=必须指定固定字（alias_prefix生效），False=AI自由生成4个字
        self.download_only = False  # True=只下载不申请别名
        self.alias_only = False  # True=跳过下载直接申请别名（需已有剧目信息）
        self.manual_mode = False  # True=手动别名模式：新任务不自动执行，等用户手动输入别名
        self._ensure_dirs()
        self._load_state()

    def request_close(self) -> None:
        self._close_requested = True

    # ---------- 路径与工具 ----------
    def _ensure_dirs(self) -> None:
        for path in (
            self.data_root / "选剧文件夹" / "原剧视频",
            self.data_root / "封面-原图",
            self.data_root / "选剧文件夹" / "工作流状态",
            self.data_root / "logs",
        ):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _find_node() -> str:
        try:
            from manju_editor.runtime import bundled_node
        except Exception:
            bundled_node = None
        if bundled_node is not None:
            try:
                candidate = bundled_node()
                if candidate.is_file():
                    return str(candidate)
            except Exception:
                pass
        for candidate in (
            Path(r"D:\漫剧剪辑工具\runtime\node\node.exe"),
            Path(r"D:\漫剧剪辑工具\release\漫剧自动任务中心_v52\_internal\runtime\node\node.exe"),
        ):
            if candidate.is_file():
                return str(candidate)
        system = shutil.which("node")
        if system:
            return system
        raise RuntimeError("未找到内置 Node.js，请确认绿色文件夹完整。")

    def _adapter(self, name: str) -> Path:
        path = self.adapter_dir / name
        if not path.is_file():
            # 打包后 mjs 位于 _MEIPASS/platform_adapter/
            bundle = Path(getattr(sys, "_MEIPASS", ""))
            if bundle.is_dir():
                path = bundle / "platform_adapter" / name
        if not path.is_file():
            raise RuntimeError(f"平台适配器缺失：{name}")
        return path

    def _state_file(self) -> Path:
        return self.data_root / "station_tasks.json"

    def _log_file(self, task: StationTask, stage: str) -> Path:
        log_dir = self.data_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / f"{_safe_filename(task.book_id)}_{stage}.log"

    # ---------- 状态持久化 ----------
    def _load_state(self) -> None:
        path = self._state_file()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        raw_tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(raw_tasks, dict):
            return
        for book_id, item in raw_tasks.items():
            task = StationTask.from_dict(item) if isinstance(item, dict) else None
            if task and task.book_id:
                self._tasks[book_id] = task
        queue = data.get("queue") if isinstance(data, dict) else None
        if isinstance(queue, list):
            self._queue = [str(x) for x in queue if str(x) in self._tasks]
        saved_prefix = data.get("alias_prefix") if isinstance(data, dict) else None
        if isinstance(saved_prefix, str):
            self.alias_prefix = saved_prefix.strip()
        saved_mode = data.get("alias_mode") if isinstance(data, dict) else None
        if isinstance(saved_mode, str) and saved_mode in ("prefix", "suffix"):
            self.alias_mode = saved_mode
        saved_fix_affix = data.get("fix_affix") if isinstance(data, dict) else None
        if isinstance(saved_fix_affix, bool):
            self.fix_affix = saved_fix_affix
        saved_download_only = data.get("download_only") if isinstance(data, dict) else None
        if isinstance(saved_download_only, bool):
            self.download_only = saved_download_only
        saved_alias_only = data.get("alias_only") if isinstance(data, dict) else None
        if isinstance(saved_alias_only, bool):
            self.alias_only = saved_alias_only
        saved_manual_mode = data.get("manual_mode") if isinstance(data, dict) else None
        if isinstance(saved_manual_mode, bool):
            self.manual_mode = saved_manual_mode
        for book_id in list(self._tasks):
            task = self._tasks[book_id]
            if task.status in (STATUS_DOWNLOADING, STATUS_GENERATING, STATUS_APPLYING):
                # 上次异常退出，回到排队可重跑（mjs 侧支持断点续传）
                task.status = STATUS_QUEUED
                task.detail = "上次运行中断，已恢复排队，可继续"
                if task.book_id not in self._queue:
                    self._queue.append(task.book_id)
        self._save_state()

    def _save_state(self) -> None:
        data = {
            "version": 1,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "queue": self._queue,
            "alias_prefix": self.alias_prefix,
            "alias_mode": self.alias_mode,
            "fix_affix": self.fix_affix,
            "download_only": self.download_only,
            "alias_only": self.alias_only,
            "manual_mode": self.manual_mode,
            "tasks": {book_id: task.to_dict() for book_id, task in self._tasks.items()},
        }
        path = self._state_file()
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    # ---------- 任务管理 ----------
    @property
    def tasks(self) -> list[StationTask]:
        ordered = list(self._tasks.values())
        ordered.sort(key=lambda t: t.created_at)
        return ordered

    def add_book_id(self, book_id: str) -> StationTask:
        book_id = str(book_id or "").strip()
        if not _is_book_id(book_id):
            raise ValueError("BookID 必须是 16~20 位纯数字")
        existing = self._tasks.get(book_id)
        if existing and existing.status not in (STATUS_DONE, STATUS_FAILED):
            raise ValueError(f"BookID {book_id} 已在任务列表中")
        task = StationTask(book_id=book_id, input_type="book_id", input_value=book_id)
        if existing:
            # 重新入队：清空旧进度，保留标题便于展示
            task.title = existing.title
            task.resolved_book_id = existing.resolved_book_id or book_id
        self._tasks[book_id] = task
        if self.manual_mode:
            task.status = STATUS_PENDING_MANUAL
            task.detail = "等待手动输入别名"
            self._save_state()
            self._emit(book_id, task.status, "已添加，等待手动别名")
            return task
        if book_id not in self._queue:
            self._queue.append(book_id)
        self._save_state()
        self._emit(book_id, task.status, "已加入队列")
        return task

    def add_task(self, identifier: str) -> StationTask:
        """统一入口：自动判断输入是 BookID（16~20位纯数字）还是剧名。"""
        identifier = str(identifier or "").strip()
        if not identifier:
            raise ValueError("请输入 BookID 或剧名")
        if _is_book_id(identifier):
            return self.add_book_id(identifier)
        # 剧名模式
        if len(identifier) > 100:
            raise ValueError("剧名过长（最多 100 字）")
        task_key = f"title:{identifier}"
        existing = self._tasks.get(task_key)
        if existing and existing.status not in (STATUS_DONE, STATUS_FAILED):
            raise ValueError(f"剧名「{identifier}」已在任务列表中")
        task = StationTask(book_id=task_key, input_type="title", input_value=identifier)
        if existing:
            task.title = existing.title
            task.resolved_book_id = existing.resolved_book_id
        self._tasks[task_key] = task
        if self.manual_mode:
            task.status = STATUS_PENDING_MANUAL
            task.detail = "等待手动输入别名"
            self._save_state()
            self._emit(task_key, task.status, "已添加，等待手动别名")
            return task
        if task_key not in self._queue:
            self._queue.append(task_key)
        self._save_state()
        self._emit(task_key, task.status, "已加入队列")
        return task

    def add_tasks_from_file(self, file_path: str | Path) -> dict:
        """从 txt 文件批量导入任务，每行一个 BookID 或剧名，自动识别类型。
        支持每行附带别名（用分号/逗号/制表符分隔），格式：BookID;别名
        - 有别名：手动申请（跳过AI生成，直接用此别名申请三端）
        - 无别名：自动申请（AI生成候选别名）
        返回 {"added": [...], "skipped": [...], "failed": [...]}。"""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")
        added: list[str] = []
        skipped: list[str] = []
        failed: list[tuple[str, str]] = []
        with open(path, "r", encoding="utf-8-sig") as f:
            for line_no, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue  # 空行和注释行跳过
                # 解析可选别名：用英文分号/中文分号/制表符分隔（不支持逗号，因为剧名里可能包含逗号）
                parts = re.split(r"[;；\t]", line, maxsplit=1)
                identifier = parts[0].strip()
                alias_part = parts[1].strip() if len(parts) > 1 else ""
                # 多个别名用中文逗号/英文逗号分隔，作为候选依次尝试
                alias_names = [a.strip() for a in re.split(r"[,，]", alias_part) if a.strip()] if alias_part else []
                try:
                    task = self.add_task(identifier)
                    if alias_names:
                        # 有别名：标记为手动别名模式
                        task.manual_alias = True
                        task.manual_alias_names = alias_names
                        task.manual_alias_name = alias_names[0]  # 兼容单个别名
                        task.detail = f"手动别名：{'、'.join(alias_names)}"
                        self._save_state()
                    added.append(task.input_value or task.book_id)
                except ValueError as error:
                    # 已存在的任务算跳过，其他错误算失败
                    msg = str(error)
                    if "已在任务列表中" in msg:
                        skipped.append(identifier)
                    else:
                        failed.append((identifier, msg))
        return {"added": added, "skipped": skipped, "failed": failed}

    def remove_task(self, book_id: str) -> None:
        if book_id in self._queue:
            self._queue.remove(book_id)
        if self._running and self._running.book_id == book_id:
            return  # 正在运行，不允许直接删除
        self._tasks.pop(book_id, None)
        self._save_state()

    def retry_task(self, book_id: str) -> None:
        task = self._tasks.get(book_id)
        if not task or task.status in (STATUS_DOWNLOADING, STATUS_GENERATING, STATUS_APPLYING):
            return
        task.status = STATUS_QUEUED
        task.error = ""
        task.detail = "已重新加入队列"
        task.updated_at = time.time()
        if book_id not in self._queue:
            self._queue.append(book_id)
        self._save_state()

    def apply_manual_alias(self, identifier: str, aliases_text: str) -> StationTask:
        """手动提供别名，直接去三个平台申请（跳过 AI 生成候选）。
        identifier: BookID 或剧名，自动识别。
        aliases_text: 一个或多个别名，用逗号/空格/换行分隔。
        要求：剧目信息.json 已存在（需先下载过）。
        """
        import json as _json
        # 1. 解析别名
        raw_aliases = [a.strip() for a in re.split(r"[,，\s\n]+", str(aliases_text or "")) if a.strip()]
        if not raw_aliases:
            raise ValueError("请输入至少一个别名")
        # 2. 校验别名格式（手动别名不被前缀后缀限制，只要求恰好4个中文字）
        invalid = [a for a in raw_aliases if not re.fullmatch(r"[\u4e00-\u9fff]{4}", a)]
        if invalid:
            raise ValueError(f"以下别名必须恰好是4个中文汉字：{'、'.join(invalid)}")
        # 从第一个别名提取固定字（前缀模式取前两字，后缀模式取后两字）
        affix = raw_aliases[0][:2] if self.alias_mode == "prefix" else raw_aliases[0][2:]
        # 校验所有别名固定字必须相同（mjs端要求同一批候选必须同一个前缀/后缀）
        diff_affix = [a for a in raw_aliases if (a[:2] if self.alias_mode == "prefix" else a[2:]) != affix]
        if diff_affix:
            mode_label = "前两字" if self.alias_mode == "prefix" else "后两字"
            raise ValueError(f"同一批别名的{mode_label}必须相同（当前为「{affix}」），不同请分批申请：{'、'.join(diff_affix)}")
        validated = raw_aliases
        # 3. 确保任务存在
        task = self.add_task(identifier)
        # 4. 确保有剧目信息
        info = self._locate_info(task, self.data_root / "选剧文件夹" / "原剧视频")
        if not info:
            raise ValueError(f"未找到剧目信息，请先下载（或勾选「只申请别名」自动获取）：{task.input_value}")
        title = str(info.get("title", task.title or ""))
        book_id = str(info.get("book_id", task.resolved_book_id or task.book_id or ""))
        task_dir = str(Path(info["info_file"]).parent.parent)
        task.info_file = str(info["info_file"])
        task.title = title
        task.task_dir = task_dir
        # 5. 写入三端别名任务.json
        info_dir = Path(info["info_file"]).parent
        task_file = info_dir / "三端别名任务.json"
        rows = [{"order": i + 1, "alias": alias, "status": "queued", "platforms": {}}
                for i, alias in enumerate(validated)]
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        payload = {
            "version": 2,
            "task_id": f"station_{book_id}",
            "batch_id": f"station_{book_id}",
            "title": title,
            "book_id": book_id,
            "alias_prefix": affix,
            "alias_mode": self.alias_mode,
            "status": "queued",
            "current_index": 0,
            "candidate_rows": rows,
            "candidates": validated,
            "history": [],
            "platforms": {},
            "approved_alias": "",
            "created_at": now,
            "updated_at": now,
        }
        task_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = task_file.with_suffix(task_file.suffix + ".tmp")
        tmp.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, task_file)
        # 6. 设置任务为手动别名模式，加入队列
        task.manual_alias = True
        task.task_file = str(task_file)
        task.status = STATUS_QUEUED
        task.error = ""
        task.detail = f"手动别名已提交：{'、'.join(validated)}"
        task.updated_at = time.time()
        if task.book_id not in self._queue:
            self._queue.append(task.book_id)
        self._save_state()
        self._emit(task.book_id, task.status, task.detail)
        return task

    def apply_manual_alias_to_tasks(self, task_keys: list[str], aliases_text: str) -> list[StationTask]:
        """批量手动别名：对选中的多个任务按顺序分配别名，跳过AI生成直接申请三端。
        task_keys: 选中的任务 book_id 列表
        aliases_text: 一个或多个别名，用逗号/空格/换行分隔
        分配规则：第一个别名给第一个任务，第二个给第二个，以此类推；
                  别名数量少于任务数量时，最后一个别名应用到剩余所有任务。
        """
        import json as _json
        # 1. 解析别名
        raw_aliases = [a.strip() for a in re.split(r"[,，\s\n]+", str(aliases_text or "")) if a.strip()]
        if not raw_aliases:
            raise ValueError("请输入至少一个别名")
        if not task_keys:
            raise ValueError("请先选中任务")
        # 2. 校验别名格式（手动别名不被前缀后缀限制，只要求恰好4个中文字）
        invalid = [a for a in raw_aliases if not re.fullmatch(r"[\u4e00-\u9fff]{4}", a)]
        if invalid:
            raise ValueError(f"以下别名必须恰好是4个中文汉字：{'、'.join(invalid)}")
        validated = raw_aliases
        # 3. 逐个任务分配别名
        results = []
        for idx, task_key in enumerate(task_keys):
            task = self._tasks.get(task_key)
            if not task:
                continue
            # 分配别名：索引超限时用最后一个
            alias = validated[min(idx, len(validated) - 1)]
            # 从别名自动提取固定字（前缀模式取前两字，后缀模式取后两字）
            affix = alias[:2] if self.alias_mode == "prefix" else alias[2:]
            # 确保有剧目信息
            info = self._locate_info(task, self.data_root / "选剧文件夹" / "原剧视频")
            if not info:
                task.status = STATUS_FAILED
                task.error = f"未找到剧目信息，无法手动申请别名：{task.input_value}"
                task.detail = task.error
                task.updated_at = time.time()
                self._save_state()
                self._emit(task.book_id, task.status, task.detail)
                results.append(task)
                continue
            title = str(info.get("title", task.title or ""))
            book_id = str(info.get("book_id", task.resolved_book_id or task.book_id or ""))
            task.info_file = str(info["info_file"])
            task.title = title
            task.task_dir = str(Path(info["info_file"]).parent.parent)
            # 写入三端别名任务.json
            info_dir = Path(info["info_file"]).parent
            task_file = info_dir / "三端别名任务.json"
            rows = [{"order": 1, "alias": alias, "status": "queued", "platforms": {}}]
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            payload = {
                "version": 2,
                "task_id": f"station_{book_id}",
                "batch_id": f"station_{book_id}",
                "title": title,
                "book_id": book_id,
                "alias_prefix": affix,
                "alias_mode": self.alias_mode,
                "status": "queued",
                "current_index": 0,
                "candidate_rows": rows,
                "candidates": [alias],
                "history": [],
                "platforms": {},
                "approved_alias": "",
                "created_at": now,
                "updated_at": now,
            }
            task_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = task_file.with_suffix(task_file.suffix + ".tmp")
            tmp.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, task_file)
            # 设置任务为手动别名模式，加入队列
            task.manual_alias = True
            task.task_file = str(task_file)
            task.status = STATUS_QUEUED
            task.error = ""
            task.detail = f"手动别名已提交：{alias}"
            task.updated_at = time.time()
            if task.book_id not in self._queue:
                self._queue.append(task.book_id)
            self._save_state()
            self._emit(task.book_id, task.status, task.detail)
            results.append(task)
        return results
        self._emit(book_id, task.status, task.detail)

    # ---------- 回调 ----------
    def set_progress_callback(self, callback) -> None:
        self._on_progress = callback

    def set_alias_prefix(self, prefix: str) -> None:
        """设置用户手动指定的别名前缀/后缀（两字），空字符串则不限制（需 fix_affix=False 才生效）。"""
        self.alias_prefix = str(prefix or "").strip()
        self._save_state()

    def set_alias_mode(self, mode: str) -> None:
        """设置别名格式：prefix=前缀+两字（默认），suffix=两字+后缀。"""
        if mode not in ("prefix", "suffix"):
            raise ValueError(f"别名模式必须是 prefix 或 suffix， got: {mode}")
        self.alias_mode = mode
        self._save_state()

    def set_fix_affix(self, enabled: bool) -> None:
        """设置固定前缀/后缀开关：True=必须在别名前缀框填写固定字，False=AI自由生成4字别名。"""
        self.fix_affix = bool(enabled)
        self._save_state()

    def set_download_only(self, enabled: bool) -> None:
        """设置只下载模式：True=只下载原剧+封面，不生成候选/申请别名。"""
        self.download_only = bool(enabled)
        if self.download_only:
            self.alias_only = False
        self._save_state()

    def set_alias_only(self, enabled: bool) -> None:
        """设置只申请别名模式：True=跳过下载，直接生成候选+申请别名（需已有剧目信息）。"""
        self.alias_only = bool(enabled)
        if self.alias_only:
            self.download_only = False
        self._save_state()

    def set_manual_mode(self, enabled: bool) -> None:
        """设置手动别名模式：True=新任务不自动执行，等用户手动输入别名后申请。"""
        self.manual_mode = bool(enabled)
        self._save_state()

    def _emit(self, book_id: str, status: str, detail: str) -> None:
        if self._on_progress:
            try:
                self._on_progress(book_id, status, detail)
            except Exception:
                pass

    # ---------- 调度 ----------
    def tick(self) -> None:
        """调度入口：每次调用最多启动一个任务，在后台线程执行，不阻塞 UI。"""
        if self._running is not None:
            return
        if self._close_requested:
            return
        with self._lock:
            while self._queue:
                book_id = self._queue[0]
                task = self._tasks.get(book_id)
                if task is None:
                    self._queue.pop(0)
                    continue
                if task.status == STATUS_DONE:
                    self._queue.pop(0)
                    continue
                self._running = task
                self._emit(book_id, task.status, "任务开始")
                thread = threading.Thread(target=self._run_task_async, args=(task,), daemon=True)
                thread.start()
                return

    def _run_task_async(self, task: StationTask) -> None:
        """后台线程执行任务，结束后清理运行状态。"""
        try:
            self._run_task(task)
        finally:
            with self._lock:
                self._running = None
                if task.book_id in self._queue:
                    self._queue.remove(task.book_id)
                self._save_state()

    # ---------- 三阶段执行 ----------
    def _run_task(self, task: StationTask) -> None:
        if not self._ensure_model_service():
            task.status = STATUS_FAILED
            task.error = "本地 Ollama 无法启动，请检查绿色文件夹是否完整"
            task.detail = task.error
            self._emit(task.book_id, task.status, task.detail)
            return
        # 手动别名申请：跳过下载和生成，直接进入三端申请
        if task.manual_alias and task.task_file:
            self._apply(task)
            return
        try:
            if self.alias_only:
                # 只申请别名模式：优先用已有剧目信息，没有则用 info-only 快速获取（不下载视频/封面）
                info = self._locate_info(task, self.data_root / "选剧文件夹" / "原剧视频")
                if not info:
                    self._emit(task.book_id, STATUS_DOWNLOADING, "未找到剧目信息，正在快速获取（不下载视频/封面）")
                    self._download(task, info_only=True)
                    if task.status != STATUS_DOWNLOADING:
                        return
                    info = self._locate_info(task, self.data_root / "选剧文件夹" / "原剧视频")
                    if not info:
                        task.status = STATUS_FAILED
                        task.error = "只申请别名模式：快速获取剧目信息失败，请取消该模式后完整运行一次"
                        task.detail = task.error
                        task.updated_at = time.time()
                        self._save_state()
                        self._emit(task.book_id, task.status, task.detail)
                        return
                task.status = STATUS_DOWNLOADING
                task.info_file = str(info["info_file"])
                task.title = str(info.get("title", task.title or ""))
                task.task_dir = str(Path(info["info_file"]).parent.parent)
                self._emit(task.book_id, task.status, f"使用剧目信息：{task.title}")
            else:
                self._download(task)
                if task.status != STATUS_DOWNLOADING:
                    return
            if self.download_only:
                # 只下载模式：下载完成即结束，不生成候选/申请别名
                task.status = STATUS_DONE
                task.detail = "下载完成（仅下载模式）"
                task.updated_at = time.time()
                self._save_state()
                self._emit(task.book_id, task.status, task.detail)
                return
            self._generate(task)
            if task.status != STATUS_GENERATING:
                return
            self._apply(task)
        except Exception as error:  # noqa: BLE001
            task.status = STATUS_FAILED
            task.error = str(error)
            task.detail = f"任务异常：{error}"
            task.updated_at = time.time()
            self._emit(task.book_id, task.status, task.detail)

    def _download(self, task: StationTask, info_only: bool = False) -> None:
        """阶段一：按 BookID 或剧名下载原剧视频 + 封面，落剧目信息.json。
        info_only=True 时只获取剧目信息（不下载视频/封面），用于只申请别名模式。"""
        task.status = STATUS_DOWNLOADING
        if info_only:
            task.detail = f"正在获取剧目信息（不下载视频/封面）：{task.input_value}"
        elif task.input_type == "title":
            task.detail = f"正在按剧名「{task.input_value}」搜索并下载原剧与封面"
        else:
            task.detail = "正在按 BookID 搜索并下载原剧与封面"
        task.updated_at = time.time()
        self._save_state()
        self._emit(task.book_id, task.status, task.detail)

        download_dir = self.data_root / "选剧文件夹" / "原剧视频"
        cover_dir = self.data_root / "封面-原图"
        adapter = self._adapter("task-platform-download.mjs")
        log_path = self._log_file(task, "download")
        extra_args = ["--info-only"] if info_only else []
        if task.input_type == "title":
            cmd = [
                self.node_command, "--experimental-websocket",
                str(adapter),
                "--title", task.input_value,
                "--content-type", "manju",
                "--output-dir", str(download_dir),
                "--cover-output-dir", str(cover_dir),
                *extra_args,
            ]
        else:
            cmd = [
                self.node_command, "--experimental-websocket",
                str(adapter),
                "--book-id", task.book_id,
                "--content-type", "manju",
                "--output-dir", str(download_dir),
                "--cover-output-dir", str(cover_dir),
                *extra_args,
            ]
        env = {
            **os.environ,
            "MANJU_TOOL_ROOT": str(self.tool_root),
            "MANJU_SHARED_ROOT": str(self.data_root),
        }
        return_code, output = self._run_process(cmd, env, log_path)

        info = self._locate_info(task, download_dir)
        if return_code != 0 or info is None:
            task.status = STATUS_FAILED
            task.error = self._read_tail(log_path, 15)
            task.detail = f"下载未完成（退出码 {return_code}）：{task.error or '未找到剧目信息'}"
            task.updated_at = time.time()
            self._emit(task.book_id, task.status, task.detail)
            return

        task.title = str(info.get("title", "") or "").strip()
        if task.input_type == "title":
            task.resolved_book_id = str(info.get("book_id", "") or "").strip()
        task.info_file = str(info["info_file"])
        task.task_dir = str(Path(info["info_file"]).parent.parent)
        task.cover_file = str(info.get("cover_file", "") or "")
        task.detail = f"原剧与封面下载完成：{task.title}（{len(info.get('chapters', []) or [])} 集）"
        task.updated_at = time.time()
        self._save_state()
        self._emit(task.book_id, task.status, task.detail)

    def _locate_info(self, task: StationTask, download_dir: Path) -> dict | None:
        """扫描下载目录，定位任务匹配的剧目信息.json。

        BookID 模式按 info.book_id 精确匹配；剧名模式按剧名归一化后匹配。
        """
        download_dir = Path(download_dir)
        if not download_dir.is_dir():
            return None
        target_title_norm = _normalize_title(task.input_value) if task.input_type == "title" else ""
        for folder in sorted(download_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True):
            if not folder.is_dir():
                continue
            info_file = folder / "剧目信息" / "剧目信息.json"
            if not info_file.is_file():
                continue
            try:
                info = json.loads(info_file.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if task.input_type == "title":
                if _normalize_title(info.get("title", "")) == target_title_norm:
                    info["info_file"] = str(info_file)
                    return info
            else:
                if str(info.get("book_id", "")) == task.book_id:
                    info["info_file"] = str(info_file)
                    return info
        return None

    def _generate(self, task: StationTask) -> None:
        """阶段二：用本地 Ollama 生成候选别名，写三端别名任务.json。
        如果 task.manual_alias 且有 manual_alias_name，跳过AI生成，直接用手动别名。"""
        task.status = STATUS_GENERATING
        task.detail = "正在生成本地候选别名"
        task.updated_at = time.time()
        self._save_state()
        self._emit(task.book_id, task.status, task.detail)

        info = self._read_json(Path(task.info_file))
        title = str(info.get("title", "") or "").strip() or task.title
        intro = str(info.get("description", "") or "").strip()
        book_id = str(info.get("book_id", "") or task.book_id).strip()

        # 手动别名：跳过AI生成，直接用手动别名写入三端别名任务.json（支持多个别名作为候选）
        if task.manual_alias and (task.manual_alias_names or task.manual_alias_name):
            aliases = task.manual_alias_names if task.manual_alias_names else [task.manual_alias_name.strip()]
            aliases = [a.strip() for a in aliases if a.strip()]
            # 手动别名不被前缀后缀限制，直接使用；但必须恰好4个中文字（mjs端要求）
            invalid = [a for a in aliases if not re.fullmatch(r"[\u4e00-\u9fff]{4}", a)]
            if invalid:
                raise ValueError(f"以下手动别名必须恰好是4个中文汉字：{'、'.join(invalid)}")
            # 从第一个别名自动提取固定字（前缀模式取前两字，后缀模式取后两字）
            affix = aliases[0][:2] if self.alias_mode == "prefix" else aliases[0][2:]
            # 校验所有别名固定字必须相同（mjs端要求同一批候选必须同一个前缀/后缀）
            diff_affix = [a for a in aliases if (a[:2] if self.alias_mode == "prefix" else a[2:]) != affix]
            if diff_affix:
                mode_label = "前两字" if self.alias_mode == "prefix" else "后两字"
                raise ValueError(f"同一批手动别名的{mode_label}必须相同（当前为「{affix}」），不同请分批申请：{'、'.join(diff_affix)}")
            # 去重
            aliases = list(dict.fromkeys(aliases))
            # 写入三端别名任务.json
            import json as _json
            info_dir = Path(task.info_file).parent
            task_file = info_dir / "三端别名任务.json"
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            rows = [{"order": i + 1, "alias": a, "status": "queued", "platforms": {}} for i, a in enumerate(aliases)]
            payload = {
                "version": 2,
                "task_id": f"station_{book_id}",
                "batch_id": f"station_{book_id}",
                "title": title,
                "book_id": book_id,
                "alias_prefix": affix,
                "alias_mode": self.alias_mode,
                "status": "queued",
                "current_index": 0,
                "candidate_rows": rows,
                "candidates": aliases,
                "history": [],
                "platforms": {},
                "approved_alias": "",
                "created_at": now,
                "updated_at": now,
            }
            task_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = task_file.with_suffix(task_file.suffix + ".tmp")
            tmp.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, task_file)
            task.task_file = str(task_file)
            task.detail = f"手动别名已写入：{'、'.join(aliases)}（固定字自动识别为「{affix}」，{self.alias_mode}模式）"
            task.updated_at = time.time()
            self._save_state()
            self._emit(task.book_id, task.status, task.detail)
            return

        try:
            from manju_editor.material_workflow import generate_alias_candidates, write_alias_task
        except ImportError as error:
            raise RuntimeError(f"独立软件缺少候选生成模块：{error}") from error

        # 根据 fix_affix 决定是否限制前缀/后缀：选上且有值=用指定固定字，否则=AI自由生成4个字
        affix = self.alias_prefix if (self.fix_affix and self.alias_prefix) else ""
        candidates = generate_alias_candidates(
            title, intro, count=3, prefix=affix,
            mode=self.alias_mode,
            excluded={str(task.approved_alias or "").strip()} if task.approved_alias else None,
        )
        project = SimpleNamespace(
            title=title,
            platform_title=title,
            book_id=book_id,
            task_id=f"station_{book_id}",
            workflow_batch_id=f"station_{book_id}",
        )
        series_root = self.data_root / "选剧文件夹" / "原剧视频"
        task_file = write_alias_task(project, candidates, preferred_series_root=series_root, prefix=affix, mode=self.alias_mode)
        task.task_file = str(task_file)
        task.detail = f"已生成候选别名：{'、'.join(candidates)}"
        task.updated_at = time.time()
        self._save_state()
        self._emit(task.book_id, task.status, task.detail)

    def _apply(self, task: StationTask) -> None:
        """阶段三：申请三端别名并轮询审核结果。"""
        task.status = STATUS_APPLYING
        task.detail = "正在申请三端关键词别名"
        task.updated_at = time.time()
        self._save_state()
        self._emit(task.book_id, task.status, task.detail)

        adapter = self._adapter("run-alias-task.mjs")
        log_path = self._log_file(task, "alias")
        cmd = [self.node_command, "--experimental-websocket", str(adapter), str(task.task_file)]
        env = {
            **os.environ,
            "MANJU_TOOL_ROOT": str(self.tool_root),
            "MANJU_SHARED_ROOT": str(self.data_root),
        }
        return_code, _output = self._run_process(cmd, env, log_path)

        state = self._read_json(Path(task.task_file)) if task.task_file else {}
        approved = str(state.get("approved_alias", "") or "").strip()
        status = str(state.get("status", "") or "")

        if return_code == 0 and approved:
            task.status = STATUS_DONE
            task.approved_alias = approved
            task.detail = f"三端全部审核通过：{approved}"
        elif return_code in (_ALIAS_EXIT_MANUAL, _ALIAS_EXIT_RETRY):
            task.status = STATUS_WAITING_MANUAL
            reason = "检测到平台滑块/安全验证" if return_code == _ALIAS_EXIT_MANUAL else "平台控件故障"
            task.error = reason
            task.detail = f"{reason}，已保留进度，请到平台处理后手动重试该任务"
        elif return_code == _ALIAS_EXIT_NEED_MORE:
            task.status = STATUS_WAITING_MANUAL
            task.error = "候选别名均未通过三端审核"
            task.detail = "现有候选均未三端通过，需要重新生成候选后重试"
        else:
            task.status = STATUS_FAILED
            task.error = self._read_tail(log_path, 12)
            task.detail = f"三端申请未完成（退出码 {return_code}）：{task.error or '未知错误'}"
        task.updated_at = time.time()
        self._save_state()
        self._emit(task.book_id, task.status, task.detail)

    # ---------- 辅助 ----------
    def _ensure_model_service(self) -> bool:
        try:
            urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1).close()
            return True
        except Exception:
            pass
        try:
            from manju_editor.runtime import bundled_ollama
            executable = bundled_ollama()
        except Exception:
            executable = Path(self.tool_root) / "runtime" / "ollama" / "ollama.exe"
        if not Path(executable).is_file():
            system = shutil.which("ollama")
            executable = Path(system) if system else executable
        if not Path(executable).is_file():
            return False
        environment = {
            **os.environ,
            "OLLAMA_MODELS": str(Path(self.tool_root) / "models" / "ollama"),
        }
        try:
            subprocess.Popen(
                [str(executable), "serve"], cwd=str(self.tool_root), env=environment,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # 等待服务就绪
            for _ in range(40):
                time.sleep(0.5)
                try:
                    urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1).close()
                    return True
                except Exception:
                    continue
            return False
        except OSError:
            return False

    @staticmethod
    def _run_process(cmd: list[str], env: dict, log_path: Path) -> tuple[int, str]:
        # 找到 mjs 脚本路径作为 cwd（cmd 可能含 --experimental-websocket 等 flag）
        mjs_path = next((arg for arg in cmd if arg.endswith(".mjs")), cmd[1] if len(cmd) > 1 else ".")
        cwd = str(Path(mjs_path).parent)
        with log_path.open("a", encoding="utf-8") as handle:
            process = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdout=handle, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return_code = process.wait()
        try:
            output = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            output = ""
        return return_code, output

    @staticmethod
    def _read_json(path: str | Path | None) -> dict:
        if not path:
            return {}
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _read_tail(path: str | Path, lines: int = 15) -> str:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        tail = "\n".join(text.strip().splitlines()[-lines:])
        return tail[-1200:]


class StationRuntime:
    """frozen 环境下解析绿色文件夹各组件路径。"""

    def __init__(self, tool_root: str | Path):
        self.tool_root = Path(tool_root).resolve()

    def node(self) -> str:
        return str(self.tool_root / "runtime" / "node" / "node.exe")

    def ollama(self) -> str:
        return str(self.tool_root / "runtime" / "ollama" / "ollama.exe")

    def models(self) -> str:
        return str(self.tool_root / "models" / "ollama")
