![封面](resourses/封面.jpg)

# VideoTrans 客户端

一行命令，将视频翻译成上百种语言。

```bash
python video_translate.py 视频1.mp4 视频2.mp4 -t en hi --server http://<ServerIP>:8000
```

**两个维度的批量处理：**
- **多个视频**：同时指定多个视频文件
- **多个语种**：一次生成英语（en）、印地语（hi）等多种语言版本

---

## 快速开始

### 1. 翻译视频

```bash
# 单个视频 → 英语
python video_translate.py "1.mp4" -t en --server http://<ServerIP>:8000

# 多个视频 + 多个语种
python video_translate.py "1.mp4" "2.mp4" -t en hi ja --server http://<ServerIP>:8000
```

### 2. 翻译音频

```bash
python audio_translate.py "1.mp3" -t en --server http://<ServerIP>:8000
```

### 3. 批量短剧翻译

```bash
python short_drama_translate.py "E:\短剧\《逐玉》" -t en --server http://<ServerIP>:8000
```

脚本自动：整理视频到标准目录 → 转换非 mp4 格式 → 跳过已翻译视频 → 按语种分组批量处理。

---

## 示例效果

以 `示例项目/逐玉` 为例，执行翻译命令后，每个视频旁边会生成翻译结果和中间文件：

```
逐玉/
├── 1.mp4                       ← 原始视频
├── 1.mp3                       ← 提取的音频
├── 1_vocals_denoised.mp3       ← 分离出的人声（经降噪）
├── 1_others_denoised.mp3       ← 分离出的背景音（经降噪）
├── 1_translated_en.mp4         ← 英文翻译视频
└── segments/                   ← 所有中间文件
    ├── ASR/                    ← 语音识别结果
    ├── English/                ← 英文翻译中间文件
    │   ├── 0.000.txt          ← 逐段翻译文本
    │   ├── 0.000.mp3          ← 逐段合成音频
    │   ├── combined.mp3       ← 合并后的完整人声
    │   └── final.mp3          ← 最终输出音轨
    └── _tts_refs/              ← TTS 参考音频
```

`segments/` 目录下包含 ASR 识别文本、逐段翻译文本、合成音频等丰富的中间文件，可用于校对和二次编辑。完整结构说明见 [输出文件结构说明.md](输出文件结构说明.md)。

---
![成本优势](resourses/成本优势.jpg)

## 架构设计

VideoTrans 采用 **客户端-服务端** 分离架构：

- **客户端**：纯命令行脚本，只负责发送请求和本地 ffmpeg 操作，零业务逻辑
- **服务端**：承载全部 AI 推理（ASR、翻译、TTS、人声分离等），部署在 GPU 机器上

一个客户端可以通过 `--server` 参数指定不同的服务端地址，理论上**支持无上限的横向扩展**——多台 GPU 机器各运行一个服务端，客户端按需分发任务即可。

> ⚠️ 由于 GPU 资源有限，**一个服务端同时只能运行一个客户端提交的任务**，不支持并行。如果需要同时处理多个任务，请部署多个服务端节点，分别用不同的 `--server` 地址提交。

### 服务端监控面板

服务端启动后，浏览器访问 `http://<ServerIP>:8000/` 即可打开任务监控面板，查看当前正在运行的任务、历史任务状态等。

### 服务端 API 文档

服务端基于 FastAPI 构建，访问 `http://<ServerIP>:8000/docs` 可查看完整的 REST API 文档（Swagger UI），包括任务提交、取消、文件上传/下载等接口。

### 成本优势

单部短剧的翻译成本约为竞品的 **十分之一**，核心原因是极致的模型压缩：

- 超过 200G 的十几个模型，压缩到一个可扩展的服务端节点上
- 单个节点只需 **24G 显存**的 4090 或 3090 即可运行
- 硬件成本低，翻译成本自然低

### 批量调度与断点续跑

极致压缩带来了复杂的模型调度需求。模型加载/卸载耗时可观，为了减少频繁切换，服务端会尽量把**所有视频、所有语言**的相同阶段（如同一种 ASR 模型、同一种 TTS 模型）集中处理完毕，再切换到下一个模型。这正是前面提到的"两个批量维度"（多视频 × 多语言）的设计初衷。

这种调度策略意味着任务运行顺序可能与用户直觉不同，中途退出后无法简单从断点顺序接续，因此**断点续跑**功能尤为关键——详见 [批量翻译断点续跑说明.md](批量翻译断点续跑说明.md)。

---

## 输入要求

### 1. 每个视频一个独立文件夹

确保翻译中间文件不会互相覆盖：

```
✅ 正确：
短剧/逐玉/
├── 1/  └── 1.mp4
├── 2/  └── 2.mp4

❌ 错误：
短剧/
├── 逐玉_1.mp4   ← 不同视频混在同一目录
├── 逐玉_2.mp4
```

> 💡 使用 `short_drama_translate.py` 时会自动整理，无需手动操作。也可运行 `Tools/organize_videos_into_folders.py` 单独整理。

### 2. 同名 SRT 字幕文件（可选但建议）

在视频旁放同名 SRT 文件，可提高翻译准确性。没有则系统自动生成。

```
1/
├── 1.mp4    ← 视频
└── 1.srt    ← 同名字幕（可选）
```

> 💡 可运行 `Tools/copy_matching_srt_to_video_folders.py` 批量复制 SRT 到对应视频目录。

> 💡 VideoTrans 会综合 ASR 和 SRT 两种来源进行交叉校准，比单一来源更准确。详见 [字幕ASR综合校准说明.md](字幕ASR综合校准说明.md)。

### 3. 支持的语种

