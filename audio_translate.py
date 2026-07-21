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


from Common.asr_languages import ALL_ASR_LANGUAGE_CODES
from Common.tts_languages import ALL_TTS_LANGUAGE_CODES


from remote_client import RemoteScriptClient, normalize_server_url

from edit_rerun import preprocess_edit_rerun


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


def _exit_task_not_found(task_id: str, input_paths: list[Path]):
    """task_id 在服务端不存在时报错退出。

    说明该任务的历史记录不在此服务器上（workspace 可能已被清理或此为另一台服务器）。
    """
    print(f"[错误] 已保存的 task_id={task_id} 在服务端不存在。")
    print(f"  该任务的历史记录不在此服务器上（workspace 可能已被清理或此为另一台服务器）。")
    print(f"  解决方法：")
    print(f"    1. 删除以下 .vt_task_id 文件后重试：")
    for p in input_paths:
        marker = Path(p).parent / TASK_ID_FILENAME
        print(f"       {marker}")
    print(f"    2. 或使用 --new-task 参数强制创建新任务")
    sys.exit(1)


def _validate_input_files_match(client: RemoteScriptClient, task_id: str,
                                 input_paths: list[Path]):
    """校验当前输入文件与服务端历史任务是否一致。

    如果服务端已有不同的媒体文件，说明用户在同一个 task_id 下换了视频/音频文件，
    会导致翻译中间结果互相覆盖。此时应报错退出。

    匹配规则（按文件名 stem 比较，不比较扩展名）：
      - stem 完全相同 → 同一源文件（如之前上传 .mp3，现在上传 .mp4）
      - 服务端文件 stem 以 "{input_stem}_" 开头 → 流水线派生文件
        （如 {stem}_vocals.mp3、{stem}_others.mp3、{stem}_vocals_denoised.mp3）
      - 以上都不匹配，且服务端有其他媒体文件 → 不同源，报错
    """
    media_exts = {'.mp3', '.mp4', '.wav', '.m4a', '.flac', '.aac', '.ogg'}
    for input_path in input_paths:
        p = Path(input_path)
        dest_dir = _compute_dest_dir(p)
        try:
            result = client.list_files(task_id, sub_dir=dest_dir, since=0)
            server_files = {item["name"] for item in result.get("items", [])
                           if item["type"] == "file"}
        except Exception:
            # 子目录不存在，跳过检查（可能是新目录）
            continue

        input_stem = p.stem
        server_stems = {Path(f).stem for f in server_files}

        # 同一源文件（扩展名可能不同，如 mp3 → mp4）
        if input_stem in server_stems:
            continue

        # 流水线派生文件（{stem}_vocals.mp3、{stem}_others.mp3 等）
        if any(s.startswith(f"{input_stem}_") for s in server_stems):
            continue

        # 当前文件不在服务端，检查是否有其他媒体文件（不同源视频/音频）
        server_media = {f for f in server_files
                       if Path(f).suffix.lower() in media_exts}
        if server_media:
            print(f"[错误] 输入文件与历史任务不匹配！")
            print(f"  当前输入: {dest_dir}/{p.name}")
            print(f"  服务端历史文件: {', '.join(sorted(server_media))}")
            print(f"  task_id={task_id} 对应的任务使用的是不同的视频/音频文件。")
            print(f"  每次任务的视频/音频文件必须在独立文件夹下，")
            print(f"  否则翻译中间结果会互相覆盖导致错乱。")
            print(f"  解决方法：将要翻译的视频/音频拷贝到新的独立目录下再执行。")
            sys.exit(1)









# ─── 辅助函数 ─────────────────────────────────────────────

def _compute_dest_dir(file_path: Path) -> str:
    """计算文件在服务端工作目录中的子目录名（直接用父目录名）。

    例如：E:\\...\\《逐玉》\\1.mp3 → "《逐玉》"

    多输入时，调用方应先通过 _validate_unique_parent_dirs 校验
    各输入文件的父目录名不重复，否则 segments 目录会冲突。
    """
    return file_path.resolve().parent.name


