# 素材准备站（Material Station）

把「下载原剧视频 / 下载封面 / 申请三端关键词别名」从漫剧自动任务中心剥离出来的独立绿色软件。

- **部署方式**：整个文件夹拷到任意 Windows 设备即用，**免安装 Ollama**（内置 Node / Ollama / 模型）
- **适用设备**：另一台设备单独运行，不依赖漫剧自动任务中心
- **开发语言**：Python（PySide6 UI + 调度器）+ Node.js（mjs 平台自动化，Chrome CDP）
- **别名生成**：纯标准库实现（`station_alias.py`），调用本地 Ollama 生成 4 字别名候选

## 功能

1. **下载原剧视频**：按 BookID 或剧名定位红果漫剧，下载视频 + 封面
2. **申请三端别名**：红果短剧 / 番茄小说 / 红果漫剧三个平台自动提交审核
   - 支持 AI 生成候选（前缀/后缀可选，固定字开关）
   - 支持手动指定别名（UI 输入或 txt 批量导入）
   - 一个剧多个别名按顺序申请，第一个成功即结束；多行同剧名各带别名 = 多个独立任务
3. **模式选项**：只下载 / 只申请别名 / 前缀后缀 / 固定字开关 / 手动别名模式

## txt 批量导入格式

- 分隔符：竖线 `|` 首选（原剧名不会用），兼容分号 `;` `；` 和制表符
- 多个别名：`剧名|别名1|别名2`（同一个任务，候选依次申请）
- 每行一个别名：多行同剧名 = 多个独立任务
- BookID 支持：`BookID|别名`

## 目录结构

```
素材准备站/
├── station_main.py          # PySide6 界面
├── station_core.py          # 任务调度器（下载→生成→申请三端）
├── station_alias.py         # 别名生成（纯标准库，调本地 Ollama）
├── platform_adapter/        # 3 个 mjs（Chrome CDP 平台自动化）
├── build_station.spec       # PyInstaller 配置
├── build_station.ps1        # 绿色文件夹组装脚本
└── tests/                   # 回归测试
```

## 开发 / 构建

```powershell
# 测试（需 pytest）
python -m pytest tests/ -q

# 构建绿色文件夹（输出到 build_station_dist/素材准备站/）
python -m PyInstaller --noconfirm --clean --workpath build_station_work --distpath build_station_dist build_station.spec
# 然后运行 build_station.ps1 组装内置 Node / Ollama / 模型
```

## 迁移到另一台设备

- 拷贝整个绿色文件夹（`素材准备站.exe + _internal + runtime + models + data`）即可
- **Chrome 登录态**：绿色版使用专用 Chrome profile，首次需在专用 Chrome 里登录达人接单平台（koc.fqopenplatform.com）
- 内置模型默认 `qwen3:4b-instruct`（CPU 可用）；有 NVIDIA 显卡可换成 CUDA 版 Ollama + `qwen3:8b`
