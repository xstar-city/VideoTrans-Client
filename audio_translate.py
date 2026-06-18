#!/usr/bin/env python3
"""音频翻译客户端代理

将音频文件上传到服务端远程执行翻译流水线，实时查看步骤进度，增量下载翻译结果到本地。

用法：
    python audio_translate.py input.mp3 -t en --server <ServerIP>

断点续跑（task_id 机制）：
    客户端与服务端为一对一模式：一个 task_id 对应一个固定的工作目录。
    上传音频后，会在第一个音频文件所在目录生成 .vt_task_id 文件记录 task_id。
    再次运行时（同一音频目录），脚本自动读取该文件，复用老 task_id 重新执行脚本——
    效果等价于登录服务器在同一目录下重新跑命令：服务端工作目录不变，
    audio_translate.py 内部的缓存检查机制会自动跳过已完成的步骤（ASR、翻译、TTS 等），
    只执行之前失败或未完成的步骤，实现断点续跑。
    如需强制从零开始，使用 --new-task 参数或手动删除 .vt_task_id 文件。

依赖：
    - requests（pip install requests）
    - remote_client.py（通用 HTTP 客户端）
"""

from __future__ import annotations

import argparse
import hashlib
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
    build_segments_dir,
)
from Common.asr_languages import ALL_ASR_LANGUAGE_CODES
from Common.tts_languages import ALL_TTS_LANGUAGE_CODES
from Common.language_map import get_language_dir_name


from remote_client import RemoteScriptClient, normalize_server_url


# ─── task_id 持久化 ───────────────────────────────────────

TASK_ID_FILENAME = ".vt_task_id"


def _save_task_id(audio_path: Path, task_id: str):
    """将 task_id 保存到音频文件旁边的 .vt_task_id 文件"""
    marker = audio_path.parent / TASK_ID_FILENAME
    marker.write_text(task_id, encoding="utf-8")


def _save_task_id_all(input_paths: list[Path], task_id: str):
    """将同一个 task_id 保存到所有输入文件所在目录的 .vt_task_id 文件"""
    for p in input_paths:
        _save_task_id(p, task_id)


def _load_task_id(audio_path: Path) -> str | None:
    """从音频文件旁读取之前保存的 task_id"""
    marker = audio_path.parent / TASK_ID_FILENAME
    if marker.exists():
        task_id = marker.read_text(encoding="utf-8").strip()
        if task_id:
            return task_id
    return None


def _validate_task_ids(input_paths: list[Path]) -> str | None:
    """检查所有输入文件目录下的 .vt_task_id 是否一致。

    返回:
        一致的 task_id（所有目录都有且相同时）
        None（所有目录都没有 .vt_task_id）

    异常退出:
        如果某些目录有、某些没有，或 task_id 不一致，报错退出。
    """
    dir_task_ids: dict[str, str | None] = {}  # {目录绝对路径: task_id 或 None}
    for p in input_paths:
        dir_key = str(p.resolve().parent)
        if dir_key not in dir_task_ids:
            dir_task_ids[dir_key] = _load_task_id(p)

    # 去重后的 task_id 集合（排除 None）
    unique_ids = {v for v in dir_task_ids.values() if v is not None}

    if not unique_ids:
        # 所有目录都没有 task_id
        return None

    if len(unique_ids) > 1:
        # 多个不同的 task_id
        id_dirs = {}
        for dir_key, tid in dir_task_ids.items():
            if tid:
                id_dirs.setdefault(tid, []).append(dir_key)
        details = "\n".join(
            f"  task_id={tid}: {', '.join(dirs)}"
            for tid, dirs in id_dirs.items()
        )
        print("[错误] 不同视频目录下的 .vt_task_id 不一致，不能混合执行！")
        print(f"  这些视频曾在不同任务中运行过，请先删除不需要的 .vt_task_id 文件：")
        print(details)
        sys.exit(1)

    # 只有一个 task_id，检查是否所有目录都有
    the_id = unique_ids.pop()
    missing_dirs = [k for k, v in dir_task_ids.items() if v is None]
    if missing_dirs:
        print("[错误] 部分视频目录缺少 .vt_task_id，不能混合执行！")
        print(f"  task_id={the_id} 存在于部分目录，但以下目录没有：")
        for d in missing_dirs:
            print(f"    {d}")
        print("  这些视频从未在此任务中运行过，混合执行会导致文件混乱。")
        print("  如需重新跑，请删除所有相关目录下的 .vt_task_id 文件后重试。")
        sys.exit(1)

    return the_id






# ─── 辅助函数 ─────────────────────────────────────────────

def _compute_dest_dir(file_path: Path) -> str:
    """计算文件在服务端工作目录中的子目录名（父目录名 + hash 防冲突）。

    例如：E:\\...\\《逐玉》\\1.mp3 → "《逐玉》_a3f1"
    """
    parent = file_path.resolve().parent
    hash_suffix = hashlib.md5(str(parent).encode()).hexdigest()[:4]
    return f"{parent.name}_{hash_suffix}"