**输入语种（源语言）**：中文、英语及多种中国方言

| 代码 | 语言 | | 代码 | 语言 |
|------|------|-|------|------|
| `zh` | 中文 | | `en` | English |
| `cantonese` | 粤语 | | `minnan` | 闽南语 |
| `sichuan` | 四川话 | | `shanghai` | 上海话 |
| `dongbei` | 东北话 | | `wu` | 吴语 |
| `henan` | 河南话 | | `shaanxi` | 陕西话 |

> 完整列表见 [asr_languages.py](https://github.com/xstar-city/VideoTrans-Common/blob/main/src/Common/asr_languages.py)

**输出语种（目标语言）**：支持 600+ 种语言

| 代码 | 语言 | | 代码 | 语言 |
|------|------|-|------|------|
| `en` | English | | `hi` | Hindi |
| `ja` | Japanese | | `ko` | Korean |
| `es` | Spanish | | `fr` | French |
| `de` | German | | `ar` | Arabic |
| `pt` | Portuguese | | `ru` | Russian |
| `it` | Italian | | `th` | Thai |
| `vi` | Vietnamese | | `id` | Indonesian |

> 完整列表见 [tts_languages.py](https://cnb.cool/xstar.city/VideoTrans-Common/-/blob/main/src/Common/tts_languages.py)

---

## 翻译命令详解

### 视频翻译

提供两种模式：**基本模式**（快速）和**高级模式**（精确）。

#### 基本模式 `video_translate_basic.py`

适合大多数场景，预置最优参数，命令更简洁：

```bash
python video_translate_basic.py "1.mp4" -t en --server http://<ServerIP>:8000
```

预置设置：
- ASR 模式：basic（ASR 自带说话人切分）
- 翻译模式：independent（纯文本翻译）
- 音频变速：禁用（保持原速）

#### 高级模式 `video_translate.py`

支持精细控制，默认启用 TTS 时长感知翻译和精确说话人切分：

```bash
# 默认高级参数
python video_translate.py "1.mp4" -t en --server http://<ServerIP>:8000

# 自定义参数
python video_translate.py "1.mp4" -t en --server http://<ServerIP>:8000 \
  --no-separate \
  --denoise normal \
  --asr-mode basic \
  --translation-mode independent \
  --translation-models "deepseek-v4-pro"
```

**常用高级参数：**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--no-separate` | 跳过人声分离 | 否（默认启用） |
| `--denoise` | 降噪级别：`none` / `normal` / `aggressive` | `aggressive` |
| `--asr-mode` | ASR 模式：`basic` / `precise`。`precise` 会执行二次说话人切分，生成校准日志（详见[二次说话人切分校准日志说明](二次说话人切分校准日志说明.md)） | `precise` |
| `--translation-mode` | 翻译模式：`independent` / `tts_aware` | `tts_aware` |
| `--translation-models` | 翻译模型（逗号分隔） | 自动选择 |
| `--extra-translation-guideline` | 额外翻译指南文件路径 | 无 |
| `--tts-aware-max-retries` | TTS 时长调整重试次数 | 3 |
| `--max-audio-slowdown-pct` | TTS 音频最大减速比例 | 0.1 |
| `--max-audio-speedup-pct` | TTS 音频最大加速比例 | 0.2 |
| `--max-video-slowdown-pct` | 视频片段最大减速比例 | 0.1 |
| `--max-video-speedup-pct` | 视频片段最大加速比例 | 0.2 |

### 音频翻译 `audio_translate.py`

仅翻译音频文件，不涉及视频处理：

```bash
python audio_translate.py "1.mp3" -t en --server http://<ServerIP>:8000
python audio_translate.py "1.mp3" "2.mp3" -t en hi --server http://<ServerIP>:8000
```

### 批量短剧翻译 `short_drama_translate.py`

翻译整部短剧（含多个视频片段）：

```bash
python short_drama_translate.py "E:\短剧\《逐玉》" -t en --server http://<ServerIP>:8000
```

批量翻译任务可能运行数小时，中途退出后重新执行同样命令即可续跑——已完成的步骤会自动跳过。详见 [批量翻译断点续跑说明.md](批量翻译断点续跑说明.md)。

---

## 依赖安装

### 1. Python 环境

```bash
pip install requests
```

### 2. FFmpeg

FFmpeg 是必需的外部工具，需在 PATH 中可用：

```bash
# 验证安装
ffmpeg -version
ffprobe -version
```

### 3. Common 模块

```bash
git clone --recurse-submodules https://cnb.cool/xstar.city/VideoTrans-Client.git
cd VideoTrans-Client
cd Common && pip install -e . && cd ..
```

详见 [Common/README.md](https://github.com/xstar-city/VideoTrans-Common/blob/main/README.md)

---

## 常见问题

**Q: 翻译中断了怎么办？**  
A: 重新运行同样的命令即可续跑。已完成的步骤（ASR、翻译、TTS 等）会自动跳过。详见 [批量翻译断点续跑说明.md](批量翻译断点续跑说明.md)。

**Q: 如何强制从零开始？**  
A: 删除视频目录下的 `.vt_task_id` 文件，或运行时加 `--new-task` 参数。

**Q: 视频不在独立目录怎么办？**  
A: 运行 `Tools/organize_videos_into_folders.py` 自动整理，或使用 `short_drama_translate.py` 会自动整理。

**Q: 支持哪些视频格式？**  
A: mp4、mov、mkv、avi、webm、m4v。非 mp4 格式会自动转换。

**Q: 服务端连接不上？**  
A: 检查 `--server` 参数是否正确，确认服务端已启动且端口可达。
