"""编辑重跑模式：检测本地对 segments/ 的编辑并同步到服务端。

启动后，客户端逐一对比本地与服务端的文件内容/存在性，检测以下编辑后自动处理：

术语：
  - ASR 文本：segments/ASR/{stem}.txt（原声音频的语音识别结果）
    格式：第一行=ASR 文本，第二行=音频时长秒数（三位小数）
  - 翻译文本：segments/{lang}/{stem}.txt（ASR 文本翻译到目标语言后的文本）
  - 原声音频 mp3：segments/{stem}.mp3（按句切分的原始音频片段，本场景不涉及编辑）
  - 合成音频 mp3：segments/{lang}/{stem}.mp3（基于翻译文本 TTS 合成的目标语言音频）
  - 翻译候选 md：segments/{lang}/{stem}.md（翻译过程中保存的候选/调试信息）

| 场景            | 操作方式                              | 客户端检测                          | 自动执行                                                              |
| --------------- | ------------------------------------ | ----------------------------------- | -------------------------------------------------------------------- |
| 改 ASR 文本     | 编辑 segments/ASR/{stem}.txt          | 下载服务端 ASR 文本逐字对比，内容不一致 | 上传新 ASR 文本；删除所有语言目录下同 stem 的 翻译文本 + 合成音频 mp3 + 翻译候选 md + 候选目录 |
|                  |                                      |                                     | 若时长行（第二行）也变更：额外删除 segments/{stem}.mp3 + 各语言目录下 {stem}.mp3 + md + 候选目录，服务端重新切分 |
| 新增 ASR 文本   | 在 segments/ASR/ 下新建 {stem}.txt    | 客户端有但服务端没有                 | 校验两行格式 + 时长 > 0.3s → 上传 txt；服务端自动从人声音频切分 mp3   |
| 改翻译文本      | 编辑 segments/{lang}/{stem}.txt       | 下载服务端翻译文本逐字对比，内容不一致   | 上传新翻译文本；删除该语言目录下同 stem 的 合成音频 mp3 + 翻译候选 md + 候选目录  |
| 替换合成音频    | 用候选/外部音频替换 segments/{lang}/{stem}.mp3 | 对比本地与服务端文件大小，大小不一致 | 上传新 MP3；删除 combined.mp3 + final.mp3 触发重新合成                    |
| 删语种         | 删除本地语言目录（如 English/）         | 本地目录不存在                       | 删除服务端对应语言目录                                                  |
| 删某句合成音频  | 删除 segments/{lang}/{stem}.mp3       | 本地合成音频 mp3 缺失                | 删除服务端对应合成音频 mp3 + 翻译候选 md + 候选目录                      |
| 删某句翻译文本  | 删除 segments/{lang}/{stem}.txt       | 本地翻译文本缺失                     | 删除服务端对应翻译文本 + 合成音频 mp3 + 翻译候选 md + 候选目录              |
| 改翻译字幕    | 编辑 segments/{lang}/full_translation.srt | 下载服务端 SRT 对比，内容不一致       | 上传 SRT；解析 SRT 将文本写回对应 txt 并上传；删除对应 TTS 产物 + combined/final；忽略 txt 的独立修改 |
| 改翻译指南    | 编辑 segments/{lang}/translation_guidelines.txt | 下载服务端指南对比，内容不一致    | 上传新指南；删除该语言目录下所有翻译 txt + TTS 产物 + combined/final，强制重新翻译 |

处理完成后，服务端跳过整个 ASR 流程（人声分离、语音识别、残差合并），直接从翻译步骤开始，
仅重跑受影响的部分。

新增/修改 ASR 文本时，服务端会在翻译前执行「音频修复」步骤：
扫描 ASR txt 对应的 mp3，缺失则从人声音频切分（含响度兜底），并更新 final-asr-result.json。
时长变更的检测和旧 mp3 删除由客户端负责（对比本地与服务端 txt 的第二行时长，
服务端旧 txt 可能没有第二行，此时只要客户端有时长行就视为变更）。
"""

from __future__ import annotations

from datetime import datetime

import difflib
import hashlib
import re
import shutil
import sys
import time
from pathlib import Path

import requests

from Common.config import (
    SEGMENTS_DIRNAME,
    ASR_DIRNAME,
    ASR_FULL_TEXT_FILENAME,
    ASR_SENTENCE_RECONCILE_FILENAME,
    SECONDARY_DIARIZATION_CALIBRATE_LOG_FILENAME,
    COMBINED_AUDIO_FILENAME,
    FINAL_AUDIO_FILENAME,
    TRANSLATION_GUIDELINES_FILENAME,
    build_segments_dir,
)
from Common.language_map import get_language_dir_name, normalize_target_language_codes

from remote_client import RemoteScriptClient


