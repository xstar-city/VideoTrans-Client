#!/usr/bin/env python3
"""视频音色替换客户端。

将视频中的音色替换为参考音频的音色，保持音频时间卡点。
采用"本地视频操作 + 远程音频处理"混合架构（与 video_translate.py 一致）：

    video_voice_replace.py
      ├── Step 1: extract_audio_ffmpeg()       # 本地 ffmpeg 抽取 mp3
      ├── Step 2: upload mp3 + ref_audio       # 上传到服务端
      ├── Step 3: run voice_replace.py          # 远程执行音色替换管线
      ├── Step 4: sync download                 # 增量下载结果
      └── Step 5: mux_audio_into_video()        # 本地 ffmpeg 替换视频音轨

用法：
    python video_voice_replace.py "video.mp4" --ref ref_audio.wav --server <ServerIP>
    python video_voice_replace.py "video.mp4" --ref ref_audio.wav -s zh --server <IP>
    python video_voice_replace.py "a.mp4" "b.mp4" --ref ref.wav --server <IP>

    # 编辑重跑：修改 ASR 文本或删除合成音频后，加 --edit-rerun 重跑
    python video_voice_replace.py "video.mp4" --ref ref_audio.wav --server <IP> --edit-rerun

断点续跑（task_id 机制）：
    与 audio_translate.py 一致，首次运行会在视频目录生成 .vt_task_id 文件，
    再次运行时复用该 task_id，服务端缓存检查自动跳过已完成步骤。
    如需强制从零开始，使用 --new-task 参数。

编辑重跑（--edit-rerun）：
    首次运行完成后，可编辑本地 segments/ 目录下的文件，加 --edit-rerun 重跑：
    - 改 ASR 文本：编辑 segments/ASR/{stem}.txt -> 上传 + 删除对应 TTS 产物 -> 重新合成
    - 删合成音频：删除 segments/{lang}/{stem}.mp3 -> 删除服务端对应文件 -> 重新合成
    服务端各步骤有缓存跳过逻辑，仅重跑受影响的部分。

依赖：
    - ffmpeg / ffprobe 在 PATH 中可用
    - requests（pip install requests）
    - remote_client.py, audio_translate.py, edit_rerun.py（复用 task_id 管理和编辑检测逻辑）
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from Common.asr_languages import ALL_ASR_LANGUAGE_CODES
from Common.config import (
    ASR_DIRNAME,
    COMBINED_AUDIO_FILENAME,
    FINAL_AUDIO_FILENAME,
    SEGMENTS_DIRNAME,
    build_segments_dir,
)
from Common.language_map import get_language_dir_name
from Common.video_utils import (
    extract_audio_ffmpeg,
    mux_audio_into_video,
)
from remote_client import RemoteScriptClient, resolve_server_arg

# 复用 audio_translate.py 的 task_id 管理和文件同步逻辑
from audio_translate import (
    _save_task_id_all,
    _load_task_id,
    _validate_task_ids,
    _validate_unique_parent_dirs,
    _compute_dest_dir,
    _compute_video_summary,
    _sync_files,
    _verify_sync,
    _find_local_only_files,
    _exit_task_not_found,
    _validate_input_files_match,
)

# 复用 edit_rerun.py 的编辑检测辅助函数
from edit_rerun import (
    _ASR_NON_TXT_FILES,
    _check_server_time,
    _download_server_txt,
    _print_txt_diff,
)


def _log(msg: str):
    """带时间戳的日志输出，用于关键节点。"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {msg}", flush=True)


# ─── 辅助函数 ─────────────────────────────────────────────

def _build_remote_args(args, ref_audio_filename: str) -> list[str]:
    """将客户端参数转换为服务端 voice_replace.py 的命令行参数。"""
    remote_args = []

    # 输入音频文件名（含子目录路径，服务端 cwd 为 workspace/{task_id}/）
    for input_path in args.inputs:
        p = Path(input_path)
        dest_dir = _compute_dest_dir(p)
        remote_args.append(f"{dest_dir}/{p.name}")

    # 参考音频（上传到 task 工作目录根，服务端用相对路径访问）
    remote_args.extend(['--ref', ref_audio_filename])

    # 源语言
    remote_args.extend(['--source', args.source])

    # 人声分离：默认关闭；只在用户显式开启时透传 --separate
    if args.separate:
        remote_args.append('--separate')

    # 降噪
    remote_args.extend(['--denoise', args.denoise])

    return remote_args


