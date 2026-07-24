#!/usr/bin/env python3
"""批量视频翻译客户端。

扫描本地目录中的视频文件，将所有视频一次性传给 video_translate.process_video_pipeline()
进行翻译（提取音频 -> 远程翻译 -> 本地合并音轨）。核心优化：所有视频在单次流水线调用中
完成，避免模型反复加载/卸载。

与 video_translate.py 的区别：
    - 输入为目录（--input-dir）而非文件列表
    - 递归扫描目录，自动发现视频文件
    - 校验：独立目录、目录名唯一、SRT 缺失警告
    - 视频时长统计 + 批量计时报告

调用链：
    batch_video_translate.py
      ├── 目录扫描 + 校验
      └── video_translate.process_video_pipeline()  # 直接 import 调用
            ├── Step 1: 本地提取音频 / 压缩视频
            ├── Step 2: audio_translate.main()      # 远程翻译（上传+执行+下载）
            ├── Step 3: mux_audio_into_video()       # 本地合并音轨（禁止伸缩）
            └── US3 归档

依赖：
    - ffmpeg / ffprobe 在 PATH 中可用
    - requests（audio_translate.py 依赖）
    - Common/ 下的共享模块

示例：
    python batch_video_translate.py "E:\\短剧\\《逐玉》" -t en --server <ServerIP>
    python batch_video_translate.py "D:\\videos" -t en ja --scheduler <SchedulerIP> -v
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter

from Common.asr_languages import ALL_ASR_LANGUAGE_CODES
from Common.config import VIDEO_CONTAINER_SUFFIXES
from Common.language_map import normalize_target_language_codes
from Common.tts_languages import ALL_TTS_LANGUAGE_CODES
from Common.config import PIPELINE_DERIVED_STEM_MARKERS
from Common.video_utils import get_video_duration
from remote_client import resolve_server_arg


# ============================================================
# 常量
# ============================================================

# 排除流水线派生文件（如 _translated_en.mp4、_vocals.wav、_upload_480p.mp4 等）
# 统一引用 Common 中的 PIPELINE_DERIVED_STEM_MARKERS，新增派生类型时只需在 Common 中添加
_IGNORED_STEM_MARKERS = PIPELINE_DERIVED_STEM_MARKERS

# video pipeline 默认翻译模型（与 video_translate.py 保持一致）
DEFAULT_MODELS = [
    'doubao-seed-2-1-turbo',
    'deepseek-v4-pro',
    'doubao-seed-2-1-pro',
    'gemini-3.5-flash',
]


# ============================================================
# 辅助函数
# ============================================================

def _log(msg: str):
    """带时间戳的日志输出，用于关键节点。"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {msg}", flush=True)


def _is_candidate_video(path: Path) -> bool:
    """判断是否为待翻译的候选视频文件（排除流水线派生文件）。"""
    if not path.is_file():
        return False
    if path.suffix.lower() not in VIDEO_CONTAINER_SUFFIXES:
        return False
    stem_lower = path.stem.lower()
    return not any(marker in stem_lower for marker in _IGNORED_STEM_MARKERS)


def _format_seconds(seconds: float) -> str:
    """将秒数格式化为 HH:MM:SS。"""
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# ============================================================
# 目录扫描与校验
# ============================================================

def scan_videos_recursive(root_dir: Path) -> list[Path]:
    """递归扫描目录，发现所有候选视频文件。

    返回按路径排序的视频列表。
    """
    videos: list[Path] = []
    for path in sorted(root_dir.rglob("*")):
        if _is_candidate_video(path):
            videos.append(path)
    return videos


def dedup_stem_variants(videos: list[Path]) -> list[Path]:
    """对同目录下的 stem 变种视频去重，保留最短 stem（即原始视频）。

    场景：同一目录下可能有原视频及其派生文件（如 xxx_upload_480p.mp4、
    xxx_translated_en.mp4）。虽然 PIPELINE_DERIVED_STEM_MARKERS 已过滤已知派生模式，
    但可能出现未登记的变种命名。此函数作为兜底：如果同目录下多个候选视频的 stem
    存在"前缀 + _"关系，则视为同源变种，只保留 stem 最短的那个（原始视频）。

    例如：家里家外1.mp4 + 家里家外1_edited.mp4 -> 保留 家里家外1.mp4
    """
    # 按父目录分组
    dir_to_videos: dict[str, list[Path]] = {}
    for vp in videos:
        dir_key = str(vp.resolve().parent)
        dir_to_videos.setdefault(dir_key, []).append(vp)

    result: list[Path] = []
    for dir_key, vps in dir_to_videos.items():
        if len(vps) <= 1:
            result.extend(vps)
            continue

        # 按 stem 长度排序，最短的在前
        sorted_vps = sorted(vps, key=lambda p: len(p.stem))
        base = sorted_vps[0]
        base_stem = base.stem.lower()

        # 检查所有其他视频是否都是 base 的变种（stem 以 base_stem + "_" 开头）
        all_variants = all(
            vp.stem.lower().startswith(base_stem + "_")
            for vp in sorted_vps[1:]
        )

        if all_variants:
            # 保留 base，警告变种被跳过
            variants_str = ", ".join(vp.name for vp in sorted_vps[1:])
            print(f"[跳过] {base.parent.name}/: 检测到 stem 变种，"
                  f"保留 {base.name}，跳过 {variants_str}")
            result.append(base)
        else:
            # 存在真正不同的视频，全部保留（由 validate_independent_dirs 报错）
            result.extend(vps)

    return sorted(result)


