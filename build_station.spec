# -*- mode: python ; coding: utf-8 -*-
"""素材准备站 PyInstaller 打包配置（onedir）。

绿色文件夹目标结构（构建脚本 build_station.ps1 负责组装）：
    素材准备站\
      素材准备站.exe
      _internal\
        platform_adapter\      # 3 个 mjs
        runtime\node\          # node.exe（69MB 独立版）
      runtime\
        ollama\                # ollama.exe + CPU 版 lib（脚本复制，不含 CUDA/ROCm）
      models\ollama\           # qwen3:4b-instruct 模型（脚本复制）
      data\                    # 产出目录
"""

from pathlib import Path

_block_cipher = None

_station_root = Path(SPECPATH)                        # src/material_station
_repo_src = _station_root.parent                      # 仓库根/src（manju_editor 所在）
_node_dir = Path(r"D:\漫剧剪辑工具\release\漫剧自动任务中心_v52\_internal\runtime\node")
_adapter_dir = _station_root / "platform_adapter"

_datas = [
    (str(_adapter_dir), "platform_adapter"),
    (str(_node_dir), "runtime/node"),
]

a = Analysis(
    [str(_station_root / "station_main.py")],
    pathex=[str(_repo_src)],
    binaries=[],
    datas=_datas,
    hiddenimports=[
        "material_station.station_core",
        "material_station.station_main",
        "manju_editor.material_workflow",
        "manju_editor.alias_lookup",
        "manju_editor.cover_workflow",
        "manju_editor.runtime",
        "manju_editor.hook_text",
        "manju_editor.auto_tasks",
        "manju_editor.media_probe",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="素材准备站",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="素材准备站",
)