def _compute_video_summary(file_paths: list[Path]) -> str:
    """计算视频名称摘要，用于 dashboard 展示。

    单文件：最后3层父路径，如 Demo\\多语种翻译\\《逐玉》
    多文件(>2)：前2个 + ...等N个
    """
    def _single_summary(p: Path) -> str:
        parts = p.resolve().parent.parts
        return "\\".join(parts[-3:]) if len(parts) >= 3 else "\\".join(parts)

    summaries = [_single_summary(p) for p in file_paths]
    if len(summaries) == 1:
        return summaries[0]
    if len(summaries) <= 2:
        return ", ".join(summaries)
    return f"{summaries[0]}, {summaries[1]}, ...等{len(summaries)}个"


def _find_matching_srt(audio_path: Path) -> Path | None:
    """查找与音频同名的 SRT 文件。"""
    for suffix in ('.srt', '.SRT'):
        srt_path = audio_path.with_suffix(suffix)
        if srt_path.exists():
            return srt_path
    return None


# ─── 编辑重跑模式 ─────────────────────────────────────────

# 服务端 segments/ASR/ 下的非 txt 文件（full_text.md 等），对比时跳过
_ASR_NON_TXT_FILES = frozenset({
    ASR_FULL_TEXT_FILENAME,  # full_text.md
    ASR_SENTENCE_RECONCILE_FILENAME,
    SECONDARY_DIARIZATION_CALIBRATE_LOG_FILENAME,
})


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
        return resp.text
    except Exception:
        return None