def _log(msg: str):
    """带时间戳的日志输出，用于关键节点。"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {msg}", flush=True)


# 服务端 segments/ASR/ 下的非 txt 文件（full_text.md 等），对比时跳过
_ASR_NON_TXT_FILES = frozenset({
    ASR_FULL_TEXT_FILENAME,  # full_text.md
    ASR_SENTENCE_RECONCILE_FILENAME,
    SECONDARY_DIARIZATION_CALIBRATE_LOG_FILENAME,
})

# 完整翻译字幕文件名（由 stop_after_translation 模式生成）
FULL_TRANSLATION_SRT_FILENAME = 'full_translation.srt'

# SRT 时间戳正则：00:00:01,234 --> 00:00:03,456
_SRT_TIMESTAMP_RE = re.compile(
    r'(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})'
)


def _parse_srt_to_segments(srt_content: str) -> list[tuple[float, str]]:
    """解析 SRT 文件内容，返回 [(start_s, text), ...] 列表。

    SRT 格式：
        1
        00:00:00,000 --> 00:00:02,500
        翻译文本

    start_s 用于匹配 txt 文件名 stem（float(stem) ≈ start_s）。
    文本可能跨多行，用换行符拼接。
    """
    segments: list[tuple[float, str]] = []
    blocks = srt_content.strip().split('\n\n')
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        # 第二行是时间戳行
        match = _SRT_TIMESTAMP_RE.search(lines[1])
        if not match:
            continue
        h, m, s, ms = (int(x) for x in match.groups()[:4])
        start_s = h * 3600 + m * 60 + s + ms / 1000.0
        # 第三行开始是文本（可能多行）
        text = '\n'.join(lines[2:]).strip()
        segments.append((start_s, text))
    return segments


def _match_stem_by_start_s(
    start_s: float,
    existing_stems: dict[str, str],
    tolerance: float = 0.3,
) -> str | None:
    """根据 SRT 开始时间匹配最近的 txt 文件 stem。

    在 tolerance 容差范围内寻找时间差最小的 stem。

    Args:
        start_s: SRT 解析出的开始时间（秒）
        existing_stems: {stem_str: stem_str} 字典（key 和 value 相同，方便查找）
        tolerance: 允许的时间误差（秒），默认 0.3s

    Returns:
        匹配的 stem 字符串，未匹配返回 None
    """
    best_stem: str | None = None
    best_diff: float = tolerance  # 初始化为容差上限
    for stem in existing_stems:
        try:
            diff = abs(float(stem) - start_s)
        except ValueError:
            continue
        if diff <= best_diff:
            best_diff = diff
            best_stem = stem
    return best_stem


def _compute_file_hash(path: Path, chunk_size: int = 65536) -> str:
    """计算文件的 MD5 哈希值，用于内容对比。"""
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()


def _delete_local_tts_artifacts(local_lang_dir: Path, stem: str):
    """删除客户端本地语言目录下指定 stem 的 TTS 产物（mp3 + md + 候选目录）。

    用于编辑重跑时同步清理客户端本地文件，避免后续同步混乱。
    """
    for ext in ('.mp3', '.md'):
        f = local_lang_dir / f"{stem}{ext}"
        if f.exists():
            f.unlink()
    candidate_dir = local_lang_dir / stem
    if candidate_dir.is_dir():
        shutil.rmtree(candidate_dir, ignore_errors=True)


def _check_server_time(client: RemoteScriptClient):
    """检查客户端与服务端系统时间是否一致，差异过大时打印警告。"""
    try:
        result = client.get_server_time()
    except Exception as e:
        print(f"[警告] 无法获取服务端时间: {e}")
        return

    server_time = result.get("server_time", 0)
    local_time = time.time()
    offset = server_time - local_time
    abs_offset = abs(offset)

    if abs_offset > 60:
        print(f"[强烈警告] 客户端与服务端时间差异 {abs_offset:.1f}s "
              f"(服务端 {'快' if offset > 0 else '慢'} {abs_offset:.1f}s)！")
        print(f"  服务端时区: {result.get('timezone', '?')}")
        print("  时间差异过大会影响文件同步和缓存判断，建议同步系统时间后重试。")
    elif abs_offset > 5:
        print(f"[警告] 客户端与服务端时间差异 {abs_offset:.1f}s "
              f"(服务端 {'快' if offset > 0 else '慢'} {abs_offset:.1f}s)")
    else:
        print(f"时间同步检查通过（差异 {abs_offset:.1f}s）")


def _download_server_txt(client: RemoteScriptClient, task_id: str,
                         remote_path: str) -> str | None:
    """下载服务端 txt 文件内容到内存，返回文本内容。失败返回 None。"""
    try:
        resp = requests.get(
            f"{client.base_url}/download/{task_id}/{remote_path}",
            headers=client._headers(),
            timeout=client.timeout,
            stream=True,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        # 服务端 txt 均为 UTF-8 编码，requests 默认用 ISO-8859-1 解码会导致中文乱码
        resp.encoding = 'utf-8'
        return resp.text
    except Exception:
        return None


def _print_txt_diff(local_content: str, server_content: str, label: str):
    """打印客户端与服务端文本的 unified diff。

    Args:
        local_content: 客户端文件内容
        server_content: 服务端文件内容
        label: 文件标识（如 "ASR/62.300.txt"），用于 diff 头部显示
    """
    local_lines = local_content.splitlines()
    server_lines = server_content.splitlines()
    diff = list(difflib.unified_diff(
        server_lines, local_lines,
        fromfile=f"服务端/{label}",
        tofile=f"客户端/{label}",
        lineterm="",
    ))
    if diff:
        for line in diff:
            print(f"    {line}")


def _parse_duration_line(content: str) -> float | None:
    """从 ASR txt 内容中解析第二行的音频时长（秒）。

    txt 格式：第一行=ASR 文本，第二行=音频时长秒数（三位小数）。
    旧格式 txt（仅一行文本）返回 None。
    """
    lines = content.splitlines()
    if len(lines) < 2:
        return None
    try:
        return float(lines[1].strip())
    except (ValueError, IndexError):
        return None


def _validate_asr_txt_for_new_segment(txt_path: Path) -> tuple[str, float] | None:
    """校验新增 ASR txt 文件是否符合要求。

    要求：
    - 必须两行（第一行=文本，第二行=时长秒数）
    - 第二行能解析为 float 且 > 0.3
    - 文件名（stem）能解析为起始秒数

    Returns:
        (stem, duration_s) 校验通过；None 校验失败（已打印错误信息）
    """
    name = txt_path.name
    stem = txt_path.stem
    try:
        start_s = float(stem)
    except ValueError:
        print(f"  [错误] 新增 ASR txt 文件名 '{name}' 不是有效的秒数")
        return None

    content = txt_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    if len(lines) < 2:
        print(f"  [错误] 新增 ASR txt '{name}' 必须两行（第一行文本，第二行时长秒数），"
              f"当前仅 {len(lines)} 行")
        return None

    text = lines[0].strip()
    if not text:
        print(f"  [错误] 新增 ASR txt '{name}' 第一行文本为空")
        return None

    try:
        duration_s = float(lines[1].strip())
    except ValueError:
        print(f"  [错误] 新增 ASR txt '{name}' 第二行时长不是有效数字: '{lines[1]}'")
        return None

    if duration_s <= 0.3:
        print(f"  [错误] 新增 ASR txt '{name}' 第二行时长 {duration_s:.3f}s ≤ 0.3s，"
              f"时长过短不允许新增")
        return None

    return (stem, duration_s)


def _detect_and_apply_edits(
    client: RemoteScriptClient,
    task_id: str,
    input_paths: list[Path],
    target_codes: list[str],
    compute_dest_dir,
):
    """编辑重跑预处理：检测本地编辑 → 上传修改 → 删除下游产物。

    检测场景：
    1. 改 ASR 文本：内容不一致 → 上传 ASR 文本 + 删除所有语言目录下对应 翻译文本/合成音频 mp3/翻译候选 md/候选目录
       - 若时长行（第二行）也变更 → 额外删除 segments/{stem}.mp3 + 各语言目录下 mp3/md/候选目录，服务端重新切分
    2. 新增 ASR txt：客户端有但服务端没有 → 校验两行格式 + 时长 > 0.3s → 上传（服务端自动切分 mp3）
    3. 改翻译文本：内容不一致 → 上传翻译文本 + 删除该语言目录下对应 合成音频 mp3/翻译候选 md/候选目录
    4. 替换合成音频：文件大小不一致 → 上传新 MP3 + 删除 combined.mp3/final.mp3 触发重新合成
    5. 删语种：本地语言目录不存在 → 删除服务端对应目录
    6. 删某句合成音频：本地合成音频 mp3 缺失 → 删除服务端对应 合成音频 mp3/翻译候选 md/候选目录
    7. 删某句翻译文本：本地翻译文本缺失 → 删除服务端对应 翻译文本+合成音频 mp3+翻译候选 md+候选目录
    8. 改翻译字幕：full_translation.srt 内容不一致 → 上传 SRT + 解析 SRT 写回 txt 并上传 + 删除 TTS 产物 + 忽略 txt 独立修改
    9. 改翻译指南：translation_guidelines.txt 内容不一致 → 上传新指南 + 删除所有翻译产物强制重新翻译

    所有删除操作同时清理服务端和客户端本地文件，避免后续同步混乱。

    Args:
        client: 远程脚本客户端
        task_id: 任务 ID
        input_paths: 输入音频文件路径列表
        target_codes: 目标语言代码列表
        compute_dest_dir: 计算输入文件在服务端工作目录中的子目录名的函数
                        （由 audio_translate.py 提供，避免重复实现）
    """
    upload_list: list[tuple[Path, str]] = []   # (本地文件路径, 服务端相对路径)
    delete_files: list[str] = []
    delete_dirs: list[str] = []

    for input_path in input_paths:
        p = Path(input_path)
        dest_dir = compute_dest_dir(p)
        local_segments_dir = build_segments_dir(p)

        # ── 递归列出服务端 segments/ 目录结构 ──
        def _list_server_files(sub_dir: str, with_hash: bool = False) -> list[dict]:
            """列出服务端指定子目录的文件和目录"""
            try:
                result = client.list_files(task_id, sub_dir=sub_dir, since=0,
                                           with_hash=with_hash)
                return result.get("items", [])
            except Exception:
                return []

        # 获取服务端 segments/ 下的内容
        server_segments_subdir = f"{dest_dir}/{SEGMENTS_DIRNAME}"
        server_segments_items = _list_server_files(server_segments_subdir)

        if not server_segments_items:
            _log(f"[错误] 服务端 {dest_dir}/segments/ 不存在或为空，"
                  f"请确认任务 {task_id} 已完成过 ASR 阶段。")
            sys.exit(1)

        # 收集服务端 segments/ASR/ 下的 ASR 文本（txt）
        server_asr_subdir = f"{server_segments_subdir}/{ASR_DIRNAME}"
        server_asr_items = _list_server_files(server_asr_subdir)
        server_asr_txts: set[str] = set()  # ASR 文本文件名（如 "0.000.txt"）
        for item in server_asr_items:
            if item["type"] == "file" and item["name"].endswith(".txt"):
                if item["name"] not in _ASR_NON_TXT_FILES:
                    server_asr_txts.add(item["name"])

        # ── 场景 1：检测 ASR 文本内容修改 ──
        local_asr_dir = local_segments_dir / ASR_DIRNAME
        changed_asr_stems: set[str] = set()
        # 时长变更的 stem：需额外删除服务端旧 mp3，触发服务端重新切分
        duration_changed_stems: set[str] = set()

        if local_asr_dir.exists():
            for asr_txt_name in server_asr_txts:
                local_asr_txt = local_asr_dir / asr_txt_name
                if not local_asr_txt.exists():
                    continue  # 客户端没有此文件，跳过（不在 ASR 层面处理删除）

                # 下载服务端 ASR 文本内容对比
                remote_asr_path = f"{server_asr_subdir}/{asr_txt_name}"
                server_content = _download_server_txt(client, task_id, remote_asr_path)
                local_content = local_asr_txt.read_text(encoding="utf-8")

                if server_content is not None and server_content != local_content:
                    stem = asr_txt_name.rsplit(".", 1)[0]
                    changed_asr_stems.add(stem)
                    upload_list.append((local_asr_txt, remote_asr_path))
                    print(f"  [改ASR文本] {asr_txt_name} 内容已修改")
                    _print_txt_diff(local_content, server_content, f"ASR/{asr_txt_name}")

                    # 检查时长行（第二行）是否变更 → 需删除旧 mp3 重新切分
                    # 服务端旧 txt 可能没有第二行（旧格式），此时 server_duration=None，
                    # 只要客户端有时长行就视为变更
                    local_duration = _parse_duration_line(local_content)
                    server_duration = _parse_duration_line(server_content)
                    if local_duration is not None:
                        if server_duration is None:
                            duration_changed_stems.add(stem)
                            print(f"    └ 时长新增: 无 → {local_duration:.3f}s，将删除旧 mp3 重新切分")
                        elif abs(local_duration - server_duration) > 0.001:
                            duration_changed_stems.add(stem)
                            print(f"    └ 时长变更: {server_duration:.3f}s → {local_duration:.3f}s，将删除旧 mp3 重新切分")

        # 时长变更的 segment：删除服务端 segments/ 下的旧 mp3（服务端修复步骤会重新切分）
        # 同时删除翻译目录下对应的 mp3（参考音频长度改变，旧 TTS 产物需重新合成）
        for stem in duration_changed_stems:
            delete_files.append(f"{server_segments_subdir}/{stem}.mp3")
            # 同步删除客户端本地的原声音频 mp3
            _local_seg_mp3 = local_segments_dir / f"{stem}.mp3"
            if _local_seg_mp3.exists():
                _local_seg_mp3.unlink()
            for code in target_codes:
                lang_dir_name = get_language_dir_name(code)
                server_lang_subdir = f"{server_segments_subdir}/{lang_dir_name}"
                local_lang_dir = local_segments_dir / lang_dir_name
                # 服务端：删除 mp3 + md + 候选目录
                delete_files.append(f"{server_lang_subdir}/{stem}.mp3")
                delete_files.append(f"{server_lang_subdir}/{stem}.md")
                delete_dirs.append(f"{server_lang_subdir}/{stem}")
                # 客户端：同步删除 TTS 产物
                _delete_local_tts_artifacts(local_lang_dir, stem)

        if changed_asr_stems:
            # 对每个语言目录，收集需要删除的文件
            for code in target_codes:
                lang_dir_name = get_language_dir_name(code)
                server_lang_subdir = f"{server_segments_subdir}/{lang_dir_name}"
                local_lang_dir = local_segments_dir / lang_dir_name
                for stem in changed_asr_stems:
                    # 服务端：删除翻译文本 + 合成音频 mp3 + 翻译候选 md + 候选目录
                    delete_files.append(f"{server_lang_subdir}/{stem}.txt")
                    delete_files.append(f"{server_lang_subdir}/{stem}.mp3")
                    delete_files.append(f"{server_lang_subdir}/{stem}.md")
                    delete_dirs.append(f"{server_lang_subdir}/{stem}")
                    # 客户端：同步删除翻译文本 + TTS 产物
                    _local_txt = local_lang_dir / f"{stem}.txt"
                    if _local_txt.exists():
                        _local_txt.unlink()
                    _delete_local_tts_artifacts(local_lang_dir, stem)

        # ── 场景 7：检测新增 ASR txt（客户端有但服务端没有）──
        # 用户手动拆句：修改原 txt 的文本和时长 + 新增一个 txt
        # 校验：txt 必须两行，第二行时长 > 0.3s
        # 上传 txt 后，服务端修复步骤会从人声音频切分对应 mp3
        if local_asr_dir.exists():
            local_asr_txt_names = {
                f.name for f in local_asr_dir.glob('*.txt')
                if f.name not in _ASR_NON_TXT_FILES
            }
            new_asr_txts = local_asr_txt_names - server_asr_txts
            for new_txt_name in sorted(new_asr_txts):
                local_txt = local_asr_dir / new_txt_name
                result = _validate_asr_txt_for_new_segment(local_txt)
                if result is None:
                    # 校验失败 → 中断，不允许继续
                    _log(f"[错误] 新增 ASR txt 校验失败，请修正后重试: {new_txt_name}")
                    sys.exit(1)
                stem, duration_s = result
                remote_asr_path = f"{server_asr_subdir}/{new_txt_name}"
                upload_list.append((local_txt, remote_asr_path))
                print(f"  [新增ASR文本] {new_txt_name}（时长 {duration_s:.3f}s）→ 服务端将自动切分 mp3")

        # ── 场景 2-5：检测各语言目录的编辑 ──
        for code in target_codes:
            lang_dir_name = get_language_dir_name(code)
            server_lang_subdir = f"{server_segments_subdir}/{lang_dir_name}"
            local_lang_dir = local_segments_dir / lang_dir_name

            # 场景 3：语言目录不存在 → 删除服务端整个目录
            if not local_lang_dir.exists():
                # 检查服务端是否有此目录
                lang_items = _list_server_files(server_lang_subdir)
                if lang_items:
                    delete_dirs.append(server_lang_subdir)
                    print(f"  [删语种] 本地 {lang_dir_name}/ 不存在 → 删除服务端目录")
                continue

            # 列出服务端语言目录下的文件（带 hash，用于 MP3 内容对比）
            server_lang_items = _list_server_files(server_lang_subdir, with_hash=True)
            server_lang_files_info = {
                item["name"]: item for item in server_lang_items if item["type"] == "file"
            }

            # 收集服务端有但本地没有的文件（场景 4、5：客户端删除了某句）
            for server_file in server_lang_files_info:
                if server_file.startswith('.'):
                    continue
                local_file = local_lang_dir / server_file
                if not local_file.exists():
                    stem = server_file.rsplit(".", 1)[0]
                    ext = server_file.rsplit(".", 1)[1] if "." in server_file else ""

                    if ext == "mp3":
                        # 场景 4：删某句合成音频 → 删除 合成音频 mp3 + 翻译候选 md + 候选目录
                        delete_files.append(f"{server_lang_subdir}/{server_file}")
                        delete_files.append(f"{server_lang_subdir}/{stem}.md")
                        delete_dirs.append(f"{server_lang_subdir}/{stem}")
                        _delete_local_tts_artifacts(local_lang_dir, stem)
                        print(f"  [删合成音频] {lang_dir_name}/{server_file} 本地已删除 → 删除合成音频+翻译候选+候选目录")
                    elif ext == "txt":
                        # 场景 5：删某句翻译文本 → 删除 翻译文本 + 合成音频 mp3 + 翻译候选 md + 候选目录
                        delete_files.append(f"{server_lang_subdir}/{stem}.txt")
                        delete_files.append(f"{server_lang_subdir}/{stem}.mp3")
                        delete_files.append(f"{server_lang_subdir}/{stem}.md")
                        delete_dirs.append(f"{server_lang_subdir}/{stem}")
                        _delete_local_tts_artifacts(local_lang_dir, stem)
                        print(f"  [删翻译文本] {lang_dir_name}/{server_file} 本地已删除 → 删除翻译文本+合成音频+翻译候选")

            # ── 场景 8：检测 full_translation.srt 修改 ──
            # 用户修改 SRT 字幕 → 解析 SRT，将文本写回对应 txt 文件，忽略 txt 的独立修改
            srt_handled_stems: set[str] = set()
            local_srt = local_lang_dir / FULL_TRANSLATION_SRT_FILENAME
            if local_srt.exists():
                server_srt_content = _download_server_txt(
                    client, task_id, f"{server_lang_subdir}/{FULL_TRANSLATION_SRT_FILENAME}",
                )
                local_srt_content = local_srt.read_text(encoding="utf-8")
                if server_srt_content is None or local_srt_content != server_srt_content:
                    print(f"  [改字幕] {lang_dir_name}/{FULL_TRANSLATION_SRT_FILENAME} 内容已修改")
                    # 上传 SRT 文件
                    upload_list.append((local_srt, f"{server_lang_subdir}/{FULL_TRANSLATION_SRT_FILENAME}"))
                    # 解析 SRT，将文本写回对应 txt 文件
                    srt_segments = _parse_srt_to_segments(local_srt_content)
                    # 收集本地已有的 txt stem（排除非逐段文件）
                    existing_stems = {
                        f.stem: f.stem for f in local_lang_dir.glob('*.txt')
                        if f.name not in (
                            TRANSLATION_GUIDELINES_FILENAME,
                            FULL_TRANSLATION_SRT_FILENAME,
                        )
                    }
                    for start_s, text in srt_segments:
                        stem = _match_stem_by_start_s(start_s, existing_stems)
                        if stem is None:
                            print(f"    [跳过] SRT 中 start={start_s:.3f}s 未匹配到任何 txt 文件")
                            continue
                        local_txt = local_lang_dir / f"{stem}.txt"
                        # 比对 SRT 解析出的文本与现有 txt 内容，一致则跳过
                        existing_text = local_txt.read_text(encoding="utf-8") if local_txt.exists() else ""
                        if existing_text == text:
                            srt_handled_stems.add(stem)
                            continue
                        # 文本有变更 → 写入本地 txt + 上传 + 删除 TTS 产物
                        local_txt.write_text(text, encoding="utf-8")
                        upload_list.append((local_txt, f"{server_lang_subdir}/{stem}.txt"))
                        delete_files.append(f"{server_lang_subdir}/{stem}.mp3")
                        delete_files.append(f"{server_lang_subdir}/{stem}.md")
                        delete_dirs.append(f"{server_lang_subdir}/{stem}")
                        _delete_local_tts_artifacts(local_lang_dir, stem)
                        srt_handled_stems.add(stem)
                        print(f"    [SRT→txt] {stem}.txt 已更新")
                    # 删除 combined/final（内容已变，需重新合成）
                    delete_files.append(f"{server_lang_subdir}/{COMBINED_AUDIO_FILENAME}")
                    delete_files.append(f"{server_lang_subdir}/{FINAL_AUDIO_FILENAME}")
                    for _fname in (COMBINED_AUDIO_FILENAME, FINAL_AUDIO_FILENAME):
                        _local_f = local_lang_dir / _fname
                        if _local_f.exists():
                            _local_f.unlink()

            # ── 场景 9：检测 translation_guidelines.txt 修改 ──
            # 用户修改翻译指南 → 上传新指南 + 删除所有翻译产物强制重新翻译
            local_guidelines = local_lang_dir / TRANSLATION_GUIDELINES_FILENAME
            if local_guidelines.exists():
                server_guidelines_content = _download_server_txt(
                    client, task_id, f"{server_lang_subdir}/{TRANSLATION_GUIDELINES_FILENAME}",
                )
                local_guidelines_content = local_guidelines.read_text(encoding="utf-8")
                if server_guidelines_content is None or local_guidelines_content != server_guidelines_content:
                    print(f"  [改翻译指南] {lang_dir_name}/{TRANSLATION_GUIDELINES_FILENAME} 内容已修改")
                    upload_list.append((local_guidelines, f"{server_lang_subdir}/{TRANSLATION_GUIDELINES_FILENAME}"))
                    # 删除所有翻译 txt + TTS 产物，强制服务端用新指南重新翻译
                    _PROTECTED_FILES = frozenset({
                        TRANSLATION_GUIDELINES_FILENAME,
                        FULL_TRANSLATION_SRT_FILENAME,
                        COMBINED_AUDIO_FILENAME,
                        FINAL_AUDIO_FILENAME,
                    })
                    for server_file in server_lang_files_info:
                        if server_file in _PROTECTED_FILES:
                            continue
                        stem = server_file.rsplit(".", 1)[0]
                        ext = server_file.rsplit(".", 1)[1] if "." in server_file else ""
                        if ext in ("txt", "mp3", "md"):
                            delete_files.append(f"{server_lang_subdir}/{server_file}")
                            if ext in ("mp3", "md"):
                                _delete_local_tts_artifacts(local_lang_dir, stem)
                            elif ext == "txt":
                                _local_txt = local_lang_dir / server_file
                                if _local_txt.exists():
                                    _local_txt.unlink()
                        # 删除候选目录
                        delete_dirs.append(f"{server_lang_subdir}/{stem}")
                    # 删除 combined/final
                    delete_files.append(f"{server_lang_subdir}/{COMBINED_AUDIO_FILENAME}")
                    delete_files.append(f"{server_lang_subdir}/{FINAL_AUDIO_FILENAME}")
                    for _fname in (COMBINED_AUDIO_FILENAME, FINAL_AUDIO_FILENAME):
                        _local_f = local_lang_dir / _fname
                        if _local_f.exists():
                            _local_f.unlink()
                    print(f"    已删除 {lang_dir_name}/ 下所有翻译产物，服务端将用新指南重新翻译")

            # 场景 2：检测翻译文本内容修改（SRT 已处理的 stem 跳过）
            for local_file in local_lang_dir.iterdir():
                if local_file.name.startswith('.'):
                    continue
                if not local_file.is_file():
                    continue
                if not local_file.name.endswith(".txt"):
                    continue
                # 跳过非逐段 txt 文件（翻译指南等）
                if local_file.name in (TRANSLATION_GUIDELINES_FILENAME,):
                    continue

                stem = local_file.name.rsplit(".", 1)[0]
                # SRT 已处理的 stem 跳过（文本已从 SRT 写入）
                if stem in srt_handled_stems:
                    continue

                server_txt_path = f"{server_lang_subdir}/{local_file.name}"

                # 服务端没有此翻译文本（可能是客户端新增的翻译，不在编辑重跑场景内，跳过）
                if local_file.name not in server_lang_files_info:
                    continue

                # 下载服务端翻译文本对比内容
                server_content = _download_server_txt(client, task_id, server_txt_path)
                local_content = local_file.read_text(encoding="utf-8")

                if server_content is not None and server_content != local_content:
                    # 改翻译文本 → 上传 + 删除对应 合成音频 mp3 + 翻译候选 md + 候选目录
                    upload_list.append((local_file, server_txt_path))
                    delete_files.append(f"{server_lang_subdir}/{stem}.mp3")
                    delete_files.append(f"{server_lang_subdir}/{stem}.md")
                    delete_dirs.append(f"{server_lang_subdir}/{stem}")
                    # 客户端：同步删除 TTS 产物
                    _delete_local_tts_artifacts(local_lang_dir, stem)
                    print(f"  [改翻译文本] {lang_dir_name}/{local_file.name} 内容已修改")
                    _print_txt_diff(local_content, server_content, f"{lang_dir_name}/{local_file.name}")

            # 场景 6：检测合成音频 MP3 被替换
            # 对比策略：先比大小，大小相同再比 MD5 哈希。
            # 纯大小对比无法检测"同大小不同内容"的替换（如从候选目录拷贝同时长不同候选）。
            # 不用 mtime：客户端从服务端下载文件时本地 mtime 会被刷新为下载时间，
            # 导致 mtime 永远比服务端新，无法区分"下载"和"替换"。
            for local_file in local_lang_dir.iterdir():
                if local_file.name.startswith('.'):
                    continue
                if not local_file.is_file():
                    continue
                if not local_file.name.endswith(".mp3"):
                    continue
                # 跳过 combined.mp3 / final.mp3（非逐句合成音频）
                if local_file.name in (COMBINED_AUDIO_FILENAME, FINAL_AUDIO_FILENAME):
                    continue

                server_file_info = server_lang_files_info.get(local_file.name)
                if server_file_info is None:
                    continue  # 服务端没有此文件，不在本场景处理

                local_size = local_file.stat().st_size
                server_size = server_file_info.get("size")
                server_hash = server_file_info.get("hash")

                if server_size is not None and local_size == server_size:
                    # 大小相同，比较哈希确认内容是否一致
                    local_hash = _compute_file_hash(local_file)
                    if local_hash == server_hash:
                        continue  # 内容相同，未修改
                    diff_reason = f"内容不同 (hash {local_hash[:8]}.. vs {server_hash[:8]}..)"
                else:
                    diff_reason = f"大小不同 (本地 {local_size} bytes vs 服务端 {server_size} bytes)"

                # MP3 已被替换 → 上传 + 删除 combined/final 触发重新合成
                remote_mp3_path = f"{server_lang_subdir}/{local_file.name}"
                upload_list.append((local_file, remote_mp3_path))
                delete_files.append(f"{server_lang_subdir}/{COMBINED_AUDIO_FILENAME}")
                delete_files.append(f"{server_lang_subdir}/{FINAL_AUDIO_FILENAME}")
                # 客户端：同步删除 combined/final
                for _fname in (COMBINED_AUDIO_FILENAME, FINAL_AUDIO_FILENAME):
                    _local_f = local_lang_dir / _fname
                    if _local_f.exists():
                        _local_f.unlink()
                print(f"  [替换合成音频] {lang_dir_name}/{local_file.name} {diff_reason}")

    # ── 执行上传 ──
    if upload_list:
        _log(f"上传 {len(upload_list)} 个修改的文件...")
        for local_file, remote_path in upload_list:
            try:
                client.upload(local_file, task_id=task_id, dest_path=remote_path)
                print(f"  已上传: {remote_path}")
                # 上传成功后 touch 本地文件，更新 mtime 为当前时间。
                # 防止后续同步逻辑因"服务端 mtime > 本地 mtime"而重新下载覆盖。
                # （用户从候选目录拷贝文件时 mtime 可能保留为旧值，导致同步误判）
                local_file.touch()
            except Exception as e:
                print(f"  [错误] 上传失败 {remote_path}: {e}")

    # ── 执行删除 ──
    if delete_files or delete_dirs:
        _log(f"删除 {len(delete_files)} 个文件 + {len(delete_dirs)} 个目录...")
        try:
            result = client.delete_files(
                task_id,
                files=delete_files,
                dirs=delete_dirs,
            )
            deleted_files = result.get("deleted_files", [])
            deleted_dirs = result.get("deleted_dirs", [])
            errors = result.get("errors", [])
            if deleted_files:
                print(f"  已删除 {len(deleted_files)} 个文件")
            if deleted_dirs:
                print(f"  已删除 {len(deleted_dirs)} 个目录")
            if errors:
                print(f"  [警告] {len(errors)} 个删除错误:")
                for err in errors[:10]:
                    print(f"    {err}")
        except Exception as e:
            print(f"  [错误] 批量删除失败: {e}")

    if not upload_list and not delete_files and not delete_dirs:
        _log("未检测到任何编辑变更，服务端文件已是最新。")


def preprocess_edit_rerun(
    client: RemoteScriptClient,
    task_id: str | None,
    input_paths: list[Path],
    target_languages: list[str] | None,
    compute_dest_dir,
):
    """编辑重跑预处理入口：在主流程上传文件之后、调用服务端脚本之前调用。

    1. 校验 task_id 存在（编辑重跑必须有历史任务）
    2. 时间同步检查
    3. 校验服务端 segments/ 已存在（必须跑过 ASR）
    4. 校验服务端无正在运行的任务
    5. 解析目标语言代码，执行 _detect_and_apply_edits

    Args:
        client: 远程脚本客户端
        task_id: 任务 ID（不能为 None）
        input_paths: 输入音频文件路径列表
        target_languages: 目标语言原始字符串列表（来自 args.targets）
        compute_dest_dir: 计算输入文件在服务端工作目录中的子目录名的函数
    """
    if not task_id:
        _log("[错误] 编辑重跑模式需要已有的 task_id，但未找到 .vt_task_id 文件。")
        print("  编辑重跑模式要求服务端之前已跑过此任务。如需新建任务，去掉 --edit-rerun 参数。")
        sys.exit(1)

    _log("--- 编辑重跑预处理 ---")

    # 时间同步检查
    _check_server_time(client)

    # 验证服务端已有 segments 输出（list_files 检查）
    first_input = input_paths[0]
    dest_dir = compute_dest_dir(first_input)
    segments_subdir = f"{dest_dir}/{SEGMENTS_DIRNAME}"
    try:
        result = client.list_files(task_id, sub_dir=segments_subdir, since=0)
        if not result.get("items"):
            _log(f"[错误] 服务端 {segments_subdir} 不存在或为空，"
                  f"请确认任务 {task_id} 已完成过 ASR 阶段。")
            sys.exit(1)
    except Exception as e:
        _log(f"[错误] 无法访问服务端 segments 目录: {e}")
        sys.exit(1)

    # 检查服务端无正在运行的任务
    try:
        running = client.status(task_id, since_line=0)
        if running.get("status") == "running":
            _log(f"[错误] 任务 {task_id} 正在运行中，请等待完成后再编辑重跑。")
            sys.exit(1)
    except Exception:
        pass  # 查询失败不阻断

    # 解析目标语言
    target_codes = normalize_target_language_codes(target_languages) if target_languages else []

    # 执行编辑检测和变更
    _detect_and_apply_edits(client, task_id, input_paths, target_codes, compute_dest_dir)

    _log("--- 编辑重跑预处理完成 ---")
