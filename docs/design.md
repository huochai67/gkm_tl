# gkm-tl: Gakumasu 自动翻译工具

## 概述

自动翻译《学园偶像大师》游戏文本的工具，可从游戏服务器获取最新原始日文资源，
与现有中文翻译模版对比，调用 LLM 翻译新增内容，生成完整的翻译资源包。

## 架构

### 五阶段流水线

```
01_download.py    02_extract.py     03_translate.py     04_build.py     05_package.py
      │                │                  │                  │                │
      ▼                ▼                  ▼                  ▼                ▼
  服务器 Octo     统一提取为          LLM 批量翻译      重建完整目录
   GitHub Release  extract.json       → translated.json  → output/         → zip
      │                │                  │                  │
      ▼                ▼                  ▼                  ▼
  cache/            extract.json     translated.json     output/
  ├── server/       对比标记:         合并现有翻译         GakumasTranslationData/
  ├── mod/          new / existing    + 新翻译             ├── resource/
  └── gkm-diff/     / changed                            ├── masterTrans/
                                                          └── genericTrans/
```

### 数据流

```json
// extract.json (统一中间格式)
{
  "uid": "adv_dear_amao_010:8:message.text",   // 唯一标识
  "source": "server",                            // server / mod / both
  "category": "resource",                        // resource / master / generic / localization
  "file": "adv_dear_amao_010.txt",
  "line": 8,
  "command": "message",                          // resource 专用
  "field": "text",                               // 具体字段名
  "jp": "みんな、今日は来てくれて、\n本当にありがとう！",
  "existing_cn": "",                              // 模版中已有翻译
  "status": "new"                                 // new / existing / changed
}
```

## Stage 1: 下载

### 数据来源

| 来源 | 内容 | 获取方式 |
|---|---|---|
| 游戏服务器 Octo Resources | adv_*.txt (3697 个冒险脚本) | octo API，无需认证 |
| GitHub Release | GakumasTranslationData.zip | GitHub API |
| gakumasu-diff | master 数据 YAML | GitHub ZIP 下载 |

### 流程

1. GitHub API 检查最新 release tag
2. 若 `mod/version.txt` 版本号不同，下载 zip 解压到 `cache/mod/`
3. 下载并原子替换 `gakumasu-diff` 缓存到 `cache/gkm-diff/`
4. 从 Octo 下载最新 adv TXT（增量下载，断点续传）
5. 记录下载日志

## Stage 2: 提取

### 四种子解析器

#### parse_resource()
- 解析 `[command key=value]` 格式
- 提取 `message`/`narration`/`title` 命令的 `text=` 字段
- 提取 `message` 命令的 `name=` 字段（角色名，需查映射表）
- 提取 `choicegroup` 中每个 `choice` 的 `text=` 字段

#### parse_master()
- 解析 gakumasu-diff 中的 YAML 文件
- 遍历每条记录的字符串字段（name、description、title 等）
- 与现有 `masterTrans/*.json` 对比

#### parse_generic()
- 解析 `genericTrans/**/*.json` 的 key-value
- 每个 key-value 对为一条待翻译条目

#### parse_localization()
- 深度遍历 `localization.json` 的所有叶子 string 值

### 对比策略

- 以 `(file, line, field)` 为唯一标识
- 没有现有译文 → `new`
- resource 嵌入的日文原文与服务器值一致 → `existing`
- resource 嵌入的日文原文与服务器值不同 → `changed`
- Master 使用上一次构建成功后记录的源文本快照；快照不同 → `changed`
- 模版有但服务器无 → 忽略（已删除的资源）

## Stage 3: 翻译

### LLM 调用

- OpenAI 兼容 API
- 可配置 base_url / api_key / model
- 批量发送，可配置 batch_size (默认 20) / max_concurrent (默认 5)

### Prompt

```
你是学园偶像大师(Gakuen iDOLM@STER)游戏文本的中文翻译专家。

上下文: 当前翻译的是{category}类型的文本。
{category_context}

要求:
- 保持原文的 \n 换行符
- 角色对话保持口语化、自然
- 专有名词（技能名、道具名、偶像名）参考游戏内现有翻译保持一致
- 不要翻译 {user} 占位符
- 只输出译文，不要任何解释

原文: {jp}
译文:
```

### 角色名映射表

```yaml
character_names:
  amao: 有村麻央
  hski: 花海咲季
  hume: 花海佑芽
  fktn: 藤田琴音
  kllj: 葛城リーリヤ
  hrnm: 姫崎莉波
  shro: 篠澤広
  ssmk: 紫雲清夏
  ttmr: 月村手毬
  kcna: 倉本千奈
  hmsz: 秦谷美鈴
  atbm: 雨夜燕
  jsna: 十王星南
  cmmn: ""                    # 通用/旁白，不显示名字
  {user}: "{user}"            # 玩家名占位符，不翻译
```

## Stage 4: 打包

### 输出目录结构

```
output/GakumasTranslationData/
├── version.txt
├── local-files/
│   ├── resource/*.txt
│   ├── masterTrans/*.json
│   ├── genericTrans/
│   │   ├── default.json
│   │   ├── default.fmt.json
│   │   ├── dafault.split.json
│   │   ├── index/*.json
│   │   └── lyrics/*.json
│   └── localization.json
```

### 重建规则

- `resource/*.txt`: 替换 `text=日文原文` → `text=<r\=日文原文>中文翻译</r\>`（与现有模版格式完全一致，同时保留日文原文和中文翻译）
- `message` 中的 `name=` 字段: 查角色名映射表替换为中文名
- 其他 JSON: 直接替换对应字段的值为中文翻译
- 空译文不会覆盖已有值

## Stage 5: 压缩

将 `output/GakumasTranslationData/` 压缩为 `output/GakumasTranslationData.zip`。ZIP 根目录直接包含 `version.txt` 和 `local-files/`。

## 配置 (config.yaml)

```yaml
llm:
  base_url: ""
  api_key: ""
  model: "gpt-4o-mini"
  batch_size: 20
  max_concurrent: 5

github:
  owner: chinosk6
  repo: GakumasTranslationData

paths:
  server_cache: cache/server
  mod_cache: cache/mod
  gkm_diff: cache/gkm-diff
  output: output
```

## 项目文件结构

```
gkm-tl/
├── config.yaml.example                 # 可提交配置模版
├── run.py                          # Orchestrator
├── stages/
│   ├── __init__.py
│   ├── 01_download.py              # 下载所有原始资源
│   ├── 02_extract.py               # 提取并对比
│   ├── 03_translate.py             # LLM 翻译
│   ├── 04_build.py                 # 打包输出
│   └── 05_package.py               # 压缩发布包
├── lib/
│   ├── __init__.py
│   ├── octo.py                     # Octo API 交互
│   ├── parser_resource.py          # resource TXT 解析器
│   ├── parser_master.py            # master YAML 解析器
│   ├── parser_generic.py           # generic JSON 解析器
│   └── parser_localization.py      # localization 解析器
├── cache/
│   ├── server/                     # 服务器原始资源
│   ├── mod/                        # 现有翻译模版
│   └── gkm-diff/                   # gakumasu-diff repo
└── output/                         # 最终翻译包
```
