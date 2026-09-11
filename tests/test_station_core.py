"""素材准备站核心调度器回归测试。

覆盖：BookID 校验、队列串行、状态持久化、下载信息定位、候选生成写任务、
三端别名退出码映射、删除/重试。全部通过 mock mjs 调用完成，不依赖 Chrome/Ollama。
"""

from pathlib import Path
import json

import pytest

from station_core import (
    StationCore,
    STATUS_QUEUED,
    STATUS_DOWNLOADING,
    STATUS_GENERATING,
    STATUS_APPLYING,
    STATUS_WAITING_MANUAL,
    STATUS_DONE,
    STATUS_FAILED,
)

BOOK_ID = "7670000000000000001"
BOOK_ID_2 = "7670000000000000002"


@pytest.fixture
def core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StationCore:
    """构造隔离的调度核心：数据目录在 tmp，Node 指向假文件，mjs 路径指向占位。"""
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "task-platform-download.mjs").write_text("// stub", encoding="utf-8")
    (adapter_dir / "run-alias-task.mjs").write_text("// stub", encoding="utf-8")
    node_file = tmp_path / "node.exe"
    node_file.write_bytes(b"MZ")
    value = StationCore(
        tool_root=tmp_path,
        data_root=tmp_path / "data",
        adapter_dir=adapter_dir,
        node_command=str(node_file),
    )
    # 禁止真实启动 Ollama
    monkeypatch.setattr(value, "_ensure_model_service", lambda: True)
    return value


def _write_drama_info(task_dir: Path, book_id: str, title: str = "示例剧", episodes: int = 12) -> None:
    info_dir = task_dir / "剧目信息"
    info_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "title": title,
        "book_id": book_id,
        "description": "测试简介",
        "chapters": [{"index": i + 1, "chapter_name": f"第{i + 1}集"} for i in range(episodes)],
    }
    (info_dir / "剧目信息.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def test_book_id_validation(core: StationCore) -> None:
    for bad in ("", "123", "abcd12345678901234"):
        with pytest.raises(ValueError):
            core.add_book_id(bad)
    # 首尾空格会被规整后接受
    task = core.add_book_id(f"  {BOOK_ID}  ")
    assert task.book_id == BOOK_ID
    assert task.status == STATUS_QUEUED


def test_add_duplicate_running_rejected(core: StationCore) -> None:
    core.add_book_id(BOOK_ID)
    with pytest.raises(ValueError):
        core.add_book_id(BOOK_ID)
    # 完成后允许重新添加
    core._tasks[BOOK_ID].status = STATUS_DONE
    core.add_book_id(BOOK_ID)


def test_state_persistence_roundtrip(tmp_path: Path) -> None:
    node_file = tmp_path / "node.exe"
    node_file.write_bytes(b"MZ")
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "task-platform-download.mjs").write_text("// s", encoding="utf-8")
    (adapter / "run-alias-task.mjs").write_text("// s", encoding="utf-8")
    first = StationCore(tmp_path, data_root=tmp_path / "data", adapter_dir=adapter, node_command=str(node_file))
    first.add_book_id(BOOK_ID)
    first._tasks[BOOK_ID].title = "标题剧"
    first._tasks[BOOK_ID].detail = "中途中断"
    first._tasks[BOOK_ID].status = STATUS_DOWNLOADING
    first._save_state()

    second = StationCore(tmp_path, data_root=tmp_path / "data", adapter_dir=adapter, node_command=str(node_file))
    restored = second._tasks.get(BOOK_ID)
    assert restored is not None
    assert restored.title == "标题剧"
    # 中断状态应恢复为排队可重跑，且仍在队列
    assert restored.status == STATUS_QUEUED
    assert BOOK_ID in second._queue


def test_serial_queue_runs_one_at_a_time(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    core.add_book_id(BOOK_ID)
    core.add_book_id(BOOK_ID_2)
    started: list[str] = []
    monkeypatch.setattr(core, "_run_task", lambda task: started.append(task.book_id))
    core.tick()
    # tick() 在后台线程执行任务，等待第一个任务完成（mock 很快）
    for _ in range(50):
        if core._running is None:
            break
        time.sleep(0.02)
    assert started == [BOOK_ID]
    # 第二次 tick 才轮到第二个
    core.tick()
    for _ in range(50):
        if core._running is None:
            break
        time.sleep(0.02)
    assert started == [BOOK_ID, BOOK_ID_2]


def test_download_locates_info_and_advances(core: StationCore) -> None:
    task = core.add_book_id(BOOK_ID)
    # 预置"下载完成"的剧目信息
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID, title="示例剧", episodes=12)
    core._run_process = lambda *args, **kwargs: (0, "")  # type: ignore[method-assign]
    core._download(task)
    assert task.status == STATUS_DOWNLOADING
    assert task.title == "示例剧"
    assert "12 集" in task.detail
    assert task.task_dir == str(task_dir)
    assert Path(task.info_file).is_file()


def test_download_failure_marks_failed(core: StationCore) -> None:
    task = core.add_book_id(BOOK_ID)
    log_file = core._log_file(task, "download")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("搜索失败\n", encoding="utf-8")
    core._run_process = lambda *args, **kwargs: (10, "搜索失败")  # type: ignore[method-assign]
    core._download(task)
    assert task.status == STATUS_FAILED
    assert "搜索失败" in task.error