def _detect_and_apply_edits(
    client: RemoteScriptClient,
    task_id: str,
    input_paths: list[Path],
    target_codes: list[str],
):
    """编辑重跑预处理：检测本地编辑 → 上传修改 → 删除下游产物。

    五种检测场景：
    1. 改 ASR txt：内容不一致 → 上传 + 删除所有语言目录下对应 txt/mp3/md
    2. 改翻译 txt：内容不一致 → 上传 + 删除该语言目录下对应 mp3/md
    3. 删语种：本地语言目录不存在 → 删除服务端对应目录
    4. 删某句 mp3：本地缺失 → 删除服务端对应 mp3
    5. 删某句 txt：本地缺失 → 删除服务端对应 txt+mp3+md+候选目录
    """
    upload_list: list[tuple[Path, str]] = []   # (本地文件路径, 服务端相对路径)
    delete_files: list[str] = []
    delete_dirs: list[str] = []

    for input_path in input_paths:
        p = Path(input_path)
        dest_dir = _compute_dest_dir(p)
        local_segments_dir = build_segments_dir(p)

        # ── 递归列出服务端 segments/ 目录结构 ──
        server_files: dict[str, set[str]] = {}  # {子目录相对路径: {文件名集合}}

        def _list_server_files(sub_dir: str) -> list[dict]:
            """列出服务端指定子目录的文件和目录"""
            try:
                result = client.list_files(task_id, sub_dir=sub_dir, since=0)
                return result.get("items", [])
            except Exception:
                return []

        # 获取服务端 segments/ 下的内容
        server_segments_subdir = f"{dest_dir}/{SEGMENTS_DIRNAME}"
        server_segments_items = _list_server_files(server_segments_subdir)

        if not server_segments_items:
            print(f"[错误] 服务端 {dest_dir}/segments/ 不存在或为空，"
                  f"请确认任务 {task_id} 已完成过 ASR 阶段。")
            sys.exit(1)

        # 收集服务端 segments/ASR/ 下的 txt 文件
        server_asr_subdir = f"{server_segments_subdir}/{ASR_DIRNAME}"
        server_asr_items = _list_server_files(server_asr_subdir)
        server_asr_txts: set[str] = set()  # ASR 目录下的 txt 文件名（如 "0.000.txt"）
        for item in server_asr_items:
            if item["type"] == "file" and item["name"].endswith(".txt"):
                if item["name"] not in _ASR_NON_TXT_FILES:
                    server_asr_txts.add(item["name"])

        # ── 场景 1：检测 ASR txt 内容修改 ──
        local_asr_dir = local_segments_dir / ASR_DIRNAME
        changed_asr_stems: set[str] = set()

        if local_asr_dir.exists():
            for asr_txt_name in server_asr_txts:
                local_asr_txt = local_asr_dir / asr_txt_name
                if not local_asr_txt.exists():
                    continue  # 客户端没有此文件，跳过（不在 ASR 层面处理删除）

                # 下载服务端 txt 内容对比
                remote_asr_path = f"{server_asr_subdir}/{asr_txt_name}"
                server_content = _download_server_txt(client, task_id, remote_asr_path)
                local_content = local_asr_txt.read_text(encoding="utf-8")

                if server_content is not None and server_content != local_content:
                    stem = asr_txt_name.rsplit(".", 1)[0]
                    changed_asr_stems.add(stem)
                    upload_list.append((local_asr_txt, remote_asr_path))
                    print(f"  [改ASR] {asr_txt_name} 内容已修改")

        if changed_asr_stems:
            # 对每个语言目录，收集需要删除的文件
            for code in target_codes:
                lang_dir_name = get_language_dir_name(code)
                server_lang_subdir = f"{server_segments_subdir}/{lang_dir_name}"
                for stem in changed_asr_stems:
                    # 删除翻译 txt + TTS mp3 + 翻译 md
                    delete_files.append(f"{server_lang_subdir}/{stem}.txt")
                    delete_files.append(f"{server_lang_subdir}/{stem}.mp3")
                    delete_files.append(f"{server_lang_subdir}/{stem}.md")

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

            # 列出服务端语言目录下的文件
            server_lang_items = _list_server_files(server_lang_subdir)
            server_lang_files = {
                item["name"] for item in server_lang_items if item["type"] == "file"
            }

            # 收集服务端有但本地没有的文件（场景 4、5：客户端删除了某句）
            for server_file in server_lang_files:
                if server_file.startswith('.'):
                    continue
                local_file = local_lang_dir / server_file
                if not local_file.exists():
                    stem = server_file.rsplit(".", 1)[0]
                    ext = server_file.rsplit(".", 1)[1] if "." in server_file else ""

                    if ext == "mp3":
                        # 场景 4：删某句 mp3
                        delete_files.append(f"{server_lang_subdir}/{server_file}")
                        print(f"  [删mp3] {lang_dir_name}/{server_file} 本地已删除")
                    elif ext == "txt":
                        # 场景 5：删某句 txt → 删除 txt + mp3 + md + 候选目录
                        delete_files.append(f"{server_lang_subdir}/{stem}.txt")
                        delete_files.append(f"{server_lang_subdir}/{stem}.mp3")
                        delete_files.append(f"{server_lang_subdir}/{stem}.md")
                        delete_dirs.append(f"{server_lang_subdir}/{stem}")
                        print(f"  [删txt] {lang_dir_name}/{server_file} 本地已删除 → 删除翻译+TTS+候选")

            # 场景 2：检测翻译 txt 内容修改
            for local_file in local_lang_dir.iterdir():
                if local_file.name.startswith('.'):
                    continue
                if not local_file.is_file():
                    continue
                if not local_file.name.endswith(".txt"):
                    continue

                stem = local_file.name.rsplit(".", 1)[0]
                server_txt_path = f"{server_lang_subdir}/{local_file.name}"

                # 服务端没有此 txt（可能是客户端新增的翻译，不在编辑重跑场景内，跳过）
                if local_file.name not in server_lang_files:
                    continue

                # 下载服务端 txt 对比内容
                server_content = _download_server_txt(client, task_id, server_txt_path)
                local_content = local_file.read_text(encoding="utf-8")

                if server_content is not None and server_content != local_content:
                    # 改翻译 txt → 上传 + 删除对应 mp3 + md
                    upload_list.append((local_file, server_txt_path))
                    delete_files.append(f"{server_lang_subdir}/{stem}.mp3")
                    delete_files.append(f"{server_lang_subdir}/{stem}.md")
                    print(f"  [改翻译] {lang_dir_name}/{local_file.name} 内容已修改")

    # ── 执行上传 ──
    if upload_list:
        print(f"\n上传 {len(upload_list)} 个修改的 txt 文件...")
        for local_file, remote_path in upload_list:
            try:
                client.upload(local_file, task_id=task_id, dest_path=remote_path)
                print(f"  已上传: {remote_path}")
            except Exception as e:
                print(f"  [错误] 上传失败 {remote_path}: {e}")

    # ── 执行删除 ──
    if delete_files or delete_dirs:
        print(f"\n删除 {len(delete_files)} 个文件 + {len(delete_dirs)} 个目录...")
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
        print("未检测到任何编辑变更，服务端文件已是最新。")


