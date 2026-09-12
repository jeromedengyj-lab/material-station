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

import atexit
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
STATUS_SUBMITTED = "submitted"     # 已提交三端申请，待统一审核（先批量提交，最后统一审核）
STATUS_REVIEWING = "reviewing"     # 统一审核中（轮询三端审核结果）
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
    """剧名归一化：NFKC + 小写 + 去空白 + 去全半角标点。

    中英文逗号/冒号/括号/书名号等全半角标点视为等价（搜索结果可能用
    中文标点，用户输入可能用英文标点），大小写不敏感。与 mjs 端
    normalizeTitle 对齐，用于剧名定位与任务查重。
    """
    import re as _re
    import unicodedata
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return _re.sub(r"[\s,，.。!！?？:：;；、《》「」『』（）()【】\[\]~～\-—_\"'“”‘’…·]+", "", text)


def _safe_filename(value: str) -> str:
    """替换 Windows 文件名非法字符，用于日志文件名。"""
    import re
    return re.sub(r'[\\/:*?"<>|]', "_", str(value or ""))


# ---------- 子进程与角色锁的退出清理 ----------
# 背景：程序退出时若 mjs/node 子进程仍在运行（wait 阻塞中），daemon 线程被
# 强杀后 node 变孤儿进程继续持有浏览器角色锁（alias.lock/download.lock）；
# 锁内旧 PID 被系统复用后，下次启动所有任务被误判"已有任务在运行"（退出码28）。
# 因此：登记所有子进程，程序退出时统一终止；同时删除角色锁，保证下次启动可运行。
_ACTIVE_PROCS: list = []
_ACTIVE_PROCS_GUARD = threading.Lock()


def _register_active_proc(process: subprocess.Popen) -> None:
    with _ACTIVE_PROCS_GUARD:
        _ACTIVE_PROCS.append(process)


def _unregister_active_proc(process: subprocess.Popen) -> None:
    with _ACTIVE_PROCS_GUARD:
        try:
            _ACTIVE_PROCS.remove(process)
        except ValueError:
            pass


def terminate_active_procs(grace: float = 3.0) -> int:
    """终止所有登记中的子进程（先 terminate 优雅退出，超时再 kill）。

    返回本次实际终止的进程数；无活动进程时返回 0。
    """
    with _ACTIVE_PROCS_GUARD:
        procs = list(_ACTIVE_PROCS)
    killed = 0
    for proc in procs:
        if proc.poll() is None:
            killed += 1
            try:
                proc.terminate()
            except OSError:
                pass
    if killed:
        deadline = time.time() + grace
        while time.time() < deadline:
            with _ACTIVE_PROCS_GUARD:
                alive = [p for p in _ACTIVE_PROCS if p.poll() is None]
            if not alive:
                break
            time.sleep(0.05)
        for proc in procs:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
    return killed


def platform_count(tool_root: str | Path) -> int:
    """读取 data/platforms.json 的平台数量（与 mjs loadPlatforms 同源，剥注释）。

    写几个平台就用几个（顺序即申请顺序）；文件缺失/非法时回退 3。
    """
    try:
        text = (Path(tool_root) / "data" / "platforms.json").read_text(encoding="utf-8")
        cleaned = re.sub(r"/\*[\s\S]*?\*/", "", text)
        cleaned = re.sub(r"(^|[^:])\/\/.*$", r"\1", cleaned, flags=re.M)
        data = json.loads(cleaned)
        if isinstance(data, list) and data:
            return len(data)
    except Exception:  # noqa: BLE001
        pass
    return 3