def test_generate_writes_alias_task(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    task = core.add_book_id(BOOK_ID)
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    task.status = STATUS_DOWNLOADING
    task.title = "示例剧"
    task.info_file = str(task_dir / "剧目信息" / "剧目信息.json")
    task.task_dir = str(task_dir)

    candidates = ["知夏藏锋", "知夏翻盘", "知夏扬眉"]
    monkeypatch.setattr(
        "station_alias.generate_alias_candidates",
        lambda *args, **kwargs: candidates,
    )
    core._generate(task)
    assert task.status == STATUS_GENERATING
    task_file = Path(task.task_file)
    assert task_file.is_file()
    payload = json.loads(task_file.read_text(encoding="utf-8"))
    assert payload["book_id"] == BOOK_ID
    assert payload["title"] == "示例剧"
    assert payload["candidate_rows"][0]["alias"] == "知夏藏锋"


def test_apply_success_marks_done(core: StationCore) -> None:
    task = core.add_book_id(BOOK_ID)
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    task_file = task_dir / "剧目信息" / "三端别名任务.json"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(json.dumps({
        "version": 2,
        "title": "示例剧",
        "book_id": BOOK_ID,
        "status": "completed",
        "approved_alias": "知夏藏锋",
        "candidate_rows": [{"order": 1, "alias": "知夏藏锋", "status": "approved"}],
    }, ensure_ascii=False), encoding="utf-8")
    task.task_file = str(task_file)
    task.status = STATUS_APPLYING
    core._run_process = lambda *args, **kwargs: (0, "")  # type: ignore[method-assign]
    core._apply(task)
    assert task.status == STATUS_DONE
    assert task.approved_alias == "知夏藏锋"


@pytest.mark.parametrize("exit_code,expected_status", [
    (29, STATUS_WAITING_MANUAL),  # 人工滑块验证
    (30, STATUS_WAITING_MANUAL),  # 平台控件故障重试
    (25, STATUS_WAITING_MANUAL),  # 候选均未通过
    (99, STATUS_FAILED),          # 其他错误
])
def test_apply_exit_code_mapping(core: StationCore, exit_code: int, expected_status: str) -> None:
    task = core.add_book_id(BOOK_ID)
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    task_file = task_dir / "剧目信息" / "三端别名任务.json"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(json.dumps({"version": 2, "status": "running"}, ensure_ascii=False), encoding="utf-8")
    task.task_file = str(task_file)
    task.status = STATUS_APPLYING
    log_file = core._log_file(task, "alias")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("mjs 输出\n", encoding="utf-8")
    core._run_process = lambda *args, **kwargs: (exit_code, "")  # type: ignore[method-assign]
    core._apply(task)
    assert task.status == expected_status


def test_remove_and_retry(core: StationCore) -> None:
    task = core.add_book_id(BOOK_ID)
    assert BOOK_ID in core._queue
    core.remove_task(BOOK_ID)
    assert core._tasks.get(BOOK_ID) is None

    core.add_book_id(BOOK_ID_2)
    core._tasks[BOOK_ID_2].status = STATUS_FAILED
    core.retry_task(BOOK_ID_2)
    assert core._tasks[BOOK_ID_2].status == STATUS_QUEUED
    assert BOOK_ID_2 in core._queue


def test_node_command_resolution(tmp_path: Path) -> None:
    node_file = tmp_path / "node.exe"
    node_file.write_bytes(b"MZ")
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "task-platform-download.mjs").write_text("// s", encoding="utf-8")
    (adapter / "run-alias-task.mjs").write_text("// s", encoding="utf-8")
    core = StationCore(tmp_path, data_root=tmp_path / "data", adapter_dir=adapter, node_command=str(node_file))
    assert core.node_command == str(node_file)


# ========== 剧名识别模式（input_type=title） ==========

TITLE_SAMPLE = "知夏雾兽"
TITLE_SAMPLE_2 = "爹爹他不装了"


def test_add_task_auto_detect_book_id_vs_title(core: StationCore) -> None:
    """纯数字 16~20 位识别为 BookID 模式；其他识别为剧名模式。"""
    book_task = core.add_task(BOOK_ID)
    assert book_task.input_type == "book_id"
    assert book_task.input_value == BOOK_ID
    assert book_task.book_id == BOOK_ID

    title_task = core.add_task(TITLE_SAMPLE)
    assert title_task.input_type == "title"
    assert title_task.input_value == TITLE_SAMPLE
    assert title_task.book_id == f"title:{TITLE_SAMPLE}"


def test_add_task_title_queued_and_persisted(core: StationCore) -> None:
    task = core.add_task(TITLE_SAMPLE)
    assert task.status == STATUS_QUEUED
    assert f"title:{TITLE_SAMPLE}" in core._queue
    # 状态文件 roundtrip
    reloaded = StationCore(
        tool_root=core.tool_root, data_root=core.data_root,
        adapter_dir=core.adapter_dir, node_command=core.node_command,
    )
    restored = reloaded._tasks.get(f"title:{TITLE_SAMPLE}")
    assert restored is not None
    assert restored.input_type == "title"
    assert restored.input_value == TITLE_SAMPLE


def test_add_task_title_dedup(core: StationCore) -> None:
    core.add_task(TITLE_SAMPLE)
    with pytest.raises(ValueError):
        core.add_task(TITLE_SAMPLE)
    # 首尾空格规整后视为同一剧名
    with pytest.raises(ValueError):
        core.add_task(f"  {TITLE_SAMPLE}  ")
    # 完成后允许重新添加
    core._tasks[f"title:{TITLE_SAMPLE}"].status = STATUS_DONE
    core.add_task(TITLE_SAMPLE)


def test_add_task_title_validation(core: StationCore) -> None:
    for bad in ("", "   ", "a" * 101):
        with pytest.raises(ValueError):
            core.add_task(bad)


