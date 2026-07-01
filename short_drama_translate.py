#!/usr/bin/env python3
"""批量短剧翻译客户端。

扫描本地目录，整理视频到标准结构，提取音频，远程翻译，本地视频同步+合并音轨。

调用链：
    short_drama_translate.py
      ├── Step 0: prepare_videos()         # 本地整理+ffmpeg转mp4
      ├── Step 1: extract_audio_ffmpeg()    # 本地ffmpeg提取音频
      ├── Step 2: subprocess audio_translate.py  # 远程翻译（上传+执行+下载）
      ├── Step 3: sync_video_to_audio()     # 本地视频同步
      └── Step 4: mux_audio_into_video()    # 本地ffmpeg合并音轨

依赖：
    - ffmpeg / ffprobe 在 PATH 中可用
    - requests（audio_translate.py 依赖）
    - Common/ 下的共享模块

示例：
    python short_drama_translate.py "E:\\短剧\\《逐玉》" -t en --server <ServerIP>
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter


from Common.asr_languages import ALL_ASR_LANGUAGE_CODES
from Common.config import FINAL_AUDIO_FILENAME, build_segments_dir
from Common.language_map import get_language_dir_name, normalize_target_language_codes
from Common.tts_languages import ALL_TTS_LANGUAGE_CODES
from Common.video_utils import (
    build_translated_output_path,
    detect_gpu_available,
    extract_audio_ffmpeg,
    mux_audio_into_video,
    sync_video_to_audio,
)

# ─── 视频发现与整理 ───────────────────────────────────────

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IGNORED_STEM_MARKERS = (
    "_translated_",
    "_match_audio",
    "_vocals",
    "_others",
)

DEFAULT_MODELS = [
    "deepseek-v4-flash",
    "gemini-3.1-flash-lite",
    "doubao-seed-2-0-lite",
    "deepseek-v4-pro",
    "doubao-seed-2-0-pro",
]


def is_candidate_video(path: Path) -> bool:
    """判断是否为待翻译的候选视频文件。"""
    if not path.is_file():
        return False
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        return False
    stem_lower = path.stem.lower()
    return not any(marker in stem_lower for marker in IGNORED_STEM_MARKERS)


def discover_standardized_source_videos(root_dir: Path) -> list[Path]:
    """发现已整理到"同名子目录/同名文件"结构的视频。

    不符合结构的候选视频会被收集并打印警告（不静默跳过）。
    """
    discovered: dict[Path, Path] = {}
    skipped: list[Path] = []
    for path in sorted(root_dir.rglob("*")):
        if not is_candidate_video(path):
            continue
        if path.parent.name != path.stem:
            skipped.append(path)
            continue
        normalized_path = path.with_suffix(".mp4") if path.suffix.lower() != ".mp4" else path
        if normalized_path in discovered:
            continue
        discovered[normalized_path] = path

    if skipped:
        print(f"[警告] 以下 {len(skipped)} 个视频未在独立子目录中，已跳过（要求：视频文件名与父目录名一致）：")
        print('提示：可运行 Tools\\organize_videos_into_folders.py 自动整理视频到独立目录。')
        for sp in skipped[:10]:
            print(f"  {sp}  (父目录: {sp.parent.name})")
        if len(skipped) > 10:
            print(f"  ... 还有 {len(skipped) - 10} 个")

    return list(discovered.values())


def convert_to_mp4(path: Path) -> Path:
    """将视频转换为 mp4 格式（已是 mp4 则原样返回）。"""
    if path.suffix.lower() == ".mp4":
        return path

    mp4_path = path.with_suffix(".mp4")
    managed_mp4_path = path.parent / path.stem / f"{path.stem}.mp4"
    if mp4_path.exists():
        print(f"  复用已有 mp4: {mp4_path}")
        return mp4_path
    if managed_mp4_path.exists():
        print(f"  复用已整理 mp4: {managed_mp4_path}")
        return managed_mp4_path

    print(f"  转换格式: {path.name} → mp4...")
    cmd = [
        "ffmpeg", "-y", "-i", str(path),
        "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart",
        str(mp4_path),
    ]
    subprocess.run(cmd, check=True)
    return mp4_path


def run_organize_videos_script(root_dir: Path) -> None:
    """调用整理脚本，把视频统一整理到同名子目录结构。"""
    organizer_script = Path(__file__).parent.parent / "服务端" / "Tools" / "organize_videos_into_folders.py"
    if not organizer_script.exists():
        # 客户端模式可能没有服务端目录，跳过
        print("[警告] 整理脚本不存在，跳过自动整理。请确保视频已按同名子目录组织。")
        return
    subprocess.run(
        [sys.executable, str(organizer_script), str(root_dir)],
        check=True,
    )


def prepare_videos(root_dir: Path) -> list[Path]:
    """整理并准备待翻译的视频文件列表。"""
    run_organize_videos_script(root_dir)

    prepared_videos: list[Path] = []
    for source_path in discover_standardized_source_videos(root_dir):
        mp4_path = convert_to_mp4(source_path)
        prepared_videos.append(mp4_path)

    unique_videos = sorted(
        {path.resolve(): path for path in prepared_videos}.values(),
        key=lambda p: str(p),
    )
    return unique_videos


# ─── 翻译结果检测与分组 ───────────────────────────────────

def translated_output_exists(video_path: Path, target_code: str) -> bool:
    """检查某视频的某目标语言翻译是否已存在。"""
    prefix = f"{video_path.stem}_translated_{target_code}"
    for sibling in video_path.parent.iterdir():
        if sibling.is_file() and sibling.suffix.lower() in VIDEO_SUFFIXES and sibling.stem == prefix:
            return True
    return False


def collect_missing_targets(
    video_paths: list[Path], targets: list[str],
) -> dict[tuple[str, ...], list[Path]]:
    """按"缺失目标语言组合"对视频分组。"""
    from collections import defaultdict
    grouped: dict[tuple[str, ...], list[Path]] = defaultdict(list)
    for video_path in video_paths:
        missing_targets = tuple(
            code for code in targets
            if not translated_output_exists(video_path, code)
        )
        if missing_targets:
            grouped[missing_targets].append(video_path)
    return dict(grouped)


# ─── 时间格式化 ──────────────────────────────────────────

def format_seconds(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# ─── 单批视频翻译流程 ────────────────────────────────────

def process_batch(
    video_paths: list[Path],
    targets: list[str],
    server_url: str,
    *,
    source: str = "zh",
    separate: bool = True,
    detect_nonverbal_and_singing: bool = True,
    denoise: str = "aggressive",
    translation_models: str = "",
    tts_aware_max_retries: int = 3,
    extra_translation_guideline: str | None = None,
) -> None:
    """处理一批视频：提取音频 → 远程翻译 → 本地视频同步+合并。"""
    use_gpu = detect_gpu_available()
    gpu_status = "GPU 加速" if use_gpu else "CPU 模式"
    print(f"\n处理 {len(video_paths)} 个视频... (视频同步/合并: {gpu_status})")

    target_codes = normalize_target_language_codes(targets)

    # ── Step 1: 本地提取音频 ──────────────────────────────
    print("\n--- Step 1: 提取音频 ---")
    video_data: dict[Path, dict] = {}

    for video_path in video_paths:
        if not video_path.exists():
            print(f"文件不存在: {video_path}，跳过")
            continue

        # 检查是否所有目标语言的视频都已存在
        if all(
            build_translated_output_path(video_path, video_path, code).exists()
            for code in target_codes
        ):
            print(f"所有目标视频已存在，跳过: {video_path}")
            continue

        mp3_path = video_path.with_suffix(".mp3")
        video_data[video_path] = {"mp3": mp3_path}

        if mp3_path.exists():
            print(f"音频已存在: {mp3_path}，跳过提取。")
        else:
            try:
                print(f"提取音频: {video_path.name}...")
                extract_audio_ffmpeg(video_path, mp3_path)
            except Exception as e:
                print(f"提取音频失败 {video_path}: {e}")
                del video_data[video_path]

    # ── Step 2: 远程音频翻译（subprocess 调用 audio_translate.py）──
    print("\n--- Step 2: 远程音频翻译 ---")
    audio_paths = [data["mp3"] for data in video_data.values()]

    if audio_paths:
        audio_translate_script = Path(__file__).parent / "audio_translate.py"
        if not audio_translate_script.exists():
            print(f"[错误] audio_translate.py 不存在: {audio_translate_script}")
            sys.exit(1)

        cmd = [
            sys.executable,
            str(audio_translate_script),
            *[str(p) for p in audio_paths],
            "--target", *target_codes,
            "--source", source,
            "--server", server_url,
        ]
        if not separate:
            cmd.append("--no-separate")
        if not detect_nonverbal_and_singing:
            cmd.append("--no-detect-nonverbal-and-singing")
        cmd.extend(["--denoise", denoise])
        if translation_models:
            cmd.extend(["--translation-models", translation_models])
        cmd.extend(["--tts-aware-max-retries", str(tts_aware_max_retries)])
        if extra_translation_guideline:
            cmd.extend(["--extra-translation-guideline", extra_translation_guideline])

        print(f"调用 audio_translate.py (server={server_url})...")
        try:
            result = subprocess.run(cmd, check=False)
            if result.returncode != 0:
                print(f"[错误] audio_translate.py 返回非零退出码: {result.returncode}")
                sys.exit(result.returncode)
        except FileNotFoundError:
            print(f"[错误] 无法执行 Python: {sys.executable}")
            sys.exit(1)

    # ── Step 3 + 4: 本地视频同步 + 合并音轨 ──────────────────
    print("\n--- Step 3: 视频同步 + 合并音轨 ---")
    for code in target_codes:
        print(f"\n处理语言: {code}")
        for video_path, data in list(video_data.items()):
            try:
                out_video = build_translated_output_path(video_path, video_path, code)
                if out_video.exists():
                    print(f"目标视频已存在，跳过: {out_video}")
                    continue

                mp3_path = data["mp3"]
                segments_dir = build_segments_dir(mp3_path)
                lang_dir = segments_dir / get_language_dir_name(code)
                final_audio = lang_dir / FINAL_AUDIO_FILENAME

                if not final_audio.exists():
                    print(f"最终音频未找到 ({video_path.name}, {code})，跳过音轨合并")
                    continue

                # Step 3: 视频同步
                print(f"  同步视频: {video_path.name} ({code})...")
                try:
                    synced_video = sync_video_to_audio(video_path, code)
                except Exception as e:
                    print(f"  [错误] 视频同步失败: {e}")
                    continue

                mux_source_video = synced_video

                # Step 4: 合并音轨
                out_video = build_translated_output_path(video_path, mux_source_video, code)
                print(f"  合并音轨: {mux_source_video.name} + {final_audio.name} → {out_video.name}...")
                mux_audio_into_video(mux_source_video, final_audio, out_video)
                print(f"  翻译视频已保存: {out_video}")

                # 清理中间文件
                if mux_source_video != video_path and mux_source_video.exists():
                    try:
                        mux_source_video.unlink()
                        print(f"  已清理中间视频: {mux_source_video.name}")
                    except OSError as e:
                        print(f"  [警告] 无法删除中间视频 {mux_source_video.name}: {e}")

            except Exception as e:
                print(f"[错误] 处理 {video_path.name} ({code}) 失败: {e}")


# ─── 命令行入口 ──────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="批量短剧翻译：扫描目录 → 整理视频 → 提取音频 → 远程翻译 → 本地视频同步"
    )
    p.add_argument("input_dir", help="包含短剧视频的根目录。")
    p.add_argument('--target', '-t', dest='targets', nargs='+', default=['en'],
                   choices=ALL_TTS_LANGUAGE_CODES,
                   help='要翻译输出的目标语言代码，默认：en（English）')
    p.add_argument('--source', '-s', default='zh', choices=ALL_ASR_LANGUAGE_CODES,
                   help='输入音频的源语言代码，默认：zh（普通话，也支持中国方言）')
    p.add_argument('--separate', action=argparse.BooleanOptionalAction, default=True,
                   help='是否运行人声分离以去除背景音。默认开启；传 --no-separate 关闭，跳过分离直接使用原始音频。')
    p.add_argument('--detect-nonverbal-and-singing', action=argparse.BooleanOptionalAction, default=True,
                   help='检测「非语言人声」（笑/咳/喷嚏/掌声/叹息）与「唱歌」段，自动从 vocals 分流到背景音轨道。'
                        '这些虽是人声但无法翻译，留在 vocals 中会污染下游 ASR。默认开启；'
                        '传 --no-detect-nonverbal-and-singing 关闭。')
    p.add_argument('--denoise', choices=['none', 'normal', 'aggressive'], default='aggressive',
                   help='降噪类型（需要人声分离）。默认：aggressive')
    p.add_argument('--translation-models', default=",".join(DEFAULT_MODELS),
                   help='翻译模型列表，以逗号分隔。')
    p.add_argument('--extra-translation-guideline',
                   help='包含额外翻译指南的文本文件路径（可选参数）')
    p.add_argument('--tts-aware-max-retries', type=int, default=3,
                   help='TTS时长感知模式中每句的最大时长调整重试次数（默认: 3）')
    p.add_argument('--server', default='localhost',
                   help='服务端 IP 地址 (默认: localhost)')
    args = p.parse_args()
    server_url = normalize_server_url(args.server)

    root_dir = Path(args.input_dir).resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        print(f"[错误] 目录不存在: {root_dir}")
        sys.exit(1)

    batch_start_time = datetime.now()
    batch_start_counter = perf_counter()

    # ── Step 0: 整理视频 ────────────────────────────────
    print("--- Step 0: 整理视频 ---")
    prepared_videos = prepare_videos(root_dir)
    if not prepared_videos:
        print("未发现可翻译的视频文件。")
        return

    print(f"发现 {len(prepared_videos)} 个视频。")

    # ── 按缺失目标语言分组 ──────────────────────────────
    targets = normalize_target_language_codes(args.targets)
    grouped_tasks = collect_missing_targets(prepared_videos, targets)

    all_pending = sorted({
        vp.resolve(): vp
        for vps in grouped_tasks.values()
        for vp in vps
    }.values(), key=lambda p: str(p))

    if not grouped_tasks:
        print("所有请求的翻译视频已存在，无需处理。")
        return

    print(f"待翻译: {len(all_pending)} 个视频，{len(grouped_tasks)} 组任务。")

    # ── 逐组执行 ────────────────────────────────────────
    for missing_targets, video_paths in sorted(
        grouped_tasks.items(),
        key=lambda item: (len(item[0]), str(item[0]), len(item[1])),
    ):
        print(f"\n{'='*60}")
        print(f"翻译 {len(video_paths)} 个视频 → 目标语言: {', '.join(missing_targets)}")
        print(f"{'='*60}")
        process_batch(
            video_paths,
            list(missing_targets),
            server_url,
            source=args.source,
            separate=args.separate,
            detect_nonverbal_and_singing=args.detect_nonverbal_and_singing,
            denoise=args.denoise,
            translation_models=args.translation_models,
            tts_aware_max_retries=args.tts_aware_max_retries,
            extra_translation_guideline=args.extra_translation_guideline,
        )

    batch_elapsed = perf_counter() - batch_start_counter
    batch_end = datetime.now()
    print(f"\n批量翻译完成！总耗时: {format_seconds(batch_elapsed)}")
    print(f"开始: {batch_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"结束: {batch_end.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)