def _build_remote_args(args) -> list[str]:
    """将客户端参数转换为服务端脚本的命令行参数。"""
    remote_args = []

    # 音频文件名（含子目录路径，服务端 cwd 为 workspace/{task_id}/）
    for input_path in args.inputs:
        p = Path(input_path)
        dest_dir = _compute_dest_dir(p)
        remote_args.append(f"{dest_dir}/{p.name}")

    # 目标语言
    if args.targets:
        remote_args.extend(['--target'] + args.targets)

    # 源语言
    if args.source:
        remote_args.extend(['--source', args.source])

    # 人声分离：默认开启；只在用户显式关闭时透传 --no-separate
    if not args.separate:
        remote_args.append('--no-separate')

    # 额外翻译指南
    if args.extra_translation_guideline:
        # 上传指南文件，传文件名
        guideline_path = Path(args.extra_translation_guideline)
        remote_args.extend(['--extra_translation_guideline', guideline_path.name])

    # 非语言人声 + 唱歌检测：默认开启；只在用户显式关闭时透传 --no- 标志
    if not args.detect_nonverbal_and_singing:
        remote_args.append('--no-detect-nonverbal-and-singing')

    # 降噪
    remote_args.extend(['--denoise', args.denoise])

    # ASR 模式
    remote_args.extend(['--asr-mode', args.asr_mode])

    # 翻译模型
    if args.translation_models:
        remote_args.extend(['--translation-models', args.translation_models])

    # 翻译模式
    remote_args.extend(['--translation-mode', args.translation_mode])

    # TTS 感知重试次数
    remote_args.extend(['--tts-aware-max-retries', str(args.tts_aware_max_retries)])

    # 音频时长伸缩限制
    remote_args.extend(['--max-audio-slowdown-pct', str(args.max_audio_slowdown_pct)])
    remote_args.extend(['--max-audio-speedup-pct', str(args.max_audio_speedup_pct)])

    # 视频时长伸缩限制
    remote_args.extend(['--max-video-slowdown-pct', str(args.max_video_slowdown_pct)])
    remote_args.extend(['--max-video-speedup-pct', str(args.max_video_speedup_pct)])

    # 视觉辅助说话人切分（默认关闭，仅在显式开启时透传）
    if getattr(args, 'enable_visual_diarization', False):
        remote_args.append('--enable-visual-diarization')

    # 编辑重跑模式：透传 --skip-asr 让服务端跳过 ASR 流程
    if getattr(args, 'edit_rerun', False):
        remote_args.append('--skip-asr')

    return remote_args


# 客户端可见文件扩展名（与服务端 client_visibility.py 白名单一致）
# 仅这些扩展名的文件参与孤儿清理，其他文件（.mp4/.wav/隐藏文件等）不受影响
_SYNC_MANAGED_EXTS = frozenset({'.mp3', '.txt', '.srt'})


def _cleanup_stale_sync_files(local_dir: Path, server_names: set[str]) -> int:
    """删除本地存在但服务端已不存在的同步管理文件（.mp3/.txt/.srt）。

    这些文件通常是因为服务端重命名/重新生成后，旧文件残留在客户端。
    典型场景：VAD 裁剪将 132.700.mp3 改名为 132.920.mp3，
    服务端已删除旧文件但客户端仍残留。

    返回删除的文件数。
    """
    if not local_dir.exists():
        return 0
    removed = 0
    for local_file in local_dir.iterdir():
        if local_file.is_dir():
            continue
        # 隐藏文件（.vt_task_id 等）不动
        if local_file.name.startswith('.'):
            continue
        # 仅清理同步管理扩展名
        if local_file.suffix.lower() not in _SYNC_MANAGED_EXTS:
            continue
        if local_file.name not in server_names:
            local_file.unlink()
            removed += 1
    if removed:
        print(f"  清理了 {removed} 个过时文件（服务端已不再有）: {local_dir.name}/")
    return removed


def _sync_files(client: RemoteScriptClient, task_id: str, local_dir: Path,
                sub_dir: str = "", since: float = 0,
                cleanup_stale: bool = False) -> float:
    """从服务端增量下载文件到本地目录，返回最新的 mtime。

    cleanup_stale=True 时，同步完成后删除本地过时文件
    （服务端已不存在的 .mp3/.txt/.srt，如 VAD 裁剪改名后的旧文件）。
    """
    result = client.list_files(task_id, sub_dir=sub_dir, since=since)
    latest_mtime = since

    # 记录服务端可见条目名，用于清理本地孤儿文件
    server_names: set[str] = set()

    for item in result.get("items", []):
        name = item["name"]
        server_names.add(name)
        mtime = item.get("mtime", 0)
        if mtime > latest_mtime:
            latest_mtime = mtime

        if item["type"] == "dir":
            # 递归同步子目录
            local_sub = local_dir / name
            local_sub.mkdir(parents=True, exist_ok=True)
            sub_latest = _sync_files(
                client, task_id, local_sub,
                sub_dir=f"{sub_dir}/{name}" if sub_dir else name,
                since=since,
                cleanup_stale=cleanup_stale,
            )
            if sub_latest > latest_mtime:
                latest_mtime = sub_latest
        else:
            # 下载文件（size 一致则跳过，避免重复下载）
            local_file = local_dir / name
            remote_path = f"{sub_dir}/{name}" if sub_dir else name
            remote_size = item.get("size", -1)
            if local_file.exists() and remote_size >= 0:
                try:
                    local_size = local_file.stat().st_size
                    if local_size == remote_size:
                        continue
                except OSError:
                    pass
            try:
                client.download(task_id, remote_path, local_path=local_file)
            except Exception as e:
                if "404" in str(e):
                    # 文件在列表获取后被改名/删除（如 VAD 裁剪改名），下一轮同步会拿到新文件名
                    pass
                else:
                    print(f"  下载失败 {remote_path}: {e}")

    # 清理孤儿文件（仅在最终同步时执行，避免轮询期间临时清空）
    if cleanup_stale:
        _cleanup_stale_sync_files(local_dir, server_names)

    return latest_mtime