def test_download_title_mode_passes_title_arg(core: StationCore) -> None:
    """剧名模式下 _download 应向 mjs 传 --title 而非 --book-id。"""
    task = core.add_task(TITLE_SAMPLE)
    captured: list[list[str]] = []
    def fake_run(cmd, env, log_path):
        captured.append(list(cmd))
        return (0, "")
    core._run_process = fake_run  # type: ignore[method-assign]
    # 预置剧目信息，避免 _locate_info 失败
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / TITLE_SAMPLE
    _write_drama_info(task_dir, BOOK_ID, title=TITLE_SAMPLE, episodes=8)
    core._download(task)
    assert task.status == STATUS_DOWNLOADING
    cmd = captured[0]
    assert "--title" in cmd
    assert TITLE_SAMPLE in cmd
    assert "--book-id" not in cmd


def test_locate_info_title_mode_matches_by_title(core: StationCore) -> None:
    """剧名模式下 _locate_info 应按剧名匹配剧目信息.json，并回填真实 BookID。"""
    task = core.add_task(TITLE_SAMPLE)
    # 预置另一个剧名的干扰项 + 目标剧名
    other_dir = core.data_root / "选剧文件夹" / "原剧视频" / "其他剧"
    _write_drama_info(other_dir, BOOK_ID_2, title="其他剧", episodes=5)
    target_dir = core.data_root / "选剧文件夹" / "原剧视频" / TITLE_SAMPLE
    _write_drama_info(target_dir, BOOK_ID, title=TITLE_SAMPLE, episodes=8)

    info = core._locate_info(task, core.data_root / "选剧文件夹" / "原剧视频")
    assert info is not None
    assert info["title"] == TITLE_SAMPLE
    assert info["book_id"] == BOOK_ID
    # 下载完成后回填 resolved_book_id
    core._run_process = lambda *a, **k: (0, "")  # type: ignore[method-assign]
    core._download(task)
    assert task.resolved_book_id == BOOK_ID
    assert task.title == TITLE_SAMPLE


def test_log_file_safe_for_title_with_special_chars(core: StationCore) -> None:
    """剧名含文件名字符（/ \\ : * ? " < > |）时日志文件名必须安全。"""
    weird_title = "知夏/雾兽:第*一季?"
    task = core.add_task(weird_title)
    log_path = core._log_file(task, "download")
    name = log_path.name
    for ch in '/\\:*?"<>|':
        assert ch not in name
    assert name.endswith("_download.log")


