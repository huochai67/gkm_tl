# gkm-tl

《学园偶像大师》(Gakuen iDOLM@STER) 游戏文本自动翻译工具。

从游戏服务器获取最新日文资源，对比现有中文翻译模版，调用 LLM 翻译新增或变更内容，输出可直接使用的翻译资源包。

## 功能

- **自动下载** — 从 Octo 服务器下载冒险脚本，从 GitHub 获取现有翻译模版和 master 数据
- **增量对比** — resource 比较嵌入的日文原文，Master 使用源文本快照识别变更，仅翻译 `new` / `changed` 条目
- **LLM 翻译** — 支持 OpenAI 兼容 API，可配置模型、批量大小、并发数
- **四类文本全覆盖** — 冒险脚本 (resource)、master 数据、generic JSON、localization JSON
- **角色名自动替换** — 内置 15 名偶像的中文名映射，自动替换对话中的角色名

## 架构

五阶段流水线，阶段间通过 JSON 文件传递数据，互不依赖：

```
01_download    02_extract    03_translate    04_build       05_package
    │              │              │              │              │
    ▼              ▼              ▼              ▼              ▼
 原始资源      统一提取为      LLM 批量翻译    重建完整目录    压缩发布包
 + 模版        extract.json   → translated    → output/      → zip
 + git repo    对比标记:        合并现有翻译      Gakumas
                new/existing                     Translation
                /changed                        Data/
```

## 快速开始

### 前置要求

- Python >= 3.11（推荐 PyPy 3.11）
- [uv](https://docs.astral.sh/uv/)（包管理器）

### 安装

```bash
uv sync
```

### 配置

```bash
Copy-Item config.yaml.example config.yaml
```

编辑 `config.yaml`，填入 LLM 的 `base_url`、`api_key`、`model`：

```yaml
llm:
  base_url: "https://api.openai.com/v1"
  api_key: "sk-..."
  model: "gpt-4o-mini"
  batch_size: 20
  max_concurrent: 5
```

也可通过 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`LLM_BACKEND`、`LLM_BATCH_SIZE`、`LLM_MAX_CONCURRENT` 和 `LLM_TIMEOUT` 覆盖对应的 `llm` 配置。环境变量优先于配置文件。

### 运行

```bash
uv run python run.py
```

流水线会自动执行全部五个阶段。也可单独运行某个阶段：

```bash
uv run python stages/01_download.py
uv run python stages/02_extract.py
uv run python stages/03_translate.py
uv run python stages/04_build.py
uv run python stages/05_package.py
```

最终翻译目录输出在 `output/GakumasTranslationData/`，压缩包输出在 `output/GakumasTranslationData.zip`。

## 流水线详情

### Stage 1: 下载

| 来源 | 内容 | 方式 |
|------|------|------|
| Octo 服务器 | `adv_*.txt` 冒险脚本 | HTTPS 请求 + AES-CBC 解密 |
| GitHub Release | 现有中文翻译模版 | GitHub API 下载 zip |
| gakumasu-diff | master 数据 YAML | GitHub ZIP 下载 |

每次运行会刷新 Octo 索引和 `gakumasu-diff`；刷新失败时回退到已有本地缓存。资源下载仅记录成功文件，失败文件会在下次运行重试。

### Stage 2: 提取

四种解析器分别处理对应格式：

- **`parser_resource`** — 解析 `[command key=value]` 格式的 TXT，提取 `message`/`narration`/`title`/`choicegroup` 文本
- **`parser_master`** — 解析 gakumasu-diff YAML，提取各记录的字符串字段
- **`parser_generic`** — 解析 `genericTrans/**/*.json` 的 key-value
- **`parser_localization`** — 深度遍历 `localization.json` 的所有叶子字符串

对比以 `(file, line, field)` 为唯一标识，标记每条记录为 `new` / `existing` / `changed`：没有现有译文为 `new`，原文未变为 `existing`，原文变更为 `changed`。

### Stage 3: 翻译

调用 LLM 批量翻译新增/变更内容。支持并发请求，可配置 `batch_size` 和 `max_concurrent`。

角色名通过映射表自动替换为中文。

### Stage 4: 打包

重建与 [chinosk6/GakumasTranslationData](https://github.com/chinosk6/GakumasTranslationData) 格式完全一致的目录结构：

- `resource/*.txt` — 替换为 `text=<r\=日文原文>中文翻译</r\>` 格式
- `message` 的 `name=` — 替换为中文角色名
- JSON 文件 — 直接替换字段值

### Stage 5: 压缩

将 `output/GakumasTranslationData/` 内的文件压缩为 `output/GakumasTranslationData.zip`。压缩包根目录直接包含 `version.txt` 和 `local-files/`。

## 项目结构

```
gkm-tl/
├── config.yaml.example      # 可提交的配置模版
├── pyproject.toml           # 项目元数据 & 依赖
├── run.py                   # 流水线调度入口
├── lib/
│   ├── config.py            # YAML 配置加载
│   ├── octo.py              # Octo API 交互
│   ├── parser_resource.py   # 冒险脚本解析器
│   ├── parser_master.py     # Master YAML 解析器
│   ├── parser_generic.py    # Generic JSON 解析器
│   └── parser_localization.py # Localization 解析器
├── stages/
│   ├── 01_download.py       # 下载阶段
│   ├── 02_extract.py        # 提取对比阶段
│   ├── 03_translate.py      # 翻译阶段
│   ├── 04_build.py          # 打包阶段
│   └── 05_package.py        # 压缩阶段
├── cache/                   # 缓存目录（自动生成）
│   ├── server/              # 服务器原始资源
│   ├── mod/                 # 现有翻译模版
│   └── gkm-diff/            # gakumasu-diff 仓库
├── docs/
│   ├── design.md            # 详细设计文档
│   └── plans/               # 开发计划
└── output/                  # 最终翻译包输出（自动生成）
```
