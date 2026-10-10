# VideoTrX

视频字幕翻译工具，PySide6 桌面界面。输入一个视频链接，产出中英双语 ASS 字幕，需要时再把字幕烧进视频。

## 简介

流程：

1. **下载**：yt-dlp 同时下载最佳音频和最佳视频。
2. **转写**：MAI-Transcribe-2 生成词级时间戳，再用 ffmpeg 静音检测，把每段发声的首词起点、末词终点贴到实测的发声边缘。
3. **英文校正**：LLM 只替换明显的拼写或识别错误（含人名、品牌、产品名），不增删词。
4. **断句**：spaCy 按标点和停顿断句，修复被切开的专有名词；超长句再由 LLM 按语义拆分。
5. **摘要与术语**：LLM 概括主题并提取术语表，可叠加自定义术语。
6. **翻译**：分块逐行初译。
7. **字幕切分**：按英文字符数和中文显示宽度，把超长的字幕拆成多条；中文超宽时先压缩，压不下去再拆。
8. **全文润色**：按字幕行对全文做一次统一润色。
9. **生成字幕**：输出双语 ASS 字幕 `output/src_trans.ass`。
10. **按需烧录**：把字幕烧进视频，自动选择可用的编码器，优先硬件编码。

## 支持平台

- Windows x64
- Linux x86_64 / aarch64（需要桌面环境）

## 安装

### 一键安装

- **Windows**：双击 `install.bat`。
- **Linux**：在项目目录运行 `bash install.sh`。

安装脚本会依次：

1. 下载 uv（Python 包管理工具）。
2. 用 uv 安装 Python 3.12，并创建虚拟环境 `.venv`。
3. 安装 `requirements.txt` 中的依赖，并把 yt-dlp 升级到最新版。
4. 安装 spaCy 英文模型 `en_core_web_md`。
5. 下载 ffmpeg、ffprobe 和 deno，放进 `bin/`。
6. `config.yaml` 不存在时，从 `config.example.yaml` 复制一份。

所有东西都装在项目目录内（`tools/`、`.venv/`、`bin/`），删除整个目录即卸载；uv 的下载缓存在系统缓存目录中，可以在删除前运行 `tools/uv/uv cache clean` 清掉。脚本可以重复运行：已安装的部分会跳过，yt-dlp 每次都会升级到最新版。

> [!IMPORTANT]
> yt-dlp 下载 YouTube 需要 JavaScript 运行时 **deno**。一键安装脚本已经把它装到 `bin/`；如果手动安装，必须自己安装 deno，并放进 `bin/` 或 PATH。

### 手动安装

需要 Python 3.12。在项目目录中执行（Windows 上用 `py -3.12` 代替 `python3.12`，把 `.venv/bin/` 换成 `.venv\Scripts\`，`cp` 换成 `copy`）：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.7.1/en_core_web_md-3.7.1-py3-none-any.whl
cp config.example.yaml config.yaml
```

然后把 ffmpeg、ffprobe、deno 放进 `bin/`，或者确保它们在 PATH 里。程序优先使用 `bin/` 下的版本。

### Linux 系统库

Qt 界面需要一些系统库。在 Debian / Ubuntu 上通常只缺 `libxcb-cursor0`：

```bash
sudo apt install libxcb-cursor0
```

### 启动

- **Windows**：双击 `start.bat`。
- **Linux**：`bash start.sh`。

## 配置

所有配置都在项目根目录的 `config.yaml` 中。一键安装会自动生成；手动安装时从 `config.example.yaml` 复制一份。

必须填写主 LLM 的 `api.key`、`api.base_url`、`api.model`，以及转写用的 `mai.key`、`mai.endpoint`。全文润色想换一个模型时，再填 `polish.api` 下的 `key`、`base_url`、`model` 等项；留空的项沿用 `api`。

### 逐项说明