def _validate_unique_parent_dirs(input_paths: list[Path]):
    """校验多个输入文件的父目录名是否唯一。

    服务端用父目录名作为子目录名（_compute_dest_dir），
    如果两个不同路径的输入文件父目录名相同（如 ...\\A\\1.mp3 和 ...\\B\\A\\2.mp3），
    它们的 segments 会写到同一个子目录下，导致结果互相覆盖。

    检测到重名时直接 sys.exit(1) 提示用户。
    """
    if len(input_paths) <= 1:
        return

    name_to_paths: dict[str, list[Path]] = {}
    for p in input_paths:
        parent_name = p.resolve().parent.name
        name_to_paths.setdefault(parent_name, []).append(p)

    duplicates = {name: paths for name, paths in name_to_paths.items() if len(paths) > 1}
    if duplicates:
        print("[错误] 多个输入文件位于同名目录下，服务端子目录会冲突，segments 结果将互相覆盖！")
        for name, paths in duplicates.items():
            print(f"  目录名 \"{name}\":")
            for p in paths:
                print(f"    {p}")
        print("请将各视频/音频放入不同名称的目录后再运行。")
        sys.exit(1)


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

    # TTS 音频拉伸百分比限制
    remote_args.extend(['--tts-max-audio-slowdown-pct', str(args.tts_max_audio_slowdown_pct)])
    remote_args.extend(['--tts-max-audio-speedup-pct', str(args.tts_max_audio_speedup_pct)])

    # TTS 时长感知翻译：最小合格候选数量（服务端会限制 1~10）
    remote_args.extend(['--tts-aware-min-candidate-count', str(args.tts_aware_min_candidate_count)])

    # 视觉辅助说话人切分（默认关闭，仅在显式开启时透传）
    if getattr(args, 'enable_visual_diarization', False):
        remote_args.append('--enable-visual-diarization')

    # 编辑重跑模式：透传 --skip-asr 让服务端跳过 ASR 流程
    if getattr(args, 'edit_rerun', False):
        remote_args.append('--skip-asr')

    # 仅翻译模式：透传 --stop-after-translation 让服务端跳过 TTS / 合并 / 混音
    if getattr(args, 'stop_after_translation', False):
        remote_args.append('--stop-after-translation')

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


