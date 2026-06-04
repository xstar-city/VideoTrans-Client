# VideoTrans 客户端使用指南 — 大纲

## 结构说明

主文档讲主要内容，子主题细节放到子文档，链接自然融入正文上下文，不单独设"详细说明"章节。

---

## 一、快速开始

### 一行命令，翻译视频到多种语言

```bash
python video_translate.py 视频1.mp4 视频2.mp4 -t en hi
```

**两个维度的批量处理：**
- **多个视频**：同时翻译多个视频文件
- **多个语种**：一次生成英语（en）、印地语（hi）等多种语言版本

### 示例效果

执行上述命令后，每个视频旁边会生成翻译结果和 `segments/` 中间文件目录。以 `示例项目\逐玉` 为例：

```
逐玉/
├── 1.mp4                    ← 原始视频
├── 1.mp3                    ← 提取的音频
├── 1_translated_en.mp4      ← 英文翻译视频
├── 1_translated_hi.mp4      ← 印地语翻译视频
└── segments/                ← 所有中间文件
    ├── ASR/                 ← 语音识别结果
    ├── English/             ← 英文翻译中间文件
    ├── Hindi/               ← 印地语翻译中间文件
    └── ...
```

> 📖 segments 目录下包含 ASR 识别文本、逐段翻译文本、合成音频等丰富的中间文件，详见 [输出文件结构说明.md](输出文件结构说明.md)

---

## 二、输入要求

### 1. 视频文件组织

**每个视频必须放在独立的文件夹中**，确保翻译中间文件不会互相覆盖。

```
✅ 正确：
短剧/
├── 逐玉/
│   ├── 1/  └── 1.mp4
│   ├── 2/  └── 2.mp4

❌ 错误：
短剧/
├── 逐玉_1.mp4   ← 不同视频混在同一目录
├── 逐玉_2.mp4
```

### 2. 可选的SRT字幕文件

视频旁放同名SRT文件可提高翻译准确性，没有则系统自动生成。

### 3. 支持的语种

- **输入语种（源语言）**：中文、英语及多种中国方言，完整列表 → [Common/src/Common/asr_languages.py](../Common/src/Common/asr_languages.py)
- **输出语种（目标语言）**：支持 600+ 种语言，完整列表 → [Common/src/Common/tts_languages.py](../Common/src/Common/tts_languages.py)

常见输出语种：`en`英语 `ja`日语 `ko`韩语 `hi`印地语 `es`西班牙语 `fr`法语 `de`德语 `ar`阿拉伯语 ...

---

## 三、翻译命令详解

### 1. 视频翻译 (`video_translate.py`)

适用场景：翻译单个或多个视频文件

```bash
# 基本用法
python video_translate.py "视频.mp4" -t en

# 批量：多视频 + 多语种
python video_translate.py "视频1.mp4" "视频2.mp4" -t en hi ja

# 指定服务端地址
python video_translate.py "视频.mp4" -t en --server http://<ServerIP>:8000
```

### 2. 音频翻译 (`audio_translate.py`)

适用场景：仅翻译音频文件，不涉及视频处理

```bash
python audio_translate.py "音频.mp3" -t en
python audio_translate.py "音频1.mp3" "音频2.mp3" -t en hi
```

### 3. 批量短剧翻译 (`short_drama_translate.py`)

适用场景：翻译整部短剧（含多个视频片段）

```bash
python short_drama_translate.py "E:\短剧\《逐玉》" -t en --server http://<ServerIP>:8000
```

脚本自动：整理视频到标准目录 → 转换非mp4格式 → 跳过已翻译视频 → 按语种分组批量处理。

> 💡 批量翻译任务可能运行数小时，中途退出后重新执行同样命令即可续跑，已完成的步骤会自动跳过。详见 [批量翻译断点续跑说明.md](批量翻译断点续跑说明.md)

---

## 四、基本模式 vs 高级模式

### 基本模式（默认）

```bash
python video_translate.py "视频.mp4" -t en
```

默认设置：人声分离启用 / 激进降噪 / 精确说话人切分 / TTS时长感知翻译 / 自动选择翻译模型

### 高级模式

```bash
python video_translate.py "视频.mp4" -t en \
  --no-separate \                         # 跳过人声分离
  --denoise normal \                      # 标准降噪
  --asr-mode basic \                      # 基本ASR模式
  --translation-mode independent \        # 独立翻译模式
  --translation-models "deepseek-v4-pro"  # 指定翻译模型
```

常用高级参数表：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--no-separate` | 跳过人声分离 | 否 |
| `--denoise` | 降噪（none/normal/aggressive） | aggressive |
| `--asr-mode` | ASR模式（basic/precise） | precise |
| `--translation-mode` | 翻译模式（independent/tts_aware） | tts_aware |
| `--translation-models` | 翻译模型（逗号分隔） | 自动选择 |
| `--extra-translation-guideline` | 额外翻译指南文件 | 无 |

---

## 五、依赖安装

- Python 环境 + pip install
- FFmpeg（必需外部工具）
- Common 模块（`pip install -e .`），详见 [Common/README.md](../Common/README.md)

---

## 六、常见问题

- 服务端连接 / 文件权限 / 磁盘空间 / FFmpeg 版本等