| 键 | 默认值 | 说明 |
|---|---|---|
| `source_language` | `auto` | 源语言。`auto` 使用转写时检测到的语言；也可写语言代码，如 `en` |
| `target_language` | `简体中文` | 译文语言 |
| `max_workers` | `3` | 并发线程数，实体修复、语义断句、分块翻译、字幕切分都用这里 |
| `api.key` | | 主 LLM 的 API key |
| `api.base_url` | `https://api.openai.com/v1` | 主 LLM 的接口地址 |
| `api.model` | | 模型名 |
| `api.protocol` | `chat_completions` | 调用协议：`chat_completions` / `responses` / `messages`，见下文 |
| `api.llm_support_json` | `true` | 模型支持 JSON Schema 结构化输出时为 `true` |
| `api.request_timeout_sec` | `300` | 单次请求超时（秒） |
| `api.request_retries` | `5` | 请求失败后的重试次数 |
| `api.request_retry_delay_sec` | `1` | 重试间隔（秒） |
| `api.reasoning_effort` | 空 | 思考等级。留空不传；写什么就原样传什么，如 `low` / `medium` / `high` |
| `polish.api.*` | | 全文润色使用的 LLM，各项含义同 `api.*`。留空的项继承 `api`；`reasoning_effort` 只看本块，不继承 |
| `mai.key` | | MAI-Transcribe-2 的 key |
| `mai.endpoint` | | MAI 所在的 Azure 资源地址，形如 `https://<资源名>.services.ai.azure.com` |
| `mai.model` | `MAI-Transcribe-2` | 转写模型 |
| `mai.snap_lead_ms` | `20` | 贴边：首词起点提前量（毫秒） |
| `mai.snap_tail_ms` | `10` | 贴边：末词终点延后量（毫秒） |
| `mai.snap_max_adjust_ms` | `500` | 贴边：单次最大偏移（毫秒），超过则保留 API 原值 |
| `mai.silence_threshold_db` | `-20` | 静音检测：比这更安静的算静音（dB） |
| `mai.silence_min_dur` | `0.1` | 静音检测：短于此的静音不计（秒） |
| `download.proxy` | 空 | yt-dlp 使用的代理；留空不用代理 |
| `download.cookies_from_browser` | 空 | 让 yt-dlp 从浏览器读取 cookies；留空不读 |
| `english_correction.enabled` | `true` | 是否做英文转写校正 |
| `english_correction.only_when_english` | `true` | 只在源语言为英语时校正 |
| `entity_repair.enabled` | `true` | 是否修复被断句切开的专有名词 |
| `entity_repair.only_when_space_joiner` | `true` | 只对以空格分词的语言（如英语）做修复 |
| `entity_repair.max_pairs_per_request` | `120` | 每次请求检查的句子边界数 |
| `entity_repair.boundary_window_words` | `8` | 每个边界两侧各给模型看多少个词 |
| `entity_repair.max_fragment_words` | `4` | 每侧最多挪动的词数 |
| `entity_repair.score_base_words` | `20` | 修复方向评分的词数基准 |
| `nlp_split.pause_threshold_sec` | `1.5` | 词间停顿超过此值（秒）时强制断句 |
| `terminology.source_chars` | `8000` | 摘要与术语提取读取的原文字符数上限 |
| `subtitle.source_max_length` | `90` | 英文一行的字符上限，包含空格、标点 |
| `subtitle.target_max_length` | `25` | 中文一行的宽度上限，单位为汉字宽度 |
| `subtitle.style.chinese_font` | `Aa研宋` | 中文字体；中文宽度表按此字体测得 |
| `subtitle.style.english_font` | `Oswald` | 英文字体 |
| `subtitle.style.font_size` | `70` | 字号（ASS 画布为 1920×1080） |

`english_correction` 和 `entity_repair` 也可以各自加一个与 `polish.api` 格式相同的 `api` 子块，单独指定模型；不加则使用 `api`。

### LLM 协议

- `chat_completions`：OpenAI 兼容的 Chat Completions 接口，适用于大多数中转站和兼容服务。
- `responses`：OpenAI Responses 接口。
- `messages`：Anthropic Messages 接口（Claude）。`base_url` 写到域名或 `/v1` 都可以，程序会补全 `/v1/messages`。

### MAI 转写

`mai.key` 和 `mai.endpoint` 在 Azure 门户中该资源的「密钥和终结点」页面获取。endpoint 只写到域名，例如 `https://my-speech.services.ai.azure.com`。

### 下载代理与 cookies

`download.proxy` 填完整的代理地址，例如：

```yaml
download:
  proxy: 'http://127.0.0.1:7890'
```

`socks5://127.0.0.1:1080` 这类 SOCKS 代理也可以。

YouTube 要求登录验证时，用 `download.cookies_from_browser` 让 yt-dlp 读取浏览器的登录状态。值的格式与 yt-dlp 的 `--cookies-from-browser` 相同：

```yaml
download:
  cookies_from_browser: 'firefox'
```

- 写浏览器名即可使用该浏览器的默认配置：`firefox`、`chrome`、`edge` 等。
- 使用指定的配置目录：`firefox:<配置目录路径>`。Windows 路径用单引号包起来，反斜杠不用转义，例如 `'firefox:C:\Users\你的用户名\AppData\Roaming\Mozilla\Firefox\Profiles\xxxx.default-release'`。
- Windows 上 Chrome、Edge 的 cookies 经常因加密或文件被占用而读取失败，推荐用 Firefox。

## 使用

### 按钮

| 按钮 | 作用 |
|---|---|
| 开始 | 清空 `output/`（旧的 `.ass` 先归档到 `archive/`），同时下载最佳音频和最佳视频；音频下载完成后自动运行字幕流程 |
| 继续任务 | 不下载，直接从检查点接着运行字幕流程 |
| 下载最佳视频 | 只下载视频，例如开始时视频下载失败 |
| 停止当前操作 | 停止下载、字幕流程和烧录 |
| 执行烧录 | 把 `output/src_trans.ass` 烧进最佳视频，在 `output/` 中生成 `<视频名>_with_subtitles.mp4` |
| 打开 output 文件夹 | 用系统文件管理器打开 `output/` |