def test_alias_prefix_passed_to_generate(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户开启固定前缀/后缀并设置前缀后，_generate 调用 generate_alias_candidates 时 prefix 必须是用户前缀。
    关闭固定前缀/后缀时，prefix 为空（AI自由生成4个字），不再回退默认前缀。"""
    task = core.add_book_id(BOOK_ID)
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    task.status = STATUS_DOWNLOADING
    task.title = "示例剧"
    task.info_file = str(task_dir / "剧目信息" / "剧目信息.json")
    task.task_dir = str(task_dir)

    captured: dict = {}
    def fake_generate(title, intro, count=3, prefix="", excluded=None, mode="prefix", shared_root=None):
        captured["prefix"] = prefix
        return ["知夏藏锋", "知夏翻盘", "知夏扬眉"]
    monkeypatch.setattr("station_alias.generate_alias_candidates", fake_generate)
    monkeypatch.setattr("station_alias.write_alias_task", lambda *a, **k: Path(task_dir) / "三端别名任务.json")

    # 开启固定前缀+设置前缀 → 传用户前缀
    core.set_fix_affix(True)
    core.set_alias_prefix("雾兽")
    core._generate(task)
    assert captured.get("prefix") == "雾兽"

    # 关闭固定前缀 → 传空（AI自由生成），不再回退默认
    core.set_fix_affix(False)
    captured.clear()
    core._generate(task)
    assert captured.get("prefix") == ""


def test_generate_and_write_use_same_prefix(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """_generate 调用 generate_alias_candidates 和 write_alias_task 必须用同一个前缀（用户前缀或默认）。
    回归：曾出现 generate 用用户前缀「年年」但 write_alias_task 误用默认「知夏」，导致候选全被过滤报错。"""
    task = core.add_book_id(BOOK_ID)
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    task.status = STATUS_DOWNLOADING
    task.title = "示例剧"
    task.info_file = str(task_dir / "剧目信息" / "剧目信息.json")
    task.task_dir = str(task_dir)

    generate_calls: list[dict] = []
    write_calls: list[dict] = []
    def fake_generate(title, intro, count=3, prefix="", excluded=None, mode="prefix", shared_root=None):
        generate_calls.append({"prefix": prefix})
        return [f"{prefix}藏锋", f"{prefix}翻盘", f"{prefix}扬眉"]
    def fake_write(project, values, preferred_series_root=None, prefix="", mode="prefix"):
        write_calls.append({"prefix": prefix, "values": values, "mode": mode})
        return Path(task_dir) / "三端别名任务.json"
    monkeypatch.setattr("station_alias.generate_alias_candidates", fake_generate)
    monkeypatch.setattr("station_alias.write_alias_task", fake_write)

    # 开启固定前缀+用户设置前缀「年年」
    core.set_fix_affix(True)
    core.set_alias_prefix("年年")
    core._generate(task)
    assert generate_calls[-1]["prefix"] == "年年"
    assert write_calls[-1]["prefix"] == "年年"
    # write_alias_task 收到的候选必须是 generate 返回的（未被前缀过滤）
    assert write_calls[-1]["values"] == ["年年藏锋", "年年翻盘", "年年扬眉"]

    # 关闭固定前缀 → 两者都传空（AI自由生成4个字），不再回退默认
    core.set_fix_affix(False)
    generate_calls.clear()
    write_calls.clear()
    core._generate(task)
    assert generate_calls[-1]["prefix"] == ""
    assert write_calls[-1]["prefix"] == ""


def test_alias_prefix_persisted_across_reload(core: StationCore, tmp_path: Path) -> None:
    """别名前缀必须持久化到 station_tasks.json，重新构造 StationCore 后能恢复。"""
    core.set_alias_prefix("知夏")
    assert core.alias_prefix == "知夏"

    # 用同一数据目录重新构造，验证前缀恢复
    adapter_dir = tmp_path / "adapter2"
    adapter_dir.mkdir()
    (adapter_dir / "task-platform-download.mjs").write_text("// stub", encoding="utf-8")
    (adapter_dir / "run-alias-task.mjs").write_text("// stub", encoding="utf-8")
    node_file = tmp_path / "node2.exe"
    node_file.write_bytes(b"MZ")
    core2 = StationCore(
        tool_root=tmp_path,
        data_root=tmp_path / "data",
        adapter_dir=adapter_dir,
        node_command=str(node_file),
    )
    assert core2.alias_prefix == "知夏"

    # 清空前缀也能持久化
    core2.set_alias_prefix("")
    core3 = StationCore(
        tool_root=tmp_path,
        data_root=tmp_path / "data",
        adapter_dir=adapter_dir,
        node_command=str(node_file),
    )
    assert core3.alias_prefix == ""


def test_suffix_mode_valid_alias_candidates() -> None:
    """后缀模式下 valid_alias_candidates 应校验'两字+后缀'格式，拒绝'前缀+两字'。"""
    from station_alias import valid_alias_candidates
    # 后缀模式：两字+知夏
    result = valid_alias_candidates(["藏锋知夏", "翻盘知夏", "扬眉知夏"], prefix="知夏", mode="suffix")
    assert result == ["藏锋知夏", "翻盘知夏", "扬眉知夏"]
    # 前缀模式的候选在后缀模式下应被拒绝
    result2 = valid_alias_candidates(["知夏藏锋", "知夏翻盘"], prefix="知夏", mode="suffix")
    assert result2 == []


def test_alias_mode_setter_and_persistence(core: StationCore, tmp_path: Path) -> None:
    """alias_mode setter 应校验取值并持久化，重新构造后恢复。"""
    core.set_alias_mode("suffix")
    assert core.alias_mode == "suffix"
    # 非法值应报错
    import pytest as _pytest
    with _pytest.raises(ValueError):
        core.set_alias_mode("invalid")
    # 重新构造应恢复 suffix
    adapter_dir = tmp_path / "adapter3"
    adapter_dir.mkdir()
    (adapter_dir / "task-platform-download.mjs").write_text("// stub", encoding="utf-8")
    (adapter_dir / "run-alias-task.mjs").write_text("// stub", encoding="utf-8")
    node_file = tmp_path / "node3.exe"
    node_file.write_bytes(b"MZ")
    core2 = StationCore(tool_root=tmp_path, data_root=tmp_path / "data", adapter_dir=adapter_dir, node_command=str(node_file))
    assert core2.alias_mode == "suffix"


def test_download_only_mode_marks_done_after_download(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """只下载模式：下载成功后应标记为 done，不执行生成/申请。"""
    task = core.add_book_id(BOOK_ID)
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    core.set_download_only(True)
    # mock 下载成功
    monkeypatch.setattr(core, "_run_process", lambda *a, **k: (0, ""))
    monkeypatch.setattr(core, "_ensure_model_service", lambda: True)
    # mock _generate 不应被调用
    generate_called = []
    monkeypatch.setattr(core, "_generate", lambda t: generate_called.append(True))
    core._run_task(task)
    assert task.status == "done"
    assert "下载完成" in task.detail
    assert generate_called == []  # 不应执行生成


def test_alias_only_mode_skips_download(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """只申请别名模式：应跳过下载，直接用已有剧目信息生成+申请。"""
    task = core.add_book_id(BOOK_ID)
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    core.set_alias_only(True)
    monkeypatch.setattr(core, "_ensure_model_service", lambda: True)
    # mock 生成和申请
    generate_called = []
    apply_called = []
    monkeypatch.setattr(core, "_generate", lambda t: generate_called.append(True) or setattr(t, "status", "generating"))
    monkeypatch.setattr(core, "_apply", lambda t, submit_only=False: apply_called.append(True))
    core._run_task(task)
    assert len(generate_called) == 1  # 应执行生成
    assert len(apply_called) == 1  # 应执行申请
    assert task.status == "generating"  # _generate 设置的状态（mock _apply 未改）


def test_alias_only_mode_auto_fetch_info_when_missing(core: StationCore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """只申请别名模式：没有剧目信息时自动用 info-only 获取（不下载视频/封面），成功后继续生成。"""
    task = core.add_book_id(BOOK_ID)
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    core.set_alias_only(True)
    monkeypatch.setattr(core, "_ensure_model_service", lambda: True)
    # mock _download：info-only 模式下写入剧目信息，设置状态为 downloading
    download_calls = []
    def fake_download(t, info_only=False):
        download_calls.append(info_only)
        _write_drama_info(task_dir, BOOK_ID)
        t.status = "downloading"
        t.info_file = str(task_dir / "剧目信息" / "剧目信息.json")
        t.title = "示例剧"
    monkeypatch.setattr(core, "_download", fake_download)
    generate_called = []
    apply_called = []
    monkeypatch.setattr(core, "_generate", lambda t: generate_called.append(True) or setattr(t, "status", "generating"))
    monkeypatch.setattr(core, "_apply", lambda t, submit_only=False: apply_called.append(True))
    core._run_task(task)
    assert download_calls == [True]  # 调用了 _download 且 info_only=True
    assert len(generate_called) == 1  # 继续执行生成
    assert len(apply_called) == 1  # 继续执行申请


def test_alias_only_mode_fails_when_info_fetch_fails(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """只申请别名模式：info-only 获取剧目信息失败时任务失败，不执行生成。"""
    task = core.add_book_id(BOOK_ID)
    core.set_alias_only(True)
    monkeypatch.setattr(core, "_ensure_model_service", lambda: True)
    # mock _download 失败：设置状态为 failed
    def fake_download(t, info_only=False):
        t.status = "failed"
        t.error = "mjs 退出码非0"
    monkeypatch.setattr(core, "_download", fake_download)
    generate_called = []
    monkeypatch.setattr(core, "_generate", lambda t: generate_called.append(True))
    core._run_task(task)
    assert task.status == "failed"
    assert generate_called == []  # 不执行生成


def test_download_info_only_passes_flag(core: StationCore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_download(info_only=True) 时 cmd 应包含 --info-only 参数。"""
    task = core.add_book_id(BOOK_ID)
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    captured_cmd = {}
    def fake_run_process(cmd, env, log_path):
        captured_cmd["cmd"] = cmd
        return (0, "")
    monkeypatch.setattr(core, "_run_process", fake_run_process)
    core._download(task, info_only=True)
    assert "--info-only" in captured_cmd["cmd"]


def test_apply_manual_alias_success(core: StationCore, tmp_path: Path) -> None:
    """手动别名申请：有剧目信息、别名格式正确时，写入三端别名任务.json并标记manual_alias。"""
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    task = core.apply_manual_alias(BOOK_ID, "知夏藏锋,知夏翻盘")
    assert task.manual_alias is True
    assert task.task_file
    assert task.status == "queued"
    # 验证三端别名任务.json已写入
    import json as _json
    data = _json.loads(Path(task.task_file).read_text(encoding="utf-8"))
    assert data["candidates"] == ["知夏藏锋", "知夏翻盘"]
    assert data["book_id"] == BOOK_ID
    assert len(data["candidate_rows"]) == 2


def test_apply_manual_alias_invalid_format(core: StationCore, tmp_path: Path) -> None:
    """手动别名申请：别名不是恰好4个中文字时抛出ValueError（手动别名不受前缀后缀限制）。"""
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    import pytest as _pytest
    with _pytest.raises(ValueError, match="4个中文汉字"):
        core.apply_manual_alias(BOOK_ID, "年年藏")  # 3个字，不符合


def test_apply_manual_alias_no_info(core: StationCore) -> None:
    """手动别名申请：没有剧目信息时抛出ValueError。"""
    import pytest as _pytest
    with _pytest.raises(ValueError, match="未找到剧目信息"):
        core.apply_manual_alias(BOOK_ID, "知夏藏锋")


def test_manual_alias_task_skips_to_apply(core: StationCore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """手动别名任务：_run_task应跳过下载和生成，直接调用_apply。"""
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    task = core.apply_manual_alias(BOOK_ID, "知夏藏锋")
    monkeypatch.setattr(core, "_ensure_model_service", lambda: True)
    apply_called = []
    download_called = []
    generate_called = []
    monkeypatch.setattr(core, "_download", lambda t, info_only=False: download_called.append(True))
    monkeypatch.setattr(core, "_generate", lambda t: generate_called.append(True))
    monkeypatch.setattr(core, "_apply", lambda t, submit_only=False: apply_called.append(True))
    core._run_task(task)
    assert len(apply_called) == 1  # 直接调用_apply
    assert download_called == []  # 不调用_download
    assert generate_called == []  # 不调用_generate


def test_add_tasks_from_file_mixed_bookid_and_title(core: StationCore, tmp_path: Path) -> None:
    """从 txt 批量导入：混合 BookID 和剧名，自动识别类型，空行/注释跳过。"""
    txt = tmp_path / "batch.txt"
    txt.write_text(
        f"{BOOK_ID}\n"
        "一碗热饭暖人性\n"
        "\n"
        "# 这是注释行\n"
        "  爹爹带娃闯江湖  \n",
        encoding="utf-8",
    )
    result = core.add_tasks_from_file(txt)
    assert len(result["added"]) == 3
    assert BOOK_ID in result["added"]
    assert "一碗热饭暖人性" in result["added"]
    assert "爹爹带娃闯江湖" in result["added"]
    assert result["skipped"] == []
    assert result["failed"] == []
    # 验证任务类型识别正确
    tasks = core.tasks
    book_task = next(t for t in tasks if t.input_type == "book_id")
    title_task = next(t for t in tasks if t.input_type == "title")
    assert book_task.input_value == BOOK_ID
    assert title_task.input_value == "一碗热饭暖人性"


def test_add_tasks_from_file_skips_existing(core: StationCore, tmp_path: Path) -> None:
    """已存在的任务应跳过，不报错。"""
    core.add_book_id(BOOK_ID)
    txt = tmp_path / "batch2.txt"
    txt.write_text(f"{BOOK_ID}\n新剧名称\n", encoding="utf-8")
    result = core.add_tasks_from_file(txt)
    assert len(result["added"]) == 1
    assert result["added"][0] == "新剧名称"
    assert BOOK_ID in result["skipped"]
    assert result["failed"] == []


def test_add_tasks_from_file_invalid_lines_fail(core: StationCore, tmp_path: Path) -> None:
    """无效行（如超长剧名）应记录为失败，不影响其他行。"""
    long_title = "剧" * 101
    txt = tmp_path / "batch3.txt"
    txt.write_text(f"{BOOK_ID}\n{long_title}\n", encoding="utf-8")
    result = core.add_tasks_from_file(txt)
    assert len(result["added"]) == 1
    assert result["added"][0] == BOOK_ID
    assert len(result["failed"]) == 1
    assert result["failed"][0][0] == long_title
    assert "过长" in result["failed"][0][1]


def test_add_tasks_from_file_nonexistent_raises(core: StationCore) -> None:
    """文件不存在应抛 FileNotFoundError。"""
    import pytest as _pytest
    with _pytest.raises(FileNotFoundError):
        core.add_tasks_from_file("nonexistent_file.txt")


def test_add_tasks_from_file_with_manual_alias(core: StationCore, tmp_path: Path) -> None:
    """txt中带别名的行（BookID;别名 或 剧名;别名）应标记manual_alias，无别名的行正常自动申请。"""
    txt = tmp_path / "batch_alias.txt"
    txt.write_text(
        f"{BOOK_ID};知夏藏锋\n"
        "一碗热饭暖人性;年年热饭\n"  # 剧名;别名
        "心跳引擎，冰山学姐有点甜;年年搜索\n"  # 剧名带中文逗号;别名
        "7680491888155036696\n"  # 无别名，自动申请
        , encoding="utf-8",
    )
    result = core.add_tasks_from_file(txt)
    assert len(result["added"]) == 4
    # 验证 BookID;别名
    task_with_alias = core._tasks[BOOK_ID]
    assert task_with_alias.manual_alias is True
    assert task_with_alias.manual_alias_name == "知夏藏锋"
    assert task_with_alias.input_type == "book_id"
    # 验证 剧名;别名（单个别名 → 独立任务，key=剧名:别名）
    title_task = core._tasks["title:一碗热饭暖人性:年年热饭"]
    assert title_task.manual_alias is True
    assert title_task.manual_alias_name == "年年热饭"
    assert title_task.input_type == "title"
    # 验证剧名带逗号的情况：心跳引擎，冰山学姐有点甜;年年搜索
    title_with_comma = core._tasks["title:心跳引擎，冰山学姐有点甜:年年搜索"]
    assert title_with_comma.manual_alias is True
    assert title_with_comma.manual_alias_name == "年年搜索"
    assert title_with_comma.input_type == "title"
    # 验证无别名的任务
    task_without = core._tasks["7680491888155036696"]
    assert task_without.manual_alias is False
    assert task_without.manual_alias_name == ""


def test_generate_with_manual_alias_skips_ai(core: StationCore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_generate有手动别名时跳过AI生成，直接写入三端别名任务.json。"""
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    task = core.add_book_id(BOOK_ID)
    task.manual_alias = True
    task.manual_alias_name = "知夏藏锋"
    task.info_file = str(task_dir / "剧目信息" / "剧目信息.json")
    # mock AI生成不应被调用
    ai_called = []
    monkeypatch.setattr("station_alias.generate_alias_candidates", lambda *a, **k: ai_called.append(True) or ["知夏AI1", "知夏AI2", "知夏AI3"])
    core._generate(task)
    assert ai_called == []  # AI生成未被调用
    assert task.task_file
    # 验证三端别名任务.json包含手动别名
    import json as _json
    data = _json.loads(Path(task.task_file).read_text(encoding="utf-8"))
    assert data["candidates"] == ["知夏藏锋"]
    assert task.status == "generating"

# ---------- 剧名标点等价 / 大小写不敏感 / 自动重试 ----------

def test_normalize_title_punctuation_and_case() -> None:
    from station_core import _normalize_title
    assert _normalize_title("妖妃蛊惑人心,暴君他失控了") == "妖妃蛊惑人心暴君他失控了"
    assert _normalize_title("妖妃蛊惑人心，暴君他失控了") == "妖妃蛊惑人心暴君他失控了"
    assert _normalize_title("妖妃蛊惑人心:暴君他失控了") == "妖妃蛊惑人心暴君他失控了"
    assert _normalize_title("妖妃蛊惑人心：暴君他失控了") == "妖妃蛊惑人心暴君他失控了"
    assert _normalize_title("《妖妃传》") == "妖妃传"
    assert _normalize_title("ABC Hero 2026") == _normalize_title("abc hero 2026")
    # 中英文逗号/冒号全等价
    eq = {"妖妃蛊惑人心,暴君他失控了", "妖妃蛊惑人心，暴君他失控了",
          "妖妃蛊惑人心:暴君他失控了", "妖妃蛊惑人心：暴君他失控了"}
    assert len({_normalize_title(x) for x in eq}) == 1


def test_add_task_title_dedup_punctuation_insensitive(core: StationCore) -> None:
    core.add_task("妖妃蛊惑人心,暴君他失控了")
    # 同一部剧（中英文逗号不同）不可重复添加
    with pytest.raises(ValueError):
        core.add_task("妖妃蛊惑人心，暴君他失控了")


def test_apply_need_more_auto_retries_then_manual(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """候选全部未通过（NEED_MORE）→ 自动生成新候选并再次申请，最多3轮后转人工。"""
    from station_core import _ALIAS_EXIT_NEED_MORE
    task = core.add_book_id(BOOK_ID)
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    task_file = task_dir / "剧目信息" / "三端别名任务.json"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(json.dumps({
        "version": 2, "status": "running",
        "candidates": ["知夏甲", "知夏乙"],
        "history": [{"alias": "知夏甲", "result": "rejected"}],
    }, ensure_ascii=False), encoding="utf-8")
    task.task_file = str(task_file)
    task.status = STATUS_APPLYING
    log_file = core._log_file(task, "alias")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("mjs 输出\n", encoding="utf-8")
    core._run_process = lambda *args, **kwargs: (_ALIAS_EXIT_NEED_MORE, "")  # type: ignore[method-assign]
    gen_calls: list[str] = []

    def fake_generate(t: object) -> None:
        gen_calls.append(str(getattr(t, "book_id", "")))
        setattr(t, "status", STATUS_GENERATING)

    monkeypatch.setattr(core, "_generate", fake_generate)
    core._apply(task)
    assert len(gen_calls) == 3  # 自动重试3轮
    assert task.auto_retry_count == 3
    assert task.status == STATUS_WAITING_MANUAL
    assert "自动重试3轮" in task.detail


def test_apply_need_more_manual_alias_not_auto_retried(core: StationCore) -> None:
    """手动别名任务候选全部失败：不自动换候选（用户指定别名），直接转人工。"""
    from station_core import _ALIAS_EXIT_NEED_MORE
    task = core.add_book_id(BOOK_ID)
    task.manual_alias = True
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "示例剧"
    _write_drama_info(task_dir, BOOK_ID)
    task_file = task_dir / "剧目信息" / "三端别名任务.json"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(json.dumps({
        "version": 2, "status": "running",
        "candidates": ["妖妃蛊惑", "暴君失控"],
        "history": [],
    }, ensure_ascii=False), encoding="utf-8")
    task.task_file = str(task_file)
    task.status = STATUS_APPLYING
    log_file = core._log_file(task, "alias")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("mjs 输出\n", encoding="utf-8")
    core._run_process = lambda *args, **kwargs: (_ALIAS_EXIT_NEED_MORE, "")  # type: ignore[method-assign]
    core._apply(task)
    assert task.auto_retry_count == 0
    assert task.status == STATUS_WAITING_MANUAL

def test_alias_task_path_safe_for_colon_title() -> None:
    """剧名含英文冒号时，别名任务路径必须与 mjs 下载目录一致（: 替换为 _），不触发 WinError 123。"""
    from types import SimpleNamespace
    from station_alias import alias_task_path, _safe_dir_title
    # 与 mjs safe() 一致
    assert _safe_dir_title("末日病宠:丧尸前任赖上我了") == "末日病宠_丧尸前任赖上我了"
    assert _safe_dir_title("岁岁和亲:换山河无恙第二季") == "岁岁和亲_换山河无恙第二季"
    assert ":" not in _safe_dir_title("a:b?c*d<e>f|g\"h")
    assert _safe_dir_title("   ") == "未命名"
    project = SimpleNamespace(series_folder="", platform_title="", title="末日病宠:丧尸前任赖上我了")
    p = alias_task_path(project, r"D:\素材准备站\data\选剧文件夹\原剧视频")
    assert "末日病宠_丧尸前任赖上我了" in str(p)
    assert p.parent.parent.name == "末日病宠_丧尸前任赖上我了"  # 目录名不含冒号


def test_generate_alias_task_writes_to_safe_dir(core: StationCore, monkeypatch: pytest.MonkeyPatch) -> None:
    """带英文冒号的剧名：生成阶段写三端别名任务.json 到下载目录（: 已替换为 _），不抛 WinError 123。"""
    import re as _re
    from pathlib import Path
    from types import SimpleNamespace
    task = core.add_book_id(BOOK_ID)
    task_dir = core.data_root / "选剧文件夹" / "原剧视频" / "末日病宠_丧尸前任赖上我了"
    _write_drama_info(task_dir, BOOK_ID, title="末日病宠:丧尸前任赖上我了")
    task.info_file = str(task_dir / "剧目信息" / "剧目信息.json")
    task.task_file = ""
    # mock 候选生成（不真正调 Ollama）
    fake_project = SimpleNamespace()

    def fake_write(project, values, preferred_series_root=None, prefix="", mode="prefix"):
        fake_project.series_folder = getattr(project, "series_folder", "")
        fake_project.title = getattr(project, "platform_title", "") or getattr(project, "title", "")
        fake_project.book_id = getattr(project, "book_id", "")
        fake_project.task_id = getattr(project, "task_id", "")
        fake_project.workflow_batch_id = getattr(project, "workflow_batch_id", "")
        from station_alias import alias_task_path
        return alias_task_path(project, preferred_series_root)

    monkeypatch.setattr("station_alias.generate_alias_candidates", lambda *a, **k: ["知夏甲", "知夏乙", "知夏丙"])
    monkeypatch.setattr("station_alias.write_alias_task", fake_write)
    task.status = STATUS_GENERATING
    core._generate(task)
    assert task.task_file  # 返回了路径
    path_str = str(task.task_file)
    assert "末日病宠_丧尸前任赖上我了" in path_str
    assert Path(path_str).parent.parent.name == "末日病宠_丧尸前任赖上我了"  # 目录名不含冒号
    # 路径必须真实存在（mkidr 由 write_alias_task 负责，这里验证目录可创建）
    p = Path(task.task_file)
    assert p.parent.name == "剧目信息"




def test_submit_alias_timeout_returns_failure_not_pending() -> None:
    """submitAlias 超时必须判失败（ok:false），不得返回 pending_visibility:true 当作成功继续下一个平台。"""
    from pathlib import Path
    mjs = Path(__file__).resolve().parent.parent / "platform_adapter" / "task-platform-download.mjs"
    text = mjs.read_text(encoding="utf-8")
    # 超时分支必须返回失败，不得有 pending_visibility:true 的默认成功
    assert "pending_visibility:true" not in text, "submitAlias 超时仍默认成功，会导致失败别名继续申请下一个平台"
    assert "按失败处理" in text
    # nonSuccess 不得依赖 dialogOpen（提交后弹窗可能关闭，失败消息以全局 notification 出现）
    assert "nonSuccess=messages.some" in text, "nonSuccess 仍依赖 dialogOpen，弹窗关闭后失败消息会被漏判"
    # 轮询时长至少 30 秒（60次*500ms）
    assert "i<40" in text
    assert "sleep(i<20?500:1000)" in text


def test_submit_alias_nonSuccess_regex_covers_common_failure_messages() -> None:
    """nonSuccess 正则必须覆盖平台常见失败/重复消息文本。"""
    import re
    from pathlib import Path
    mjs = Path(__file__).resolve().parent.parent / "platform_adapter" / "task-platform-download.mjs"
    text = mjs.read_text(encoding="utf-8")
    m = re.search(r"nonSuccess=messages\.some\(x=>/([^/]+)/", text)
    assert m, "未找到 nonSuccess 正则"
    pattern = re.compile(m.group(1))
    # 平台常见失败消息必须命中
    for msg in ["已提交", "审核中", "提交成功", "已申请此别名，请勿重复申请",
                "重复申请", "已存在", "申请失败", "不通过", "被驳回", "已拒绝",
                "该别名不可用", "不符合规范", "内容违规", "别名敏感"]:
        assert pattern.search(msg), f"nonSuccess 正则未覆盖: {msg}"


def test_platforms_config_driven_mjs() -> None:
    """平台列表必须配置化：data/platforms.json 写几个用几个，找不到回退默认3个；
    marker 随平台配置（缺省“漫剧”，可填“*”不限制）。"""
    from pathlib import Path
    mjs = Path(__file__).resolve().parent.parent / "platform_adapter" / "task-platform-download.mjs"
    text = mjs.read_text(encoding="utf-8")
    assert "DEFAULT_PLATFORMS" in text and "loadPlatforms" in text
    assert "path.join(ROOT,'platforms.json')" in text or "path.join(ROOT, 'platforms.json')" in text
    assert "写几个用几个" in text or "out.length?out:DEFAULT_PLATFORMS" in text
    # 配置缺失/非法回退默认
    assert "return DEFAULT_PLATFORMS" in text
    # 卡片与申词记录判断必须用 platform.marker，不再写死“漫剧”
    assert "t.includes(platform.marker)" in text
    assert "marker==='*'||" in text
    # --probe-platforms 自动探测内容库菜单
    assert "--probe-platforms" in text
    assert "tab_type=(\\d+)" in text
    # platforms.json 支持 // 行注释 与 /* 块注释，且不会误删字符串里的冒号
    assert "stripJsonComments" in text
    assert "s.replace(/\\/\\*[\\s\\S]*?\\*\\//g,'')" in text


def test_platforms_json_contains_tomato_ting() -> None:
    """data/platforms.json 必须包含 4 个平台，番茄畅听 tab=3；文件允许 // 注释。"""
    import json
    import re
    from pathlib import Path
    p = Path(r"D:\漫剧剪辑工具\素材准备站\data\platforms.json")
    assert p.is_file(), "缺少 data/platforms.json"
    raw = p.read_text(encoding="utf-8")
    cleaned = re.sub(r"/\*[\s\S]*?\*/", "", raw)
    cleaned = re.sub(r"(^|[^:])\/\/.*$", "\\1", cleaned, flags=re.M)
    cfg = json.loads(cleaned)
    assert isinstance(cfg, list) and len(cfg) == 4
    by_name = {x["name"]: x["tab"] for x in cfg}
    assert by_name == {"红果短剧": 6, "番茄小说": 2, "番茄畅听": 3, "红果漫剧": 16}

def test_build_uninstall_bat() -> None:
    """一键卸载脚本：杀进程、删程序本体；keep_data=False 连 data 一起删；GBK 可编码。"""
    from station_main import build_uninstall_bat
    keep = build_uninstall_bat(r"D:\漫剧剪辑工具\素材准备站", keep_data=True)
    assert 'taskkill /IM "素材准备站.exe" /F' in keep
    assert "素材准备站.exe" in keep
    assert keep.count("rd /s /q") == 3  # 仅 _internal / runtime / logs，不删 data
    assert "data" not in keep
    full = build_uninstall_bat(r"D:\漫剧剪辑工具\素材准备站", keep_data=False)
    assert "data" in full
    assert full.count("rd /s /q") == 6  # + data + 整个运行目录 + LOCALAPPDATA 登录态缓存
    keep.encode("gbk")
    full.encode("gbk")

def test_browser_profile_persists_outside_run_dir() -> None:
    """浏览器登录态必须存到运行目录之外（%LOCALAPPDATA%\\素材准备站），更新删目录不丢登录；
    并保留一次性迁移旧 profile 的逻辑。"""
    from pathlib import Path
    mjs = Path(__file__).resolve().parent.parent / "platform_adapter" / "task-platform-download.mjs"
    text = mjs.read_text(encoding="utf-8")
    assert "PERSIST_PROFILE_ROOT" in text and "LOCALAPPDATA" in text
    assert "LEGACY_PROFILE_ROOT" in text
    assert "renameSync" in text  # 旧 profile 一次性迁移
    assert "PROFILE_NAME" in text


def test_uninstall_full_mode_clears_login_cache() -> None:
    """彻底卸载模式必须同时删除 %LOCALAPPDATA%\\素材准备站（浏览器登录态缓存）。"""
    from station_main import build_uninstall_bat
    full = build_uninstall_bat(r"D:\漫剧剪辑工具\素材准备站", keep_data=False)
    assert "%LOCALAPPDATA%\\素材准备站" in full
    keep = build_uninstall_bat(r"D:\漫剧剪辑工具\素材准备站", keep_data=True)
    assert "LOCALAPPDATA" not in keep  # 保留模式不删登录态，重装免登录