def _sync_files(client: RemoteScriptClient, task_id: str, local_dir: Path,
                sub_dir: str = "", since: float = 0,
                cleanup_stale: bool = False) -> float:
    """从服务端增量下载文件到本地目录，返回最新的 mtime。

    cleanup_stale=True 时，同步完成后删除本地过时文件
    （服务端已不存在的 .mp3/.txt/.srt，如 VAD 裁剪改名后的旧文件）。
    """
    result = client.list_files(task_id, sub_dir=sub_dir, since=since, with_hash=True)
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
            # 下载文件（size + mtime + hash 三级对比，避免重复下载）
            # 对比优先级：size → mtime → hash
            # mtime 检查：服务端重新生成文件后（如 edit-rerun 触发 TTS 重合成），
            # 即使 size 不变也能检测到并重新下载。但 mtime 受客户端/服务端时间差影响，
            # 可能误判 → hash 兜底：size 相同且 hash 相同则跳过，无论 mtime 如何。
            local_file = local_dir / name
            remote_path = f"{sub_dir}/{name}" if sub_dir else name
            remote_size = item.get("size", -1)
            remote_mtime = item.get("mtime", 0)
            remote_hash = item.get("hash")
            if local_file.exists() and remote_size >= 0:
                try:
                    local_stat = local_file.stat()
                    if local_stat.st_size == remote_size:
                        # size 匹配，检查 mtime
                        if remote_mtime > 0 and remote_mtime > local_stat.st_mtime:
                            # mtime 提示服务端文件更新，但可能是时间波动 → hash 兜底
                            if remote_hash:
                                local_hash = _compute_file_hash(local_file)
                                if local_hash == remote_hash:
                                    continue  # hash 相同，内容未变，跳过
                            print(f"  [更新] {remote_path} (服务端文件已更新，重新下载)")
                        else:
                            continue  # size 和 mtime 都匹配，跳过
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
        result = client.list_files(task_id, sub_dir=sub_dir, since=0, with_hash=True)
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
            remote_mtime = item.get("mtime", 0)
            remote_hash = item.get("hash")
            if not local_file.exists():
                missing.append(remote_path)
            elif remote_size >= 0:
                try:
                    local_stat = local_file.stat()
                    if local_stat.st_size != remote_size:
                        missing.append(f"{remote_path} (大小不匹配: 本地{local_stat.st_size} vs 服务端{remote_size})")
                    elif remote_mtime > 0 and remote_mtime > local_stat.st_mtime:
                        # size 匹配但 mtime 不同，可能是时间波动 → hash 兜底
                        if remote_hash:
                            local_hash = _compute_file_hash(local_file)
                            if local_hash == remote_hash:
                                continue  # hash 相同，内容未变，不算缺失
                        missing.append(f"{remote_path} (服务端文件已更新: mtime {remote_mtime:.0f} > 本地 {local_stat.st_mtime:.0f})")
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
    p.add_argument('--enable-visual-diarization', dest='enable_visual_diarization', action='store_true', default=False, help=argparse.SUPPRESS)
    
    p.add_argument('--translation-models', default='', help='用于翻译的逗号分隔模型列表。空值使用服务端默认值。')
    p.add_argument('--translation-mode', choices=['independent', 'tts_aware'], default='tts_aware', help='翻译模式: independent=纯文本独立翻译, tts_aware=TTS时长感知翻译（翻译+TTS试合成+时长评估+LLM反馈调整）。默认：tts_aware')
    p.add_argument('--extra-translation-guideline', help='包含额外翻译指南（e.g.定制化场景要求）的文本文件路径（可选参数）')
    p.add_argument('--tts-aware-max-retries', type=int, default=3, help='TTS时长感知模式中每句的最大时长调整重试次数（默认: 3）')
    p.add_argument('--tts-max-audio-slowdown-pct', type=float, default=0.2,
                   help='TTS 合成音频最大减速百分比（合成短于参考时拉伸上限）。默认: 0.2')
    p.add_argument('--tts-max-audio-speedup-pct', type=float, default=0.2,
                   help='TTS 合成音频最大加速百分比（合成长于参考时拉伸上限）。默认: 0.2')
    p.add_argument('--tts-aware-min-candidate-count', type=int, default=3,
                   help='每个片段至少保留的合格候选音频数量（1-10）。默认: 3')
    p.add_argument('--stop-after-translation', action='store_true',
                   help='翻译完成后停止流水线，跳过 TTS / 音频合并 / 最终混音。'
                        '翻译完成后始终生成 full_translation.srt 字幕文件（无论是否启用此参数）。'
                        '核心用途：翻译文本后人工介入检查，核查字幕内容和翻译指南，确认无误后再继续后续流程。')
                   
    p.add_argument('--server', default='localhost', help='服务端 IP 地址 (默认: localhost)')
    p.add_argument('--new-task', action='store_true', help='忽略本地保存的 task_id，强制创建新任务。')
    p.add_argument('--edit-rerun', action='store_true',
                   help='编辑重跑模式：检测本地编辑（改ASR/改翻译/替换合成音频/删语种/删mp3/删txt），'
                        '上传修改的文件并删除服务端对应的下游产物，服务端跳过ASR直接从翻译开始。'
                        '要求服务端已有该任务的运行记录。')
    # 内部参数：仅供客户端 video_translate.py 透传给服务端；不在 --help 中展示，
    # 终端用户不应通过 audio_translate 的命令行使用此开关（它要求输入是视频，与音频翻译入口职责冲突）。
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

    # 校验多输入时父目录名不重复（否则服务端子目录冲突）
    _validate_unique_parent_dirs(input_paths)

    task_id = None
    if not args.new_task:
        task_id = _validate_task_ids(input_paths)
        if task_id:
            if client.task_exists(task_id):
                print(f"发现已保存的 task_id={task_id}，复用该任务继续跑...")
                # 校验输入文件与历史任务是否一致（防止换视频导致中间结果覆盖）
                _validate_input_files_match(client, task_id, input_paths)
            else:
                _exit_task_not_found(task_id, input_paths)

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
        preprocess_edit_rerun(
            client=client,
            task_id=task_id,
            input_paths=input_paths,
            target_languages=args.targets,
            compute_dest_dir=_compute_dest_dir,
        )

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