# ─── 编辑重跑（简化版 edit_rerun）─────────────────────────

def _delete_local_voice_replace_artifacts(local_lang_dir: Path, stem: str):
    """删除本地语言目录下指定 stem 的 TTS 产物（mp3 + txt + 候选目录）。"""
    for ext in ('.mp3', '.txt'):
        f = local_lang_dir / f"{stem}{ext}"
        if f.exists():
            f.unlink()
    cand_dir = local_lang_dir / '_dubbing_candidates' / stem
    if cand_dir.is_dir():
        shutil.rmtree(cand_dir, ignore_errors=True)


def _delete_local_combined_final(local_lang_dir: Path):
    """删除本地语言目录下的 combined.mp3 和 final.mp3。"""
    for fname in (COMBINED_AUDIO_FILENAME, FINAL_AUDIO_FILENAME):
        f = local_lang_dir / fname
        if f.exists():
            f.unlink()


def _detect_and_apply_edits_voice_replace(
    client: RemoteScriptClient,
    task_id: str,
    input_paths: list[Path],
    source: str,
):
    """简化版编辑重跑：检测 ASR 文本修改和合成音频删除。

    支持的编辑场景：
    1. 改 ASR 文本：编辑 segments/ASR/{stem}.txt
       -> 上传新 txt + 删除语言目录下 txt/mp3/候选目录 + combined/final
    2. 删合成音频：删除 segments/{lang}/{stem}.mp3
       -> 删除服务端对应 mp3 + combined/final + 候选目录

    服务端各步骤均有缓存跳过逻辑，删除对应文件即可触发重跑。
    """
    lang_dir_name = get_language_dir_name(source)

    upload_list: list[tuple[Path, str]] = []   # (本地文件路径, 服务端相对路径)
    delete_files: set[str] = set()
    delete_dirs: set[str] = set()

    for input_path in input_paths:
        p = Path(input_path)
        dest_dir = _compute_dest_dir(p)
        local_segments_dir = build_segments_dir(p)

        server_segments_subdir = f"{dest_dir}/{SEGMENTS_DIRNAME}"
        server_asr_subdir = f"{server_segments_subdir}/{ASR_DIRNAME}"
        server_lang_subdir = f"{server_segments_subdir}/{lang_dir_name}"
        local_lang_dir = local_segments_dir / lang_dir_name

        def _list_server_files(sub_dir: str, with_hash: bool = False) -> list[dict]:
            try:
                result = client.list_files(task_id, sub_dir=sub_dir, since=0,
                                           with_hash=with_hash)
                return result.get("items", [])
            except Exception:
                return []

        # ── 场景 1：检测 ASR 文本内容修改 ──
        server_asr_items = _list_server_files(server_asr_subdir)
        server_asr_txts: set[str] = set()
        for item in server_asr_items:
            if item["type"] == "file" and item["name"].endswith(".txt"):
                if item["name"] not in _ASR_NON_TXT_FILES:
                    server_asr_txts.add(item["name"])

        local_asr_dir = local_segments_dir / ASR_DIRNAME
        changed_stems: set[str] = set()

        if local_asr_dir.exists():
            for asr_txt_name in sorted(server_asr_txts):
                local_asr_txt = local_asr_dir / asr_txt_name
                if not local_asr_txt.exists():
                    continue

                remote_asr_path = f"{server_asr_subdir}/{asr_txt_name}"
                server_content = _download_server_txt(client, task_id, remote_asr_path)
                local_content = local_asr_txt.read_text(encoding="utf-8")

                if server_content is not None and server_content != local_content:
                    stem = asr_txt_name.rsplit(".", 1)[0]
                    changed_stems.add(stem)
                    upload_list.append((local_asr_txt, remote_asr_path))
                    print(f"  [改ASR文本] {asr_txt_name} 内容已修改")
                    _print_txt_diff(local_content, server_content, f"ASR/{asr_txt_name}")

        # 删除受影响 stem 的下游产物（语言目录下 txt + mp3 + 候选 + combined + final）
        for stem in changed_stems:
            delete_files.add(f"{server_lang_subdir}/{stem}.txt")
            delete_files.add(f"{server_lang_subdir}/{stem}.mp3")
            delete_dirs.add(f"{server_lang_subdir}/_dubbing_candidates/{stem}")
            delete_files.add(f"{server_lang_subdir}/{COMBINED_AUDIO_FILENAME}")
            delete_files.add(f"{server_lang_subdir}/{FINAL_AUDIO_FILENAME}")
            # 同步删除客户端本地文件
            if local_lang_dir.exists():
                _delete_local_voice_replace_artifacts(local_lang_dir, stem)
                _delete_local_combined_final(local_lang_dir)

        # ── 场景 2：检测合成音频 mp3 被删除 ──
        if local_lang_dir.exists():
            server_lang_items = _list_server_files(server_lang_subdir, with_hash=True)
            server_lang_files = {
                item["name"]: item for item in server_lang_items
                if item["type"] == "file"
            }

            for server_file in server_lang_files:
                if server_file.startswith('.') or server_file.startswith('_'):
                    continue
                # 跳过 combined.mp3 / final.mp3（非逐句合成音频）
                if server_file in (COMBINED_AUDIO_FILENAME, FINAL_AUDIO_FILENAME):
                    continue
                if not server_file.endswith(".mp3"):
                    continue

                local_file = local_lang_dir / server_file
                if not local_file.exists():
                    stem = server_file.rsplit(".", 1)[0]
                    delete_files.add(f"{server_lang_subdir}/{server_file}")
                    delete_files.add(f"{server_lang_subdir}/{COMBINED_AUDIO_FILENAME}")
                    delete_files.add(f"{server_lang_subdir}/{FINAL_AUDIO_FILENAME}")
                    delete_dirs.add(f"{server_lang_subdir}/_dubbing_candidates/{stem}")
                    print(f"  [删合成音频] {lang_dir_name}/{server_file} 本地已删除 \u2192 删除合成音频+combined+final")

    # ── 执行上传 ──
    if upload_list:
        print(f"\n上传 {len(upload_list)} 个修改的文件...")
        for local_file, remote_path in upload_list:
            try:
                client.upload(local_file, task_id=task_id, dest_path=remote_path)
                print(f"  已上传: {remote_path}")
                local_file.touch()
            except Exception as e:
                print(f"  [错误] 上传失败 {remote_path}: {e}")

    # ── 执行删除 ──
    if delete_files or delete_dirs:
        print(f"\n删除 {len(delete_files)} 个文件 + {len(delete_dirs)} 个目录...")
        try:
            result = client.delete_files(
                task_id,
                files=list(delete_files),
                dirs=list(delete_dirs),
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


# ─── 主流程 ────────────────────────────────────────────────

def process_voice_replace_pipeline(
    video_paths: list[Path],
    ref_audio_path: Path,
    server_url: str,
    *,
    source: str = "zh",
    separate: bool = False,
    denoise: str = "aggressive",
    new_task: bool = False,
    edit_rerun: bool = False,
):
    """视频音色替换主流程：提取音频 -> 上传 -> 远程替换 -> 下载 -> mux。"""
    task_start_time = time.time()

    client = RemoteScriptClient(server_url)

    # ── 预检：验证服务端可达 ──────────────────────────────────
    try:
        client.check_server()
    except ConnectionError as e:
        _log(f"[错误] {e}")
        sys.exit(1)

    # ── Step 1: 提取音频 ─────────────────────────────────────
    _log("--- Step 1: 提取音频 ---")
    video_data: dict[Path, dict] = {}

    for video_path in video_paths:
        if not video_path.exists():
            print(f"文件不存在: {video_path}，跳过")
            continue

        # 检查输出是否已存在
        out_video = video_path.with_name(f"{video_path.stem}_voice_replaced{video_path.suffix}")
        if out_video.exists():
            print(f"输出已存在，跳过: {out_video}")
            continue

        # 提取 mp3（视频输入时）
        suffix = video_path.suffix.lower()
        from Common.config import VIDEO_CONTAINER_SUFFIXES
        if suffix in VIDEO_CONTAINER_SUFFIXES:
            mp3_path = video_path.with_suffix(".mp3")
            if mp3_path.exists():
                print(f"音频已存在: {mp3_path}，跳过提取。")
            else:
                try:
                    print(f"提取音频: {video_path.name}...")
                    extract_audio_ffmpeg(video_path, mp3_path)
                except Exception as e:
                    print(f"提取音频失败 {video_path}: {e}")
                    continue
            video_data[video_path] = {"upload": mp3_path}
        else:
            # 纯音频输入，直接上传
            video_data[video_path] = {"upload": video_path}

    if not video_data:
        print("无待处理文件。")
        return

    # ── Step 2: 上传文件 ─────────────────────────────────────
    _log("--- Step 2: 上传文件 ---")

    # 参考音频上传到 task 工作目录根（服务端 voice_replace.py --ref 参数用相对路径访问）
    ref_filename = ref_audio_path.name

    # task_id 管理（复用 audio_translate.py 逻辑）
    input_paths = [data["upload"] for data in video_data.values()]

    # 校验多输入时父目录名不重复（否则服务端子目录冲突）
    _validate_unique_parent_dirs(input_paths)

    task_id = None
    if not new_task:
        task_id = _validate_task_ids(input_paths)
        if task_id:
            if client.task_exists(task_id):
                _log(f"发现已保存的 task_id={task_id}，复用该任务继续跑...")
                # 校验输入文件与历史任务是否一致（防止换视频导致中间结果覆盖）
                _validate_input_files_match(client, task_id, input_paths)
            else:
                _exit_task_not_found(task_id, input_paths)

    # 查询服务端已有文件（老 task_id 时跳过已上传的）
    existing_remote_files: dict[str, int] = {}
    if task_id:
        try:
            result = client.list_files(task_id, sub_dir="", since=0)
            for item in result.get("items", []):
                if item["type"] == "file":
                    size = item.get("size", -1)
                    if size >= 0:
                        existing_remote_files[item["name"]] = size
        except Exception:
            pass
        for input_path in input_paths:
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
                pass

    # 上传输入音频
    for video_path, data in video_data.items():
        path = data["upload"]
        if not path.exists():
            print(f"文件不存在: {path}")
            continue

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

    # 上传参考音频到根目录
    ref_dest = ref_filename
    ref_local_size = ref_audio_path.stat().st_size
    ref_remote_size = existing_remote_files.get(ref_dest)
    if ref_remote_size is not None and ref_remote_size == ref_local_size:
        print(f"服务端已存在相同参考音频，跳过上传: {ref_dest}")
    else:
        print(f"上传参考音频: {ref_dest}...")
        result = client.upload(ref_audio_path, task_id=task_id, dest_path=ref_dest)
        task_id = result["task_id"]
        print(f"  已上传, task_id={task_id}")

    # 持久化 task_id
    _save_task_id_all(input_paths, task_id)

    # ── 编辑重跑预处理（可选）────────────────────────────────
    if edit_rerun:
        if not task_id:
            _log("[错误] 编辑重跑模式需要已有的 task_id，但未找到 .vt_task_id 文件。")
            sys.exit(1)

        _log("--- 编辑重跑预处理 ---")
        _check_server_time(client)

        # 验证服务端已有 segments 输出
        first_input = input_paths[0]
        dest_dir = _compute_dest_dir(Path(first_input))
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
            pass

        # 执行编辑检测和变更
        _detect_and_apply_edits_voice_replace(client, task_id, input_paths, source)
        _log("--- 编辑重跑预处理完成 ---")

    # ── Step 3: 远程执行 ─────────────────────────────────────
    _log("--- Step 3: 远程音色替换 ---")

    # 构建服务端参数
    class _Args:
        """临时参数容器，供 _build_remote_args 使用。"""
        pass
    args = _Args()
    args.inputs = [str(data["upload"]) for data in video_data.values()]
    args.source = source
    args.separate = separate
    args.denoise = denoise

    remote_args = _build_remote_args(args, ref_filename)
    video_summary = _compute_video_summary([Path(p) for p in args.inputs])
    _log("启动远程音色替换 (client_mode)...")

    try:
        client.run('voice_replace.py', remote_args, task_id=task_id,
                   client_mode=True, video_summary=video_summary)
    except Exception as e:
        if "409" in str(e) or "并发上限" in str(e):
            _log("[错误] 服务端已有任务运行中，请等待或通过 Web UI 取消。")
            print(f"  task_id={task_id} 已保存，稍后可重跑此命令恢复。")
            sys.exit(1)
        raise

    # ── Step 4: 轮询 + 增量下载 ──────────────────────────────
    last_sync_check = 0.0
    input_sync_info = []
    for video_path, data in video_data.items():
        p = data["upload"]
        input_sync_info.append((p.parent, _compute_dest_dir(p)))

    def on_progress(status_info):
        nonlocal last_sync_check
        stdout = status_info.get("stdout", "")
        if stdout:
            print(stdout, end='', flush=True)

        now = time.time()
        if now - last_sync_check >= 3.0:
            try:
                for local_dir, dest_dir in input_sync_info:
                    _sync_files(client, task_id, local_dir, sub_dir=dest_dir, since=0)
            except Exception:
                pass
            last_sync_check = now

    try:
        client.wait(task_id, poll_interval=2.0, on_progress=on_progress)
    except KeyboardInterrupt:
        _log(f"中断信号收到，正在取消服务端任务 {task_id}...")
        try:
            result = client.cancel(task_id)
            status = result.get('status', 'unknown')
            _log(f"任务已取消: {status}")
        except Exception as e:
            print(f"取消任务失败: {e}")
        _log(f"task_id={task_id} 已保存，重跑可恢复。")
        sys.exit(130)
    except RuntimeError as e:
        _log(f"任务失败: {e}")
        sys.exit(1)
    except TimeoutError as e:
        _log(f"任务超时: {e}")
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

    # ── 最终全量同步 ─────────────────────────────────────────
    _log("--- Step 4: 下载结果 ---")
    MAX_SYNC_ROUNDS = 3
    all_missing = []
    for round_num in range(1, MAX_SYNC_ROUNDS + 1):
        for local_dir, dest_dir in input_sync_info:
            _sync_files(client, task_id, local_dir, sub_dir=dest_dir, since=0,
                        cleanup_stale=(round_num == 1))

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
        _log(f"[错误] 以下 {len(all_missing)} 个文件未能同步到本地：")
        for f in all_missing[:20]:
            print(f"  - {f}")
        if len(all_missing) > 20:
            print(f"  ...等共 {len(all_missing)} 个")
        print(f"task_id={task_id} 已保存。")
        sys.exit(1)

    # 清理孤儿文件
    for local_dir, dest_dir in input_sync_info:
        _sync_files(client, task_id, local_dir, sub_dir=dest_dir, since=0, cleanup_stale=True)

    # ── Step 5: 替换视频音轨 ─────────────────────────────────
    _log("--- Step 5: 替换视频音轨 ---")

    lang_dir_name = get_language_dir_name(source)

    for video_path, data in video_data.items():
        try:
            out_video = video_path.with_name(f"{video_path.stem}_voice_replaced{video_path.suffix}")
            if out_video.exists():
                print(f"输出已存在，跳过: {out_video}")
                continue

            mp3_path = data["upload"]
            segments_dir = build_segments_dir(mp3_path)
            lang_dir = segments_dir / lang_dir_name
            final_audio = lang_dir / FINAL_AUDIO_FILENAME

            # 若无 final.mp3（未分离背景音），回退到 combined.mp3
            if not final_audio.exists():
                final_audio = lang_dir / COMBINED_AUDIO_FILENAME
                print(f"  未找到 final.mp3，使用 combined.mp3: {final_audio.name}")

            if not final_audio.exists():
                print(f"合成音频未找到 ({video_path.name})，跳过音轨替换")
                continue

            print(f"  替换音轨: {video_path.name} + {final_audio.name} \u2192 {out_video.name}...")
            mux_audio_into_video(video_path, final_audio, out_video)
            print(f"  音色替换视频已保存: {out_video}")

        except Exception as e:
            print(f"[错误] 处理 {video_path.name} 失败: {e}")

    elapsed = time.time() - task_start_time
    mins, secs = divmod(int(elapsed), 60)
    hours, mins = divmod(mins, 60)
    _log(f"任务处理总耗时: {hours}小时{mins}分{secs}秒")


# ============================================================
# 命令行入口
# ============================================================

def main():
    p = argparse.ArgumentParser(
        description='视频音色替换：提取音频 -> 远程 ASR + TTS 语音克隆 -> 本地替换视频音轨。'
                    '使用统一参考音频做语音克隆，保持音频时间卡点。'
    )
    p.add_argument('inputs', nargs='+', help='本地视频文件路径列表（如一个或多个 mp4）。也支持纯音频 mp3/wav。')
    p.add_argument('--ref', required=True, help='参考音频文件路径（用于语音克隆的音色参考）')
    p.add_argument('--source', '-s', default='zh', choices=ALL_ASR_LANGUAGE_CODES,
                   help='输入音频的源语言代码（例如 en, zh），默认：zh')
    p.add_argument('--separate', action=argparse.BooleanOptionalAction, default=False,
                   help='是否运行人声分离以去除背景音。默认关闭；传 --separate 开启。'
                        '开启后 TTS 合成人声会与背景音混音输出 final.mp3；关闭时直接使用 combined.mp3。')
    p.add_argument('--denoise', choices=['none', 'normal', 'aggressive'], default='aggressive',
                   help='降噪类型（需要人声分离）。默认：aggressive')
    server_group = p.add_mutually_exclusive_group()
    server_group.add_argument('--server', default='localhost',
                              help='服务端地址（直连模式），支持 IP、域名或完整 URL。默认: localhost')
    server_group.add_argument('--scheduler', default=None,
                              help='调度器地址（IP/域名/URL），指定后由调度器自动分配空闲服务端。'
                                   '与 --server 互斥。')
    p.add_argument('--new-task', action='store_true', help='忽略本地保存的 task_id，强制创建新任务。')
    p.add_argument('--edit-rerun', action='store_true',
                   help='编辑重跑模式：检测本地对 segments/ 的编辑（改 ASR 文本、删合成音频 mp3），'
                        '同步到服务端后仅重跑受影响的部分。要求已有 task_id。')

    args = p.parse_args()

    # 验证参考音频
    ref_audio_path = Path(args.ref).resolve()
    if not ref_audio_path.exists():
        _log(f"参考音频文件不存在: {ref_audio_path}")
        sys.exit(1)

    # 解析输入路径
    video_paths = [Path(p) for p in args.inputs]
    video_paths = [p for p in video_paths if p.exists()]
    if not video_paths:
        _log("未找到有效的输入文件")
        sys.exit(1)

    # 检查每个视频是否在独立目录中
    dir_to_videos: dict[str, list[Path]] = {}
    for vp in video_paths:
        dir_key = str(vp.resolve().parent)
        dir_to_videos.setdefault(dir_key, []).append(vp)
    multi_video_dirs = {d: vps for d, vps in dir_to_videos.items() if len(vps) > 1}
    if multi_video_dirs:
        _log('[错误] 以下目录中包含多个输入文件，违反"每个文件独立目录"规则：')
        for dir_path, vps in multi_video_dirs.items():
            print(f"  目录: {dir_path}")
            for vp in vps:
                print(f"    - {vp.name}")
        print('请将每个文件移到独立的子目录中，避免中间文件互相覆盖。')
        sys.exit(1)

    # 解析服务端地址：--scheduler 由调度器分配空闲节点，--server 直连（老模式）
    try:
        server_url = resolve_server_arg(args.server, scheduler=args.scheduler)
    except (ConnectionError, RuntimeError) as e:
        _log(f"[错误] {e}")
        sys.exit(1)

    process_voice_replace_pipeline(
        video_paths,
        ref_audio_path,
        server_url,
        source=args.source,
        separate=args.separate,
        denoise=args.denoise,
        new_task=args.new_task,
        edit_rerun=args.edit_rerun,
    )


if __name__ == '__main__':
    main()