def validate_independent_dirs(videos: list[Path]) -> None:
    """校验每个视频的父目录只含这一个视频文件。

    不满足则报错退出，提示使用 Tools/organize_videos_into_folders.py 整理。
    """
    # 按父目录分组
    dir_to_videos: dict[str, list[Path]] = {}
    for vp in videos:
        dir_key = str(vp.resolve().parent)
        dir_to_videos.setdefault(dir_key, []).append(vp)

    multi_video_dirs = {d: vps for d, vps in dir_to_videos.items() if len(vps) > 1}
    if multi_video_dirs:
        _log('[错误] 以下目录中包含多个视频文件，违反"每个视频独立目录"规则：')
        for dir_path, vps in multi_video_dirs.items():
            print(f"  目录: {dir_path}")
            for vp in vps:
                print(f"    - {vp.name}")
        print()
        print('每个视频文件必须在独立目录下，且同一批量任务中各目录名不能重复，')
        print('否则翻译中间结果（segments/、.vt_task_id 等）会互相覆盖导致错乱。')
        print()
        print('推荐做法：将每集放入独立子目录，目录名和文件名用集号对应，如：')
        print('  短剧/1/1.mp4')
        print('  短剧/2/2.mp4')
        print('  短剧/3/3.mp4')
        print()
        print('可使用 Tools/organize_videos_into_folders.py 自动整理视频到独立目录。')
        sys.exit(1)


def validate_unique_dir_names(videos: list[Path]) -> None:
    """校验所有视频父目录名不重复。

    服务端用父目录名作为子目录名（_compute_dest_dir），目录名冲突会导致
    segments 结果互相覆盖。冲突则报错退出，建议缩小范围。
    """
    name_to_paths: dict[str, list[Path]] = {}
    for vp in videos:
        parent_name = vp.resolve().parent.name
        name_to_paths.setdefault(parent_name, []).append(vp)

    duplicates = {name: paths for name, paths in name_to_paths.items() if len(paths) > 1}
    if duplicates:
        _log("[错误] 多个视频的目录名重复，服务端子目录会冲突，segments 结果将互相覆盖！")
        for name, paths in duplicates.items():
            print(f'  目录名 "{name}":')
            for p in paths:
                print(f"    {p}")
        print()
        print('每个视频的目录名在同一个批量任务中必须唯一。')
        print('建议缩小范围，例如只跑某一季（season1）下的视频，避免不同季同名目录冲突。')
        sys.exit(1)


def check_srt_files(videos: list[Path]) -> None:
    """检查每个视频目录下是否有同 stem 的 SRT 文件，缺失则警告。

    SRT 缺失不影响翻译，只影响 ASR 校准精度。
    """
    missing: list[Path] = []
    for vp in videos:
        srt_path = vp.with_suffix(".srt")
        if not srt_path.exists():
            # 也检查大写扩展名
            srt_path_upper = vp.with_suffix(".SRT")
            if not srt_path_upper.exists():
                missing.append(vp)

    if missing:
        print(f"[警告] 以下 {len(missing)} 个视频缺少同名 SRT 字幕文件（不影响翻译，只影响 ASR 校准精度）：")
        for vp in missing[:10]:
            print(f"  {vp.parent.name}/{vp.name}")
        if len(missing) > 10:
            print(f"  ... 还有 {len(missing) - 10} 个")
        print()


# ============================================================
# 视频时长统计
# ============================================================