def clean_role_locks(tool_root: str | Path) -> int:
    """删除 runtime/platform_adapter 下的角色锁（alias.lock/download.lock）。

    只删这两个锁，不动其他文件；目录缺失时安全返回 0。
    """
    lock_dir = Path(tool_root) / "runtime" / "platform_adapter"
    removed = 0
    if lock_dir.is_dir():
        for name in ("alias.lock", "download.lock"):
            target = lock_dir / name
            try:
                if target.is_file():
                    target.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


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
    content_type: str = ""          # 内容类型：manju=漫剧 / wangwen=网文 / duanju=短剧；空=不限制（mjs 默认漫剧）
    expected_episodes: int = 0      # 期望集数（同名同标签时精确区分）；0=不限制
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    auto_retry_count: int = 0  # 候选全部未通过后自动换新候选重试的轮数（上限3轮，防死循环）

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
        self.content_type = ""          # 全局默认内容类型：空=不限制（mjs 兜底漫剧），UI 可选 网文/漫剧/短剧
        self.expected_episodes = 0      # 全局默认期望集数：0=不限制
        self.post_type = self._load_post_type()  # 发文类型（平台弹窗素材类型），默认解说混剪，可选并记住
        self._tasks: dict[str, StationTask] = {}
        self._queue: list[str] = []
        self._review_queue: list[str] = []  # 已提交三端、待统一审核的任务
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
        self.table_mode = "single"  # "single"=始终同一表格（累积追加去重）；"per_batch"=每批一个新表格
        self.table_path = ""        # single 模式固定的表格文件路径（首次导出时选择并记住）
        self._ensure_dirs()
        self._load_state()
        # 程序退出时：终止登记中的子进程（防 node 孤儿持锁）+ 删除角色锁
        tool_root = self.tool_root
        atexit.register(lambda: (terminate_active_procs(), clean_role_locks(tool_root)))

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
        bundled_node = None  # 独立版本：不依赖主程序 runtime，直接走下方路径查找
        if bundled_node is not None:
            try:
                candidate = bundled_node()
                if candidate.is_file():
                    return str(candidate)
            except Exception:
                pass
        # 打包后 node 位于 _MEIPASS/runtime/node/node.exe（onedir 即 _internal/runtime/node）
        bundle = Path(getattr(sys, "_MEIPASS", ""))
        if bundle.is_dir() and (bundle / "runtime" / "node" / "node.exe").is_file():
            return str(bundle / "runtime" / "node" / "node.exe")
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
        review_queue = data.get("review_queue") if isinstance(data, dict) else None
        if isinstance(review_queue, list):
            self._review_queue = [str(x) for x in review_queue if str(x) in self._tasks]
        # 兼容兜底：任何处于"已提交待审核"状态的任务都应回到统一审核队列
        for book_id, task in self._tasks.items():
            if task.status == STATUS_SUBMITTED and book_id not in self._review_queue:
                self._review_queue.append(book_id)
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
        saved_content_type = data.get("content_type") if isinstance(data, dict) else None
        if isinstance(saved_content_type, str) and saved_content_type in ("manju", "wangwen", "duanju", ""):
            self.content_type = saved_content_type
        saved_table_mode = data.get("table_mode") if isinstance(data, dict) else None
        if isinstance(saved_table_mode, str) and saved_table_mode in ("single", "per_batch"):
            self.table_mode = saved_table_mode
        saved_table_path = data.get("table_path") if isinstance(data, dict) else None
        if isinstance(saved_table_path, str):
            self.table_path = saved_table_path.strip()
        saved_episodes = data.get("expected_episodes") if isinstance(data, dict) else None
        if isinstance(saved_episodes, int) and saved_episodes >= 0:
            self.expected_episodes = saved_episodes
        for book_id in list(self._tasks):
            task = self._tasks[book_id]
            if task.status in (STATUS_DOWNLOADING, STATUS_GENERATING, STATUS_APPLYING):
                # 上次异常退出，回到排队可重跑（mjs 侧支持断点续传）
                task.status = STATUS_QUEUED
                task.detail = "上次运行中断，已恢复排队，可继续"
                if task.book_id not in self._queue:
                    self._queue.append(task.book_id)
        self._save_state()

    def save_state(self) -> None:
        """立即持久化当前配置与任务状态（UI 变更时调用）。"""
        self._save_state()

    def _save_state(self) -> None:
        data = {
            "version": 1,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "queue": self._queue,
            "review_queue": self._review_queue,
            "alias_prefix": self.alias_prefix,
            "alias_mode": self.alias_mode,
            "fix_affix": self.fix_affix,
            "download_only": self.download_only,
            "alias_only": self.alias_only,
            "manual_mode": self.manual_mode,
            "content_type": self.content_type,
            "expected_episodes": self.expected_episodes,
            "table_mode": self.table_mode,
            "table_path": self.table_path,
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

    def add_book_id(self, book_id: str, content_type: str | None = None,
                    expected_episodes: int | None = None) -> StationTask:
        """添加 BookID 任务（直达定位不依赖类型/集数；传入时仅作记录）。"""
        book_id = str(book_id or "").strip()
        if not _is_book_id(book_id):
            raise ValueError("BookID 必须是 16~20 位纯数字")
        existing = self._tasks.get(book_id)
        if existing and existing.status not in (STATUS_DONE, STATUS_FAILED):
            raise ValueError(f"BookID {book_id} 已在任务列表中")
        task = StationTask(book_id=book_id, input_type="book_id", input_value=book_id)
        task.content_type = str(content_type or "").strip()
        task.expected_episodes = max(0, int(expected_episodes or 0))
        if existing:
            # 重新入队：清空旧进度，保留标题便于展示
            task.title = existing.title
            task.resolved_book_id = existing.resolved_book_id or book_id
            task.content_type = task.content_type or existing.content_type
            task.expected_episodes = task.expected_episodes or existing.expected_episodes
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

    def add_task(self, identifier: str, alias_key: str = "", content_type: str | None = None,
                 expected_episodes: int | None = None) -> StationTask:
        """统一入口：自动判断输入是 BookID（16~20位纯数字）还是剧名。
        alias_key：剧名模式下，每行一个别名时用 剧名:别名 作为独立任务key；
                   同一行多个别名时不传 alias_key，用 剧名 作为key，多个别名作为候选。
        content_type/expected_episodes：任务级精确定位参数（识图/UI 显式传入），
                   不传=None = 该任务不限制（标签与集数只是同名难区分时的辅助，不写死全局）。"""
        identifier = str(identifier or "").strip()
        if not identifier:
            raise ValueError("请输入 BookID 或剧名")
        if _is_book_id(identifier):
            return self.add_book_id(identifier, content_type=content_type, expected_episodes=expected_episodes)
        # 剧名模式
        if len(identifier) > 100:
            raise ValueError("剧名过长（最多 100 字）")
        # 任务key：有alias_key时用 剧名:别名（独立任务），否则用 剧名（多候选任务）
        task_key = f"title:{identifier}:{alias_key}" if alias_key else f"title:{identifier}"
        # 查重：按规范化剧名（中英文标点等价、大小写不敏感）比较，
        # 兼容旧任务 key（原始剧名），避免同一部剧因标点形式不同被重复添加
        norm_id = _normalize_title(identifier)
        existing = None
        if alias_key:
            existing = self._tasks.get(task_key)
        else:
            for _task in self._tasks.values():
                if (_task.input_type == "title"
                        and _normalize_title(_task.input_value) == norm_id
                        and _task.status not in (STATUS_DONE, STATUS_FAILED)):
                    existing = _task
                    break
            if existing is None:
                existing = self._tasks.get(task_key)
        if existing and existing.status not in (STATUS_DONE, STATUS_FAILED):
            raise ValueError(f"剧名「{identifier}」已在任务列表中")
        task = StationTask(book_id=task_key, input_type="title", input_value=identifier)
        task.content_type = str(content_type or "").strip()
        task.expected_episodes = max(0, int(expected_episodes or 0))
        if existing:
            task.title = existing.title
            task.resolved_book_id = existing.resolved_book_id
            task.content_type = task.content_type or existing.content_type
            task.expected_episodes = task.expected_episodes or existing.expected_episodes
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
                # 解析可选别名：首选竖线|（原剧名几乎不会用），兼容英文分号;、中文分号；、制表符
                parts = re.split(r"[|;；\t]", line, maxsplit=1)
                identifier = parts[0].strip()
                alias_part = parts[1].strip() if len(parts) > 1 else ""
                # 多个别名首选竖线|分隔，兼容中文逗号，、英文逗号,
                alias_names = [a.strip() for a in re.split(r"[|,，]", alias_part) if a.strip()] if alias_part else []
                try:
                    # 任务key策略：
                    # - BookID：用 BookID 作为key
                    # - 剧名+多个别名（同一行）：用 剧名 作为key，多个别名作为候选
                    # - 剧名+单个别名（每行一个）：用 剧名:别名 作为key，独立任务
                    # - 剧名+无别名：用 剧名 作为key
                    is_book = _is_book_id(identifier)
                    if is_book:
                        task = self.add_book_id(identifier)
                    elif len(alias_names) > 1:
                        # 同一行多个别名 → 同一个任务，多个候选
                        task = self.add_task(identifier)
                    elif len(alias_names) == 1:
                        # 每行一个别名 → 独立任务，key=剧名:别名
                        task = self.add_task(identifier, alias_key=alias_names[0])
                    else:
                        # 无别名 → 用剧名作为key
                        task = self.add_task(identifier)
                except ValueError as error:
                    msg = str(error)
                    if "已在任务列表中" in msg:
                        # 重复任务：只有"剧名+多个别名"模式才追加别名，其他模式跳过
                        is_book = _is_book_id(identifier)
                        if not is_book and len(alias_names) > 1:
                            task_key = f"title:{identifier}"
                            existing = self._tasks.get(task_key)
                            if existing and existing.status not in (STATUS_DOWNLOADING, STATUS_GENERATING, STATUS_APPLYING, STATUS_REVIEWING):
                                # 追加新别名，去重
                                existing.manual_alias = True
                                merged = list(dict.fromkeys((existing.manual_alias_names or []) + alias_names))
                                existing.manual_alias_names = merged
                                existing.manual_alias_name = merged[0]
                                existing.error = ""
                                existing.task_file = ""
                                if existing.status == STATUS_SUBMITTED:
                                    # 已提交过三端的任务：追加别名后回统一审核队列（不重复提交）
                                    existing.status = STATUS_QUEUED
                                    existing.detail = f"手动别名（追加）：{'、'.join(merged)}，待统一审核"
                                    if existing.book_id not in self._review_queue:
                                        self._review_queue.append(existing.book_id)
                                else:
                                    # 重置状态重新入队
                                    existing.status = STATUS_QUEUED
                                    existing.detail = f"手动别名（追加）：{'、'.join(merged)}"
                                    if existing.book_id not in self._queue:
                                        self._queue.append(existing.book_id)
                                existing.updated_at = time.time()
                                self._save_state()
                                added.append(existing.input_value or existing.book_id)
                            else:
                                skipped.append(identifier)
                        else:
                            skipped.append(identifier)  # BookID或单别名独立任务重复，跳过
                    else:
                        failed.append((identifier, msg))
                    continue
                if alias_names:
                    # 有别名：标记为手动别名模式
                    task.manual_alias = True
                    task.manual_alias_names = alias_names
                    task.manual_alias_name = alias_names[0]  # 兼容单个别名
                    task.detail = f"手动别名：{'、'.join(alias_names)}"
                    self._save_state()
                added.append(task.input_value or task.book_id)
        return {"added": added, "skipped": skipped, "failed": failed}

    def remove_task(self, book_id: str) -> None:
        if book_id in self._queue:
            self._queue.remove(book_id)
        if book_id in self._review_queue:
            self._review_queue.remove(book_id)
        if self._running and self._running.book_id == book_id:
            return  # 正在运行，不允许直接删除
        self._tasks.pop(book_id, None)
        self._save_state()

    def retry_task(self, book_id: str) -> None:
        task = self._tasks.get(book_id)
        if not task or task.status in (STATUS_DOWNLOADING, STATUS_GENERATING, STATUS_APPLYING, STATUS_REVIEWING):
            return
        # 已提交过三端的任务：重试直接回到统一审核队列（不重复提交）
        if task.status == STATUS_SUBMITTED:
            task.status = STATUS_QUEUED
            task.detail = "已重新加入统一审核队列"
            task.error = ""
            task.updated_at = time.time()
            if book_id not in self._review_queue:
                self._review_queue.append(book_id)
            self._save_state()
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
        raw_aliases = [a.strip() for a in re.split(r"[|,，\s\n]+", str(aliases_text or "")) if a.strip()]
        if not raw_aliases:
            raise ValueError("请输入至少一个别名")
        # 2. 校验别名格式（手动别名不被前缀后缀限制，只要求恰好4个中文字且互不重复）
        invalid = [a for a in raw_aliases if not re.fullmatch(r"[\u4e00-\u9fff]{4}", a)]
        if invalid:
            raise ValueError(f"以下别名必须恰好是4个中文汉字：{'、'.join(invalid)}")
        unique = list(dict.fromkeys(raw_aliases))
        if len(unique) != len(raw_aliases):
            dup = [a for a in raw_aliases if raw_aliases.count(a) > 1]
            raise ValueError(f"同一批别名不可重复：{'、'.join(dict.fromkeys(dup))}")
        validated = unique
        # 固定字仅用于任务文件展示/兼容旧字段，手动候选在 mjs 侧不校验固定字
        affix = validated[0][:2] if self.alias_mode == "prefix" else validated[0][2:]
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
            "manual": True,  # 手动别名：mjs 跳过固定字校验，按提供的原样申请
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
        task.error = ""
        if task.status == STATUS_SUBMITTED:
            # 已提交过三端的任务：更新别名后回统一审核队列（mjs 对未提交候选会自动先提交再审核）
            task.status = STATUS_QUEUED
            task.detail = f"别名已更新：{'、'.join(validated)}，待统一审核"
            if task.book_id not in self._review_queue:
                self._review_queue.append(task.book_id)
        else:
            task.status = STATUS_QUEUED
            task.detail = f"手动别名已提交：{'、'.join(validated)}"
            if task.book_id not in self._queue:
                self._queue.append(task.book_id)
        task.updated_at = time.time()
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
        raw_aliases = [a.strip() for a in re.split(r"[|,，\s\n]+", str(aliases_text or "")) if a.strip()]
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
                "manual": True,  # 手动别名：mjs 跳过固定字校验，按提供的原样申请
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
            task.error = ""
            if task.status == STATUS_SUBMITTED:
                # 已提交过三端的任务：更新别名后回统一审核队列（mjs 对未提交候选会自动先提交再审核）
                task.status = STATUS_QUEUED
                task.detail = f"别名已更新：{alias}，待统一审核"
                if task.book_id not in self._review_queue:
                    self._review_queue.append(task.book_id)
            else:
                task.status = STATUS_QUEUED
                task.detail = f"手动别名已提交：{alias}"
                if task.book_id not in self._queue:
                    self._queue.append(task.book_id)
            task.updated_at = time.time()
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

    def set_content_type(self, type_key: str) -> None:
        """设置全局内容类型：manju=漫剧 / wangwen=网文 / duanju=短剧；空=不限制（不过滤、不核验）。"""
        type_key = str(type_key or "").strip()
        if type_key and type_key not in ("manju", "wangwen", "duanju"):
            raise ValueError(f"内容类型只支持 manju/wangwen/duanju，got: {type_key}")
        self.content_type = type_key
        self._save_state()

    # 任务台「请选择计划发文的素材类型」默认选项（官方若新增/改名，改 data/post_type.json 的 options 即可，无需改代码）
    DEFAULT_POST_TYPES = [
        "解说混剪", "真人出镜", "图文", "解压TTS", "meme剪辑",
        "AIGC", "营销号", "沙雕漫", "AI数字人", "滚屏素材",
    ]

    def _load_post_type(self) -> str:
        """读取发文类型配置（data/post_type.json），缺失/非法回退「解说混剪」。"""
        try:
            data = json.loads((self.data_root / "post_type.json").read_text(encoding="utf-8"))
            value = str((data or {}).get("post_type") or "").strip()
            return value or "解说混剪"
        except Exception:  # noqa: BLE001
            return "解说混剪"

    def post_type_options(self) -> list[str]:
        """发文类型下拉选项：优先读 data/post_type.json 的 options（官方改选项时手动维护），
        缺失/非法回退内置默认列表。"""
        try:
            data = json.loads((self.data_root / "post_type.json").read_text(encoding="utf-8"))
            options = [str(x).strip() for x in (data or {}).get("options") or []]
            options = [x for x in options if x]
        except Exception:  # noqa: BLE001
            options = []
        return options or list(self.DEFAULT_POST_TYPES)

    def set_post_type(self, value: str) -> None:
        """设置发文类型并持久化到 data/post_type.json（下次打开保持）。"""
        value = str(value or "").strip() or "解说混剪"
        self.post_type = value
        # 写回时保留 options（用户手动维护的选项列表不能被覆盖）
        try:
            old_data = json.loads((self.data_root / "post_type.json").read_text(encoding="utf-8"))
            options = old_data.get("options") if isinstance(old_data, dict) else None
        except Exception:  # noqa: BLE001
            options = None
        payload = {"post_type": value}
        if options:
            payload["options"] = options
        try:
            (self.data_root / "post_type.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass

    def set_expected_episodes(self, episodes: int) -> None:
        """设置全局期望集数（0=不限制），用于同名同标签时精确区分。"""
        try:
            value = int(str(episodes or 0).strip())
        except ValueError as error:
            raise ValueError(f"集数必须是整数，got: {episodes}") from error
        self.expected_episodes = max(0, value)
        self._save_state()

    # ---------- 识图（Windows 系统 OCR，零依赖） ----------
    def ocr_image(self, image_path: str | Path) -> list[dict]:
        """对图片执行 Windows 系统 OCR，返回按行排列的识别结果 [{line, text, words:[{text,conf}]}]。

        绿色软件零依赖方案：调用内置 ocr.ps1（WinRT OcrEngine，Win10+ 自带中文语言包）。
        失败时抛 RuntimeError 并附系统返回信息。
        """
        image_path = Path(image_path)
        if not image_path.is_file():
            raise FileNotFoundError(f"图片不存在：{image_path}")
        ps1 = self.adapter_dir / "ocr.ps1"
        if not ps1.is_file():
            raise RuntimeError(f"缺少内置 ocr.ps1：{ps1}")
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            raise RuntimeError("系统缺少 PowerShell，无法执行识图")
        cmd = [
            powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(ps1), "-ImagePath", str(image_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
        output = (proc.stdout or "").strip()
        if proc.returncode != 0 or not output:
            raise RuntimeError(f"识图失败：{(proc.stderr or '').strip()[-400:] or '无输出'}")
        try:
            payload = json.loads(output)
        except ValueError as error:
            raise RuntimeError(f"识图输出解析失败：{output[:200]}") from error
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("message") or "识图失败"))
        return payload.get("lines") or []

    # 播放页截图 UI 噪声词：这些行是播放器控件/剧情描述，不是剧名
    _OCR_NOISE_MARKERS = (
        "第", "集", "选集", "全屏", "分享", "作者声明", "倍速", "免费观看",
        "展开", "返回", "弹幕", "评论", "转发", "缓存", "关注", "内容由",
    )
    # 剧名行首尾的播放器/弹窗装饰符号
    _OCR_TITLE_TRIM = "〈〉《》「」『』〔〕【】|│·•|｜　\"\"''（）()[]~～—-_.,，。!！?？:：;；"
    # 平台状态标签（完整词匹配）
    _OCR_STATE_TAGS = ("新剧", "热剧", "独播", "完结", "限免", "会员", "爆款", "推荐")
    # OCR 对"剧"字的常见误读（0/O/囗/口/固/目），短行（≤3字）按此模糊还原
    _OCR_TAG_FUZZY = {0: "剧", "O": "剧", "囗": "剧", "口": "剧", "固": "剧", "目": "剧", "0": "剧"}

    @staticmethod
    def _ocr_text_quality(text: str) -> float:
        """估算 OCR 文本质量：正常中文字符占比（0~1）。乱码（●●½ż◆ 类）占比极低。"""
        text = str(text or "")
        if not text:
            return 0.0
        normal = len(re.findall(r"[\u4e00-\u9fff，。！？、：；「」『』【】《》（）0-9a-zA-Z]", text))
        return normal / len(text)

    @classmethod
    def parse_ocr_meta(cls, lines: list[dict]) -> dict:
        """从 OCR 行提取 {title, episodes, content_type, tags}。

        - content_type：恒空——内容类型是用户主动选择的（识图/搜索时的定位条件），
          不是 OCR 识别对象，避免识别词覆盖用户选择
        - tags：平台状态标签（新剧/热剧/独播/完结/限免/会员…），含 OCR 短行模糊还原
          （"新0"/"新囗" → 新剧）
        - episodes：正则 `(\\d{1,4})\\s*集` / `第(\\d+)集`
        - title：过滤播放页 UI 噪声行后取最长行，并清理首尾装饰符号；
          全部被过滤（如纯 UI 截图）时回退原始最长行，允许 UI 修正
        """
        texts = [str(x.get("text", "") or "").strip() for x in lines if str(x.get("text", "") or "").strip()]
        meta: dict = {"title": "", "episodes": 0, "content_type": "", "tags": []}
        # 状态标签：完整词 + 短行模糊还原（"新0"→"新剧"）
        tags: list[str] = []
        for text in texts:
            for tag in cls._OCR_STATE_TAGS:
                if tag in text and tag not in tags:
                    tags.append(tag)
        for text in texts:
            if len(text) > 3:
                continue
            fixed = "".join(cls._OCR_TAG_FUZZY.get(ch, ch) for ch in text)
            for tag in ("新剧", "热剧", "独播", "完结"):
                if fixed == tag and tag not in tags:
                    tags.append(tag)
        meta["tags"] = tags
        # 集数：优先「全 N 集」全集数；次选 N 集（排除「第 N 集」当前集）；取最大值
        episode_values: list[int] = []
        for text in texts:
            for m in re.finditer(r"(\d{1,4})\s*集", text):
                num = int(m.group(1))
                before = text[max(0, m.start() - 2):m.start()]
                if "第" in before:
                    continue  # 「第 N 集」是当前集，不是总集数
                episode_values.append(num)
        if episode_values:
            meta["episodes"] = max(episode_values)
        if not texts:
            return meta
        clean = [t.strip().strip(cls._OCR_TITLE_TRIM).strip() for t in texts]
        clean = [t for t in clean if t]
        candidates = [t for t in clean if not any(n in t for n in cls._OCR_NOISE_MARKERS)]
        pool = candidates or clean
        best = max(pool, key=lambda t: (len(t), len(re.findall(r"[\u4e00-\u9fff]", t))))
        # OCR 会在汉字之间插入空格（"沂 居 天 桥"），去掉中文-中文之间的空白；英文词间空格保留
        best = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", best)
        # 清理显示不全的截断符（"…"/"..."/"。。"结尾 = 平台省略号，不是剧名一部分）
        best = re.sub(r"(?:…|\.\.\.|。{2,})$", "", best)
        best = best.strip()
        # 绿色过滤后角标与剧名连行（"新剧天桥乞讨…"）：OCR 独立词命中的标签词在开头 → 移除
        leading_tags = set()
        for x in lines:
            for w in x.get("words", []):
                wt = str(w.get("text", "") or "").strip()
                if wt in cls._OCR_STATE_TAGS:
                    leading_tags.add(wt)
        for tag in sorted(leading_tags, key=len, reverse=True):
            if best.startswith(tag) and (len(best) - len(tag)) >= 4:
                best = best[len(tag):]
                break
        else:
            # 兜底：OCR 未分独立词时，若开头标签词且剩余仍 ≥8 字（足够长的剧名），保守移除
            for tag in cls._OCR_STATE_TAGS:
                if best.startswith(tag) and (len(best) - len(tag)) >= 8:
                    best = best[len(tag):]
                    break
        meta["title"] = best
        return meta

    def add_tasks_from_images(self, image_paths: list[str | Path], content_type: str = "") -> dict:
        """批量识图添加任务：逐张 OCR → 提取剧名/集数 → 创建任务。

        - content_type：用户主动选择的内容类型（"manju"/"wangwen"/"duanju"，空=不限），
          应用到每张图添加的任务——类型不是 OCR 识别对象，同名不同标签时按此过滤
        - 识别不出剧名 / 乱码低质量 / 已在任务列表 的图片计入 skipped（不中断整批）
        - 集数以识别结果为准，识别不到则用 0（不限制）
        - 返回 {"added": [(剧名, meta)], "skipped": [(图片, 原因)]}
        """
        content_type = str(content_type or "").strip()
        if content_type and content_type not in ("manju", "wangwen", "duanju"):
            raise ValueError(f"内容类型只支持 manju/wangwen/duanju，got: {content_type}")
        added: list[tuple[str, dict]] = []
        skipped: list[tuple[str, str]] = []
        for raw in image_paths:
            image = Path(raw)
            label = str(image)
            try:
                lines = self.ocr_image(image)
                meta = self.parse_ocr_meta(lines)
                title = str(meta["title"] or "").strip()
                if not title or self._ocr_text_quality(title) < 0.45:
                    skipped.append((label, "未识别出有效剧名（图片文字不清晰或为乱码）"))
                    continue
                task = self.add_task(title, content_type=content_type)
                if meta["episodes"]:
                    task.expected_episodes = meta["episodes"]
                parts = [title]
                if meta["episodes"]:
                    parts.append(f"{meta['episodes']}集")
                if content_type:
                    parts.append(content_type)
                if meta.get("tags"):
                    parts.append("/".join(meta["tags"]))
                task.detail = "识图添加：" + "，".join(parts)
                self._save_state()
                added.append((title, meta))
            except ValueError as error:  # 已在任务列表等
                skipped.append((label, str(error)))
            except Exception as error:  # noqa: BLE001
                skipped.append((label, str(error)))
        return {"added": added, "skipped": skipped}

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
        """调度入口：每次调用最多启动一个任务，在后台线程执行，不阻塞 UI。

        先跑提交队列（下载/生成/提交三端，提交完即走，不等待审核）；
        提交队列清空后，自动转入统一审核队列，逐个轮询三端审核结果。
        """
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
            # 提交队列已空：进入统一审核阶段
            while self._review_queue:
                book_id = self._review_queue[0]
                task = self._tasks.get(book_id)
                if task is None:
                    self._review_queue.pop(0)
                    continue
                if task.status == STATUS_DONE:
                    self._review_queue.pop(0)
                    continue
                self._running = task
                self._emit(book_id, STATUS_REVIEWING, "开始统一审核")
                thread = threading.Thread(target=self._run_review_async, args=(task,), daemon=True)
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

    def _run_review_async(self, task: StationTask) -> None:
        """后台线程执行统一审核，结束后移出审核队列。"""
        try:
            self._review(task)
        finally:
            with self._lock:
                self._running = None
                if task.book_id in self._review_queue:
                    self._review_queue.remove(task.book_id)
                self._save_state()

    # ---------- 三阶段执行 ----------
    def _run_task(self, task: StationTask) -> None:
        if not self._ensure_model_service():
            task.status = STATUS_FAILED
            task.error = "本地 Ollama 无法启动，请检查绿色文件夹是否完整"
            task.detail = task.error
            self._emit(task.book_id, task.status, task.detail)
            return
        # 手动别名申请：跳过下载和生成，直接进入三端申请（先提交，统一审核）
        if task.manual_alias and task.task_file:
            self._apply(task, submit_only=True)
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
            self._apply(task, submit_only=True)
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
        content_type = (task.content_type or "").strip()
        if task.expected_episodes > 0:
            extra_args = [*extra_args, "--expected-episodes", str(task.expected_episodes)]
        if task.input_type == "title":
            cmd = [
                self.node_command, "--experimental-websocket",
                str(adapter),
                "--title", task.input_value,
                "--content-type", content_type,
                "--output-dir", str(download_dir),
                "--cover-output-dir", str(cover_dir),
                *extra_args,
            ]
        else:
            cmd = [
                self.node_command, "--experimental-websocket",
                str(adapter),
                "--book-id", task.book_id,
                "--content-type", content_type,
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
            # 手动别名不受前缀/后缀模式限制，按提供的原样使用；但必须恰好4个中文字且互不重复（mjs端/平台要求）
            invalid = [a for a in aliases if not re.fullmatch(r"[\u4e00-\u9fff]{4}", a)]
            if invalid:
                raise ValueError(f"以下手动别名必须恰好是4个中文汉字：{'、'.join(invalid)}")
            unique = list(dict.fromkeys(aliases))
            if len(unique) != len(aliases):
                dup = [a for a in aliases if aliases.count(a) > 1]
                raise ValueError(f"同一批手动别名不可重复：{'、'.join(dict.fromkeys(dup))}")
            aliases = unique
            # 固定字仅用于任务文件展示/兼容旧字段，手动候选在 mjs 侧不校验固定字
            affix = aliases[0][:2] if self.alias_mode == "prefix" else aliases[0][2:]
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
                "manual": True,  # 手动别名：mjs 跳过固定字校验，按提供的原样申请
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
            from station_alias import generate_alias_candidates, write_alias_task
        except ImportError as error:
            raise RuntimeError(f"独立软件缺少候选生成模块：{error}") from error

        # 根据 fix_affix 决定是否限制前缀/后缀：选上且有值=用指定固定字，否则=AI自由生成4个字
        affix = self.alias_prefix if (self.fix_affix and self.alias_prefix) else ""
        # excluded：排除已采用的别名 + 本任务历史上已尝试过的所有候选（失败/被拒的不再生成）
        excluded: set[str] = set()
        if str(task.approved_alias or "").strip():
            excluded.add(str(task.approved_alias).strip())
        if task.task_file:
            try:
                old_state = self._read_json(Path(task.task_file))
                if isinstance(old_state, dict):
                    for name in (old_state.get("candidates") or []):
                        if str(name or "").strip():
                            excluded.add(str(name).strip())
                    for row in (old_state.get("history") or []):
                        if isinstance(row, dict) and str(row.get("alias") or "").strip():
                            excluded.add(str(row.get("alias")).strip())
            except Exception:  # noqa: BLE001
                pass
        candidates = generate_alias_candidates(
            title, intro, count=3, prefix=affix,
            mode=self.alias_mode,
            excluded=excluded or None,
            shared_root=self.data_root,
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

    def _apply(self, task: StationTask, submit_only: bool = False) -> None:
        """阶段三：申请三端别名。

        submit_only=True：只提交三端申请，不等审核结果（mjs --submit-only）。
          提交完进入 waiting_review 的任务 → STATUS_SUBMITTED + 加入统一审核队列。
        submit_only=False：完整流程，提交后原地轮询审核直到出结果（watch 模式）。
        """
        task.status = STATUS_APPLYING
        task.detail = f"正在申请{platform_count(self.tool_root)}端关键词别名"
        task.updated_at = time.time()
        self._save_state()
        self._emit(task.book_id, task.status, task.detail)

        adapter = self._adapter("run-alias-task.mjs")
        log_path = self._log_file(task, "alias")
        cmd = [self.node_command, "--experimental-websocket", str(adapter), str(task.task_file)]
        if submit_only:
            cmd.append("--submit-only")
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
        elif submit_only and return_code == 0 and status == "waiting_review":
            # 已提交三端、平台审核中：不原地等待，转入统一审核队列
            task.status = STATUS_SUBMITTED
            task.detail = "已提交三端申请，待统一审核"
            if task.book_id not in self._review_queue:
                self._review_queue.append(task.book_id)
        elif return_code in (_ALIAS_EXIT_MANUAL, _ALIAS_EXIT_RETRY):
            task.status = STATUS_WAITING_MANUAL
            reason = "检测到平台滑块/安全验证" if return_code == _ALIAS_EXIT_MANUAL else "平台控件故障"
            task.error = reason
            task.detail = f"{reason}，已保留进度，请到平台处理后手动重试该任务"
        elif return_code == _ALIAS_EXIT_NEED_MORE:
            # 候选全部未通过：自动换新候选重试（手动别名除外，最多3轮防死循环），
            # 超过上限才转人工——对应"审核失败的自动加一个新任务返回去再申请"
            if not task.manual_alias and (task.auto_retry_count or 0) < 3:
                task.auto_retry_count = (task.auto_retry_count or 0) + 1
                round_no = task.auto_retry_count
                task.status = STATUS_GENERATING
                task.detail = f"第{round_no}轮候选全部未通过三端，自动生成新候选重试"
                task.updated_at = time.time()
                self._save_state()
                self._emit(task.book_id, task.status, task.detail)
                try:
                    self._generate(task)
                except Exception as error:  # noqa: BLE001
                    task.status = STATUS_WAITING_MANUAL
                    task.error = str(error)
                    task.detail = f"自动生成新候选失败：{error}"
                    task.updated_at = time.time()
                    self._save_state()
                    self._emit(task.book_id, task.status, task.detail)
                    return
                if task.status == STATUS_GENERATING:
                    self._apply(task, submit_only=True)
                return
            task.status = STATUS_WAITING_MANUAL
            task.error = "候选别名均未通过三端审核"
            task.detail = (
                f"已自动重试{task.auto_retry_count}轮仍无候选通过三端，请人工处理"
                if task.auto_retry_count else "现有候选均未三端通过，需要重新生成候选后重试"
            )
        else:
            task.status = STATUS_FAILED
            task.error = self._read_tail(log_path, 12)
            task.detail = f"三端申请未完成（退出码 {return_code}）：{task.error or '未知错误'}"
        task.updated_at = time.time()
        self._save_state()
        self._emit(task.book_id, task.status, task.detail)

    def _review(self, task: StationTask) -> None:
        """统一审核阶段：以 watch 模式轮询已提交任务的审核结果，直到通过或失败。"""
        task.status = STATUS_REVIEWING
        task.detail = "统一审核中，正在轮询三端审核结果"
        task.updated_at = time.time()
        self._save_state()
        self._emit(task.book_id, task.status, task.detail)
        self._apply(task, submit_only=False)

    # ---------- 辅助 ----------
    def _ensure_model_service(self) -> bool:
        try:
            urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1).close()
            return True
        except Exception:
            pass
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
            _register_active_proc(process)
            try:
                return_code = process.wait()
            finally:
                _unregister_active_proc(process)
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