### 继续任务与检查点

字幕流程的每个阶段完成后都会写出一个文件。「继续任务」从最后一个已存在的输出之后接着运行，不会重复已完成的阶段。

想从某个阶段重新运行，就删掉该阶段及其后各阶段的输出文件，再点「继续任务」。例如删掉 `translation.json`、`subtitle_lines.json`、`polished_lines.json` 和 `output/src_trans.ass`，就会从「翻译」重跑。

| 阶段 | 输出文件 |
|---|---|
| 转写 | `output/log/transcript.json` |
| 英文校正 | `output/log/english_correction_done.json` |
| NLP 断句 | `output/log/split_by_nlp.json` |
| 语义断句 | `output/log/split_by_meaning.json` |
| 摘要与术语 | `output/log/terminology.json` |
| 翻译 | `output/log/translation.json` |
| 字幕切分 | `output/log/subtitle_lines.json` |
| 全文润色 | `output/log/polished_lines.json` |
| 生成字幕 | `output/src_trans.ass` |

转写需要音频：从「开始」进入时使用刚下载的音频；从「继续任务」进入时使用 `output/` 中最新的 `*_audio.*` 文件。

### output/log 中间文件

| 文件 | 内容 |
|---|---|
| `transcript.json` | 转写结果：检测到的语言和词级时间戳；英文校正会改写其中的词 |
| `transcript_before_english_correction.json` | 英文校正前的转写备份，有校正建议时生成 |
| `english_correction_done.json` | 英文校正完成标记 |
| `english_correction_changelog.jsonl` | 英文校正的逐条记录 |
| `split_by_nlp.json` | NLP 断句结果 |
| `split_by_nlp_before_entity_repair.json` | 实体修复前的断句，有修复建议时生成 |
| `entity_repair_changelog.jsonl` | 实体修复的逐条记录 |
| `split_by_meaning.json` | 语义断句结果 |
| `terminology.json` | 主题摘要和术语表 |
| `translation.json` | 逐句原文与初译 |
| `subtitle_lines.json` | 切分后的字幕行 |
| `subtitle_split_decisions.json` | 字幕切分的决策记录，包含压缩和拆分的记录 |
| `polish_changelog.jsonl` | 全文润色的逐行记录 |
| `polish_progress.json`、`polish_progress.txt`、`polish_stream.txt` | 全文润色的断点续传进度、预览和流式快照，润色完成后删除 |
| `polished_lines.json` | 润色后的最终字幕行 |

`output/gpt_log/` 中保存每次 LLM 请求的提示词和回复。除全文润色外，提示词和模型都相同的请求会直接复用已保存的回复，不再重复调用。

### 自定义术语

在项目根目录的 `custom_terms.json` 中添加术语，摘要与术语阶段会把它们加入术语表，并让模型不再重复提取：

```json
[
  {"src": "Machine Learning", "tgt": "机器学习", "note": "AI 的核心技术"}
]
```

`note` 可以省略。

### 烧录编码器

执行烧录时依次检测 NVIDIA（`hevc_nvenc`）→ AMD（`hevc_amf`）→ CPU（`libx265`），使用第一个可用的编码器，并在日志中显示「使用编码器：<名称>」。检测结果在程序运行期间缓存。

## 常见问题

### 下载失败

- 检查 `download.proxy` 是否填对、代理是否可用。
- 提示需要登录或确认不是机器人时，设置 `download.cookies_from_browser`。
- YouTube 经常调整接口，重新运行安装脚本即可把 yt-dlp 更新到最新版。

### 安装时下载很慢

- 设置 `HTTPS_PROXY` 环境变量后再运行安装脚本，例如 `HTTPS_PROXY=http://127.0.0.1:7890 bash install.sh`。Windows 上先在命令提示符中执行 `set HTTPS_PROXY=http://127.0.0.1:7890`，再在同一窗口运行 `install.bat`，Python 和依赖包的下载会走这个代理；uv、ffmpeg、deno 由 PowerShell 下载，走系统代理设置。
- Python 包可以改用国内镜像：设置 `UV_DEFAULT_INDEX`，例如 `UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple bash install.sh`。

### Linux 下启动报 Qt xcb 错误

报错类似 `Could not load the Qt platform plugin "xcb"` 时，先安装 `libxcb-cursor0`。仍然报错的话，设置 `QT_DEBUG_PLUGINS=1` 再启动，日志会列出缺少的库。

### 其他语言的 spaCy 模型

安装脚本只装英文模型。源语言是其他语言时，首次运行会自动下载对应的 spaCy 模型，需要能访问网络。

## 致谢

感谢 [VideoLingo](https://github.com/Huanshere/VideoLingo) 带来的启发。本项目最初在 VideoLingo（Apache-2.0）的基础上修改而来，现已基本重写：修正了原有流程中一些不太合适的处理，并换用了更现代的技术方案。

## 许可证

[Apache-2.0](LICENSE)
