# -*- coding: utf-8 -*-
import io

# ---------- station_core.py ----------
p = r'D:\漫剧剪辑工具\material-station\station_core.py'
t = io.open(p, encoding='utf-8').read()

old = """        self.post_type = self._load_post_type()  # 发文类型（平台弹窗素材类型），默认解说混剪，可选并记住"""
new = """        self.post_type = self._load_post_type()  # 发文类型（平台弹窗素材类型），默认解说混剪，可选并记住"""
assert old in t  # 确认锚点存在（本行不改）

# 1) 加 DEFAULT_POST_TYPES 常量（放在 _load_post_type 定义前）
anchor = "    def _load_post_type(self) -> str:"
const = '''    # 任务台「请选择计划发文的素材类型」默认选项（官方若新增/改名，改 data/post_type.json 的 options 即可，无需改代码）
    DEFAULT_POST_TYPES = [
        "解说混剪", "真人出镜", "图文", "解压TTS", "meme剪辑",
        "AIGC", "营销号", "沙雕漫", "AI数字人", "滚屏素材",
    ]

    def _load_post_type(self) -> str:'''
assert anchor in t
t = t.replace(anchor, const, 1)

# 2) 加 post_type_options()（放在 _load_post_type 之后、set_post_type 之前）
anchor2 = "    def set_post_type(self, value: str) -> None:"
options_method = '''    def post_type_options(self) -> list[str]:
        """发文类型下拉选项：优先读 data/post_type.json 的 options（官方改选项时手动维护），
        缺失/非法回退内置默认列表。"""
        try:
            data = json.loads((self.data_root / "post_type.json").read_text(encoding="utf-8"))
            options = [str(x).strip() for x in (data or {}).get("options") or []]
            options = [x for x in options if x]
        except Exception:  # noqa: BLE001
            options = []
        return options or list(self.DEFAULT_POST_TYPES)

    def set_post_type(self, value: str) -> None:'''
assert anchor2 in t
t = t.replace(anchor2, options_method, 1)

# 3) set_post_type 保留 options 写回（不能把用户手动维护的选项列表冲掉）
old3 = """        self.post_type = value
        (self.data_root / "post_type.json").write_text(
            json.dumps({"post_type": value}, ensure_ascii=False), encoding="utf-8"
        )"""
new3 = """        self.post_type = value
        # 写回时保留 options（用户手动维护的选项列表不能被覆盖）
        try:
            old_data = json.loads((self.data_root / "post_type.json").read_text(encoding="utf-8"))
            options = old_data.get("options") if isinstance(old_data, dict) else None
        except Exception:  # noqa: BLE001
            options = None
        payload = {"post_type": value}
        if options:
            payload["options"] = options
        (self.data_root / "post_type.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )"""
assert old3 in t
t = t.replace(old3, new3, 1)
io.open(p, 'w', encoding='utf-8', newline='\n').write(t)
print('station_core.py 已改')

# ---------- station_main.py ----------
p2 = r'D:\漫剧剪辑工具\material-station\station_main.py'
t2 = io.open(p2, encoding='utf-8').read()
old4 = """            self.post_type_combo.setEditable(True)
            # 任务台「请选择计划发文的素材类型」全部选项，直接点选；仍可编辑（可填新类型）
            self.post_type_combo.addItems([
                "解说混剪", "真人出镜", "图文", "解压TTS", "meme剪辑",
                "AIGC", "营销号", "沙雕漫", "AI数字人", "滚屏素材",
            ])"""
new4 = """            self.post_type_combo.setEditable(True)
            # 任务台「请选择计划发文的素材类型」选项：优先读 data/post_type.json 的 options
            # （官方新增/改名类型时，手动编辑该文件 options 列表即可，不用改代码），缺失回退内置默认
            self.post_type_combo.addItems(core.post_type_options())"""
assert old4 in t2
t2 = t2.replace(old4, new4, 1)
io.open(p2, 'w', encoding='utf-8', newline='\n').write(t2)
print('station_main.py 已改')