def _verify_sync(client: RemoteScriptClient, task_id: str, local_dir: Path,
                 sub_dir: str = "") -> list[str]:
    """验证本地文件与服务端一致，返回缺失或大小不匹配的文件路径列表。

    与 _sync_files 不同，此函数只检查不下载，用于最终确认所有文件已同步完成。
    """
    missing = []
    try:
        result = client.list_files(task_id, sub_dir=sub_dir, since=0)
    except Exception:
        # 无法连接服务端时，无法验证，返回空列表
        return missing

    for item in result.get("items", []):
        name = item["name"]
        if item["type"] == "dir":
            # 递归验证子目录
            local_sub = local_dir / name
            sub_missing = _verify_sync(
                client, task_id, local_sub,
                sub_dir=f"{sub_dir}/{name}" if sub_dir else name,
            )
            missing.extend(sub_missing)
        else:
            local_file = local_dir / name
            remote_path = f"{sub_dir}/{name}" if sub_dir else name
            remote_size = item.get("size", -1)
            if not local_file.exists():
                missing.append(remote_path)
            elif remote_size >= 0:
                try:
                    local_size = local_file.stat().st_size
                    if local_size != remote_size:
                        missing.append(f"{remote_path} (大小不匹配: 本地{local_size} vs 服务端{remote_size})")
                except OSError:
                    missing.append(remote_path)
    return missing


def _find_local_only_files(
    client: RemoteScriptClient, task_id: str,
    local_dir: Path, sub_dir: str = ""
) -> list[str]:
    """检查本地存在但服务端不存在的同步管理文件（.mp3/.txt/.srt）。

    与 _verify_sync 互补：_verify_sync 检查"服务端→本地"方向（服务端有的文件本地是否也有），
    此函数检查"本地→服务端"方向（本地有的文件服务端是否也有）。

    返回多余本地文件的相对路径列表。
    """
    extras = []

    if not local_dir.exists():
        return extras

    try:
        result = client.list_files(task_id, sub_dir=sub_dir, since=0)
    except Exception:
        # 无法连接服务端时跳过此目录的检查
        return extras

    server_file_names: set[str] = set()
    for item in result.get("items", []):
        if item["type"] == "file":
            server_file_names.add(item["name"])

    for local_file in local_dir.iterdir():
        if local_file.name.startswith('.'):
            continue
        if local_file.is_dir():
            child_sub = f"{sub_dir}/{local_file.name}" if sub_dir else local_file.name
            child_extras = _find_local_only_files(
                client, task_id, local_file, sub_dir=child_sub
            )
            extras.extend(child_extras)
        elif local_file.suffix.lower() in _SYNC_MANAGED_EXTS:
            if local_file.name not in server_file_names:
                rel = f"{sub_dir}/{local_file.name}" if sub_dir else local_file.name
                extras.append(f"{rel} (服务端不存在)")

    return extras