def print_video_summary(videos: list[Path]) -> float:
    """打印视频时长统计，返回总时长（秒）。"""
    _log(f"--- 视频时长统计 ({len(videos)} 个视频) ---")
    total_duration = 0.0
    for vp in videos:
        try:
            duration = get_video_duration(vp)
            total_duration += duration
            size_mb = vp.stat().st_size / (1024 * 1024)
            print(f"  {vp.parent.name}/{vp.name}  {_format_seconds(duration)}  {size_mb:.1f}MB")
        except Exception as e:
            print(f"  {vp.parent.name}/{vp.name}  [时长获取失败: {e}]")
    print(f"  {'─' * 50}")
    print(f"  总时长: {_format_seconds(total_duration)}  共 {len(videos)} 个视频")
    print()
    return total_duration


def print_batch_timing(
    start_time: datetime,
    end_time: datetime,
    elapsed: float,
    total_duration: float,
) -> None:
    """打印批量计时报告。"""
    _log(f"{'=' * 60}")
    _log(f"批量翻译完成！")
    _log(f"  开始: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    _log(f"  结束: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    _log(f"  总耗时: {_format_seconds(elapsed)}")
    _log(f"  视频总时长: {_format_seconds(total_duration)}")
    if total_duration > 0:
        ratio = elapsed / total_duration
        _log(f"  处理速度比: {ratio:.2f}x（耗时 / 视频时长）")
    _log(f"{'=' * 60}")


# ============================================================
# 命令行入口
# ============================================================

def main():
    p = argparse.ArgumentParser(
        description="批量视频翻译：扫描目录 -> 委托 process_video_pipeline（提取音频 -> 远程翻译 -> 本地合并音轨）"
    )
    p.add_argument("input_dir", help="包含视频文件的根目录，递归扫描所有子目录。")
    p.add_argument('--target', '-t', dest='targets', nargs='+', default=['en'],
                   choices=ALL_TTS_LANGUAGE_CODES,
                   help='要翻译输出的目标语言代码，默认：en（English）')
    p.add_argument('--source', '-s', default='zh', choices=ALL_ASR_LANGUAGE_CODES,
                   help='输入音频的源语言代码（例如 en, zh），默认：zh（普通话，也支持中国方言）')
    p.add_argument('--separate', action=argparse.BooleanOptionalAction, default=True,
                   help='是否运行人声分离以去除背景音。默认开启；传 --no-separate 关闭。')
    p.add_argument('--detect-nonverbal-and-singing', action=argparse.BooleanOptionalAction, default=True,
                   help='检测「非语言人声」（笑/咳/喷嚏/掌声/叹息）与「唱歌」段，自动从 vocals 分流到背景音轨道。'
                        '默认开启；传 --no-detect-nonverbal-and-singing 关闭。')
    p.add_argument('--denoise', choices=['none', 'normal', 'aggressive'], default='aggressive',
                   help='音频降噪类型（需要人声分离）。none=不降噪，normal=标准降噪，aggressive=激进降噪。默认：aggressive')
    p.add_argument('--asr-mode', choices=['basic', 'precise'], default='precise',
                   help='ASR 说话人切分模式: basic=ASR 自带说话人切分, precise=二次精细说话人切分（默认）')
    p.add_argument('--enable-visual-diarization', '-v', action=argparse.BooleanOptionalAction, default=False,
                   help='是否启用视觉辅助说话人切分（视觉 diarization）。默认关闭。'
                        '关闭时本地抽 mp3 上传服务端（带宽友好）；'
                        '开启时本地压缩视频到 480p/25fps 后上传，由服务端在 diarization 阶段结合人脸跟踪/嘴部运动等'
                        '视觉信号辅助说话人切分。最终合成视频仍使用本地高清原版。')

    p.add_argument('--translation-models', default=",".join(DEFAULT_MODELS),
                   help='翻译模型列表，以逗号分隔。空值使用默认模型。')
    p.add_argument('--translation-mode', choices=['independent', 'tts_aware'], default='tts_aware',
                   help='翻译模式: independent=纯文本独立翻译, tts_aware=TTS时长感知翻译。默认：tts_aware')
    p.add_argument('--extra-translation-guideline',
                   help='包含额外翻译指南（e.g.定制化场景要求）的文本文件路径（可选参数）')
    p.add_argument('--tts-aware-max-retries', type=int, default=10,
                   help='TTS感知翻译中每句的自适应翻译重试次数（默认: 10）')
    p.add_argument('--tts-max-audio-slowdown-pct', type=float, default=0.2,
                   help='TTS 合成音频最大减速百分比（合成短于参考时拉伸上限）。默认: 0.2')
    p.add_argument('--tts-max-audio-speedup-pct', type=float, default=0.2,
                   help='TTS 合成音频最大加速百分比（合成长于参考时拉伸上限）。默认: 0.2')
    p.add_argument('--tts-aware-min-candidate-count', type=int, default=3,
                   help='每个片段至少保留的合格候选音频数量（1-10）。默认: 3')

    server_group = p.add_mutually_exclusive_group()
    server_group.add_argument('--server', default='localhost',
                              help='服务端地址（直连模式），支持 IP、域名或完整 URL。默认: localhost')
    server_group.add_argument('--scheduler', default=None,
                              help='调度器地址（IP/域名/URL），指定后由调度器自动分配空闲服务端。'
                                   '与 --server 互斥。')
    p.add_argument('--edit-rerun', '-e', action='store_true',
                   help='编辑重跑模式：检测本地编辑（改ASR/改翻译/替换合成音频/删语种/删mp3/删txt），'
                        '上传修改的文件并删除服务端对应的下游产物，服务端跳过ASR直接从翻译开始。'
                        '要求服务端已有该任务的运行记录。')
    p.add_argument('--stop-after-translation', action='store_true',
                   help='翻译完成后停止流水线，跳过 TTS / 音频合并 / 最终混音。'
                        '翻译完成后始终生成 full_translation.srt 字幕文件（无论是否启用此参数）。'
                        '核心用途：翻译文本后人工介入检查，确认无误后再继续后续流程。')
    p.add_argument('--new-task', '-n', action='store_true',
                   help='强制从头重新翻译：删除本地已翻译视频、segments 目录和 .vt_task_id 文件，'
                        '在服务端创建全新任务。用于需要完全重跑的场景。')

    args = p.parse_args()

    # 解析输入目录
    root_dir = Path(args.input_dir).resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        _log(f"[错误] 目录不存在: {root_dir}")
        sys.exit(1)

    # 解析服务端地址
    try:
        server_url = resolve_server_arg(args.server, scheduler=args.scheduler)
    except (ConnectionError, RuntimeError) as e:
        _log(f"[错误] {e}")
        sys.exit(1)

    # ── Step 1: 递归扫描目录，发现视频文件 ──────────────────
    _log(f"--- Step 1: 扫描目录 ---")
    _log(f"扫描目录: {root_dir}")
    videos = scan_videos_recursive(root_dir)
    if not videos:
        _log("未发现可翻译的视频文件。")
        return

    _log(f"发现 {len(videos)} 个视频文件。")

    # 去重同目录下的 stem 变种（如原视频 + 未登记的派生文件）
    videos = dedup_stem_variants(videos)

    # ── Step 2: 校验 ────────────────────────────────────────
    _log("--- Step 2: 校验视频目录 ---")
    validate_independent_dirs(videos)
    validate_unique_dir_names(videos)
    check_srt_files(videos)

    # ── Step 3: 视频时长统计 ────────────────────────────────
    _log("--- Step 3: 视频时长统计 ---")
    total_duration = print_video_summary(videos)

    # ── Step 4: 单次调用 process_video_pipeline ─────────────
    targets = normalize_target_language_codes(args.targets)

    _log("--- Step 4: 启动批量视频翻译流水线 ---")
    _log(f"视频数: {len(videos)}, 目标语言: {', '.join(targets)}")
    _log(f"服务端: {server_url}")

    batch_start_time = datetime.now()
    batch_start_counter = perf_counter()

    try:
        from video_translate import process_video_pipeline
        process_video_pipeline(
            videos,
            targets,
            server_url,
            source=args.source,
            separate=args.separate,
            detect_nonverbal_and_singing=args.detect_nonverbal_and_singing,
            denoise=args.denoise,
            asr_mode=args.asr_mode,
            translation_models=args.translation_models,
            translation_mode=args.translation_mode,
            tts_aware_max_retries=args.tts_aware_max_retries,
            tts_max_audio_slowdown_pct=args.tts_max_audio_slowdown_pct,
            tts_max_audio_speedup_pct=args.tts_max_audio_speedup_pct,
            tts_aware_min_candidate_count=args.tts_aware_min_candidate_count,
            extra_translation_guideline=args.extra_translation_guideline,
            enable_visual_diarization=args.enable_visual_diarization,
            edit_rerun=args.edit_rerun,
            stop_after_translation=args.stop_after_translation,
            new_task=args.new_task,
        )
    except KeyboardInterrupt:
        print("\n\n用户取消，批量视频翻译流程已中断。")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        _log(f"[错误] {e}")
        sys.exit(1)

    # ── Step 5: 批量计时报告 ────────────────────────────────
    batch_elapsed = perf_counter() - batch_start_counter
    batch_end_time = datetime.now()
    print_batch_timing(batch_start_time, batch_end_time, batch_elapsed, total_duration)


if __name__ == "__main__":
    main()