# ─── 主流程 ────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description='音频翻译客户端：上传音频到服务端远程执行翻译，增量下载结果。')
    p.add_argument('inputs', nargs='+', help='本地音频文件路径列表 (mp3)。')
    p.add_argument('--target', '-t', dest='targets', nargs='+', default=['en'], choices=ALL_TTS_LANGUAGE_CODES, help='要翻译输出的目标语言代码，默认：en（English）')
    p.add_argument('--source', '-s', default='zh', choices=ALL_ASR_LANGUAGE_CODES, help='输入音频的源语言代码（例如 en, zh），默认：zh（普通话，也支持中国方言）')
    p.add_argument('--separate', action=argparse.BooleanOptionalAction, default=True,
                   help='是否运行人声分离以去除背景音。默认开启；传 --no-separate 关闭，跳过分离直接使用原始音频。')
    p.add_argument('--detect-nonverbal-and-singing', action=argparse.BooleanOptionalAction, default=True,
                   help='检测「非语言人声」（笑/咳/喷嚏/掌声/叹息）与「唱歌」段，自动从 vocals 分流到背景音轨道。'
                        '这些虽是人声但无法翻译，留在 vocals 中会污染下游 ASR。默认开启；'
                        '传 --no-detect-nonverbal-and-singing 关闭。')
    p.add_argument('--denoise', choices=['none', 'normal', 'aggressive'], default='aggressive', help='降噪类型（需要人声分离）。none=不降噪，normal=标准降噪，aggressive=激进降噪。默认：aggressive')
    p.add_argument('--asr-mode', choices=['basic', 'precise'], default='precise', help='ASR 说话人切分模式: basic=ASR 自带说话人切分, precise=二次精细说话人切分（默认）')
    p.add_argument('--diarization-lowpass-freq', type=int, default=8000, help='二次精细说话人切分的低通滤波截止频率 (Hz)。杂音多的录音可调低到 3000-5000，正常录音 8000。越高保留越多高频语音细节，但也更易引入伪影噪音。默认：8000')
    p.add_argument('--diarization-highpass-freq', type=int, default=80, help='二次精细说话人切分的高通滤波截止频率 (Hz)。默认：80')
    p.add_argument('--translation-models', default='', help='用于翻译的逗号分隔模型列表。空值使用服务端默认值。')
    p.add_argument('--translation-mode', choices=['independent', 'tts_aware'], default='tts_aware', help='翻译模式: independent=纯文本独立翻译, tts_aware=TTS时长感知翻译（翻译+TTS试合成+时长评估+LLM反馈调整）。默认：tts_aware')
    p.add_argument('--extra-translation-guideline', help='包含额外翻译指南（e.g.定制化场景要求）的文本文件路径（可选参数）')
    p.add_argument('--tts-aware-max-retries', type=int, default=3, help='TTS时长感知模式中每句的最大时长调整重试次数（默认: 3）')
    # 视频的伸缩，是音频最后没能伸缩到1，剩下的部分。比如tts合成音频长1.5s，原音频长1s，压缩音频到1.3s后，剩下的0.3s，就是视频的伸缩，画面会变快。
    p.add_argument('--max-audio-slowdown-pct', type=float, default=0.1, help='允许的最大 TTS 音频加速比例（相对原始时长）')
    p.add_argument('--max-audio-speedup-pct', type=float, default=0.2, help='允许的最大 TTS 音频减慢比例（相对原始时长）')
    p.add_argument('--max-video-slowdown-pct', type=float, default=0.1, help='视频片段最大允许减速比例（相对原始时长）')
    p.add_argument('--max-video-speedup-pct', type=float, default=0.2, help='视频片段最大允许加速比例（相对原始时长）')
    p.add_argument('--server', default='localhost', help='服务端 IP 地址 (默认: localhost)')
    p.add_argument('--new-task', action='store_true', help='忽略本地保存的 task_id，强制创建新任务。')
    p.add_argument('--edit-rerun', action='store_true',
                   help='编辑重跑模式：检测本地编辑（改ASR/改翻译/删语种/删mp3/删txt），'
                        '上传修改的文件并删除服务端对应的下游产物，服务端跳过ASR直接从翻译开始。'
                        '要求服务端已有该任务的运行记录。')
    # 内部参数：仅供客户端 video_translate.py 透传给服务端；不在 --help 中展示，
    # 终端用户不应通过 audio_translate 的命令行使用此开关（它要求输入是视频，与音频翻译入口职责冲突）。
    p.add_argument('--enable-visual-diarization', dest='enable_visual_diarization',
                   action='store_true', default=False, help=argparse.SUPPRESS)
    args = p.parse_args()

    task_start_time = time.time()

    server_url = normalize_server_url(args.server)
    client = RemoteScriptClient(server_url)

    # ── 预检：验证服务端可达 ──────────────────────────────────
    try:
        client.check_server()
    except ConnectionError as e:
        print(f"[错误] {e}")
        sys.exit(1)

    first_input = Path(args.inputs[0])

    # ── 0. 检查本地是否有保存的 task_id ──────────────────────
    # 有则直接复用老 task_id 重新执行脚本，服务端工作目录不变，缓存文件可用
    # 无则上传文件 + 创建新任务
    # 多视频时，所有视频目录的 .vt_task_id 必须一致，否则报错
    input_paths = [Path(p) for p in args.inputs]
    task_id = None
    if not args.new_task:
        task_id = _validate_task_ids(input_paths)
        if task_id:
            print(f"发现已保存的 task_id={task_id}，复用该任务继续跑...")

    # ── 1. 上传音频文件 ──────────────────────────────────────
    # 老 task_id 时先查询服务端已有文件，size 一致则跳过上传
    existing_remote_files: dict[str, int] = {}  # {相对路径: size}
    if task_id:
        # 查询根目录文件（翻译指南等放根目录）
        try:
            result = client.list_files(task_id, sub_dir="", since=0)
            for item in result.get("items", []):
                if item["type"] == "file":
                    size = item.get("size", -1)
                    if size >= 0:
                        existing_remote_files[item["name"]] = size
        except Exception:
            pass
        # 查询各子目录中的文件
        for input_path in args.inputs:
            dest_dir = _compute_dest_dir(Path(input_path))
            try:
                result = client.list_files(task_id, sub_dir=dest_dir, since=0)
                for item in result.get("items", []):
                    if item["type"] == "file":
                        key = f"{dest_dir}/{item['name']}"
                        size = item.get("size", -1)
                        if size >= 0:
                            existing_remote_files[key] = size
            except Exception:
                pass  # 子目录不存在时不阻断，走全量上传

    for input_path in args.inputs:
        path = Path(input_path)
        if not path.exists():
            print(f"文件不存在: {path}")
            sys.exit(1)

        dest_dir = _compute_dest_dir(path)
        dest_path = f"{dest_dir}/{path.name}"

        local_size = path.stat().st_size
        remote_size = existing_remote_files.get(dest_path)
        if remote_size is not None and remote_size == local_size:
            print(f"服务端已存在相同文件，跳过上传: {dest_path}")
            continue

        file_size_mb = local_size / (1024 * 1024)
        print(f"上传: {dest_path} ({file_size_mb:.1f} MB)...")
        result = client.upload(path, task_id=task_id, dest_path=dest_path)
        task_id = result["task_id"]
        print(f"  已上传, task_id={task_id}")

    # 持久化 task_id（保存到所有输入文件所在目录）
    _save_task_id_all(input_paths, task_id)

    # ── 2. 自动发现并上传同名 SRT 和翻译指南 ──────────────
    for input_path in args.inputs:
        srt_path = _find_matching_srt(Path(input_path))
        if srt_path:
            dest_dir = _compute_dest_dir(Path(input_path))
            srt_dest = f"{dest_dir}/{srt_path.name}"
            local_size = srt_path.stat().st_size
            remote_size = existing_remote_files.get(srt_dest)
            if remote_size is not None and remote_size == local_size:
                print(f"服务端已存在相同文件，跳过上传: {srt_dest}")
            else:
                print(f"上传辅助文件: {srt_dest}...")
                client.upload(srt_path, task_id=task_id, dest_path=srt_dest)

    if args.extra_translation_guideline:
        guideline_path = Path(args.extra_translation_guideline)
        if guideline_path.exists():
            local_size = guideline_path.stat().st_size
            remote_size = existing_remote_files.get(guideline_path.name)
            if remote_size is not None and remote_size == local_size:
                print(f"服务端已存在相同文件，跳过上传: {guideline_path.name}")
            else:
                print(f"上传翻译指南: {guideline_path.name}...")
                client.upload(guideline_path, task_id=task_id)

    # ── 2b. 编辑重跑预处理 ─────────────────────────────────
    if args.edit_rerun:
        if not task_id:
            print("[错误] 编辑重跑模式需要已有的 task_id，但未找到 .vt_task_id 文件。")
            print("  编辑重跑模式要求服务端之前已跑过此任务。如需新建任务，去掉 --edit-rerun 参数。")
            sys.exit(1)

        print("\n--- 编辑重跑预处理 ---")

        # 时间同步检查
        _check_server_time(client)

        # 验证服务端已有 segments 输出（list_files 检查）
        first_input = Path(args.inputs[0])
        dest_dir = _compute_dest_dir(first_input)
        segments_subdir = f"{dest_dir}/{SEGMENTS_DIRNAME}"
        try:
            result = client.list_files(task_id, sub_dir=segments_subdir, since=0)
            if not result.get("items"):
                print(f"[错误] 服务端 {segments_subdir} 不存在或为空，"
                      f"请确认任务 {task_id} 已完成过 ASR 阶段。")
                sys.exit(1)
        except Exception as e:
            print(f"[错误] 无法访问服务端 segments 目录: {e}")
            sys.exit(1)

        # 检查服务端无正在运行的任务
        try:
            running = client.status(task_id, since_line=0)
            if running.get("status") == "running":
                print(f"[错误] 任务 {task_id} 正在运行中，请等待完成后再编辑重跑。")
                sys.exit(1)
        except Exception:
            pass  # 查询失败不阻断

        # 解析目标语言
        from Common.language_map import normalize_target_language_codes
        target_codes = normalize_target_language_codes(args.targets) if args.targets else []

        # 执行编辑检测和变更
        _detect_and_apply_edits(client, task_id, input_paths, target_codes)

        print("--- 编辑重跑预处理完成 ---\n")

    # ── 3. 远程执行 ─────────────────────────────────────────
    remote_args = _build_remote_args(args)
    video_summary = _compute_video_summary([Path(p) for p in args.inputs])
    print(f"启动远程翻译 (client_mode)...")
    try:
        client.run('audio_translate.py', remote_args, task_id=task_id,
                   client_mode=True, video_summary=video_summary)
    except Exception as e:
        # 服务端可能因为一对一约束拒绝（409），此时给出提示
        if "409" in str(e) or "并发上限" in str(e):
            print(f"\n[错误] 服务端已有任务运行中，请等待或通过 Web UI 取消。")
            print(f"  task_id={task_id} 已保存，稍后可重跑此命令恢复。")
            sys.exit(1)
        raise

    # ── 4. 轮询 + 全量扫描下载 ──────────────────────────────
    last_sync_check = 0.0  # 同步频率控制：wall clock
    last_line = 0

    # 为每个输入文件计算本地目录和对应的子目录名
    input_sync_info = []
    for input_path in args.inputs:
        p = Path(input_path)
        input_sync_info.append((p.parent, _compute_dest_dir(p)))

    def on_progress(status_info):
        nonlocal last_sync_check, last_line

        # 打印增量日志
        stdout = status_info.get("stdout", "")
        if stdout:
            print(stdout, end='', flush=True)
        last_line = status_info.get("total_lines", last_line)

        # 每 3 秒全量扫描下载：确保之前下载失败的文件被重试
        now = time.time()
        if now - last_sync_check >= 3.0:
            try:
                for local_dir, dest_dir in input_sync_info:
                    _sync_files(
                        client, task_id, local_dir,
                        sub_dir=dest_dir, since=0
                    )
            except Exception:
                pass
            last_sync_check = now

    try:
        client.wait(task_id, poll_interval=2.0, on_progress=on_progress)
    except KeyboardInterrupt:
        # 捕获 Ctrl+C：通知服务端取消任务，然后退出
        print(f"\n\n中断信号收到，正在取消服务端任务 {task_id}...")
        try:
            result = client.cancel(task_id)
            status = result.get('status', 'unknown')
            print(f"任务已取消: {status}")
            # 容器重启在服务端进程内完成（按需重启当前正在使用的 GPU 容器）
        except Exception as e:
            print(f"取消任务失败: {e}")
        print(f"task_id={task_id} 已保存，重跑可恢复。")
        sys.exit(130)
    except RuntimeError as e:
        print(f"\n任务失败: {e}")
        sys.exit(1)
    except TimeoutError as e:
        print(f"\n任务超时: {e}")
        # 查询服务端任务当前状态，帮助用户判断是否仍在运行
        try:
            info = client.status(task_id)
            srv_status = info.get("status", "unknown")
            total_lines = info.get("total_lines", 0)
            if srv_status == "running":
                print(f"  服务端任务仍在运行 (已输出 {total_lines} 行)，可重新运行此命令恢复监听。")
                print(f"  task_id={task_id} 已保存。")
            else:
                print(f"  服务端任务状态: {srv_status}")
        except Exception:
            print(f"  无法查询服务端状态，task_id={task_id} 已保存，可重新运行此命令恢复。")
        sys.exit(1)

    # ── 5. 最终全量同步（带验证和重试） ─────────────────────
    MAX_SYNC_ROUNDS = 3
    all_missing = []
    for round_num in range(1, MAX_SYNC_ROUNDS + 1):
        # 全量扫描下载（首轮启用孤儿文件清理：删除服务端已改名/删除但本地仍残留的文件）
        for local_dir, dest_dir in input_sync_info:
            _sync_files(client, task_id, local_dir, sub_dir=dest_dir, since=0,
                        cleanup_stale=(round_num == 1))

        # 验证：检查服务端所有可见文件是否已同步到本地
        all_missing = []
        for local_dir, dest_dir in input_sync_info:
            missing = _verify_sync(client, task_id, local_dir, sub_dir=dest_dir)
            all_missing.extend(missing)

        if not all_missing:
            break

        if round_num < MAX_SYNC_ROUNDS:
            print(f"  仍有 {len(all_missing)} 个文件未同步完成，2秒后重试 (第{round_num+1}/{MAX_SYNC_ROUNDS}轮)...")
            time.sleep(2)

    if all_missing:
        print(f"\n[错误] 以下 {len(all_missing)} 个文件未能同步到本地：")
        for f in all_missing[:20]:
            print(f"  - {f}")
        if len(all_missing) > 20:
            print(f"  ...等共 {len(all_missing)} 个")
        print("文件同步不完整，无法进行后续视频合成。请重跑以恢复同步。")
        print(f"task_id={task_id} 已保存。")
        sys.exit(1)

    # ── 6. 最终清理 + 双向校验 ──────────────────────────────
    # 经过 3 轮同步重试后，再执行一次孤儿文件清理（删除本地有但服务端没有的过时文件），
    # 然后验证本地与服务端文件一一对应。
    # _verify_sync 已确保"服务端有的文件本地也有"，
    # 此步骤先删除"本地有但服务端没有"的残留文件，再做最终确认。
    for local_dir, dest_dir in input_sync_info:
        _sync_files(client, task_id, local_dir, sub_dir=dest_dir, since=0,
                    cleanup_stale=True)

    # 验证：清理后确认本地与服务端文件一一对应
    extra_local = []
    for local_dir, dest_dir in input_sync_info:
        extras = _find_local_only_files(client, task_id, local_dir, sub_dir=dest_dir)
        extra_local.extend(extras)

    if extra_local:
        print(f"\n[错误] 清理后仍有 {len(extra_local)} 个本地文件在服务端不存在：")
        for f in extra_local[:20]:
            print(f"  - {f}")
        if len(extra_local) > 20:
            print(f"  ...等共 {len(extra_local)} 个")
        print("客户端与服务端文件不一致，请重跑以恢复同步。")
        print(f"task_id={task_id} 已保存。")
        sys.exit(1)

    elapsed = time.time() - task_start_time
    mins, secs = divmod(int(elapsed), 60)
    hours, mins = divmod(mins, 60)
    print(f"所有文件同步完成，客户端与服务端文件一一对应。")
    print(f"任务处理总耗时: {hours}小时{mins}分{secs}秒")


if __name__ == '__main__':
    main()
