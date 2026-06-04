#!/usr/bin/env python3
"""开源版视频翻译入口：本地提取音频 → 远程翻译 → 本地视频同步。

与 audio_translate.py 的纯远程调用模式不同，视频翻译采用
"本地视频操作 + 远程音频翻译" 的混合架构，避免上传大视频文件。

调用链：
    video_translate.py
      ├── Step 1: extract_audio_ffmpeg()       # 本地 ffmpeg
      ├── Step 2: subprocess audio_translate.py # 远程调用
      ├── Step 3: sync_video_to_audio()         # 本地视频同步
      └── Step 4: mux_audio_into_video()        # 本地 ffmpeg

依赖：
    - ffmpeg / ffprobe 在 PATH 中可用
    - requests（audio_translate.py 依赖）
    - Common/ 下的共享模块

示例：
    python video_translate.py "video.mp4" -t en --server http://<ServerIP>:8000 -s
    python video_translate.py "a.mp4" "b.mp4" -t en --server http://<ServerIP>:8000 -s
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


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


# ============================================================
# 主流程
# ============================================================

def process_video_pipeline(
    video_paths: list[Path],
    targets: list[str],
    server_url: str,
    *,
    source: str = "zh",
    separate: bool = True,
    detect_flexsed_event: bool = False,
    denoise: str = "aggressive",
    asr_mode: str = "precise",
    translation_models: str = "",
    translation_mode: str = "tts_aware",
    tts_aware_max_retries: int = 3,
    max_audio_slowdown_pct: float = 0.2,
    max_audio_speedup_pct: float = 0.3,
    extra_translation_guideline: str | None = None,
    max_video_slowdown_pct: float = 0.1,
    max_video_speedup_pct: float = 0.2,
):
    """视频翻译主流程：提取音频 → 远程翻译 → 本地视频同步 + 合并音轨。"""
    use_gpu = detect_gpu_available()
    gpu_status = "GPU 加速" if use_gpu else "CPU 模式"
    print(f"\n处理 {len(video_paths)} 个视频文件... (视频同步/合并: {gpu_status})")

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

    # ── Step 2: 远程音频翻译（直接调用 audio_translate.main()）──
    # 注意：不使用 subprocess，否则 Windows 下 Ctrl+C 无法传递给子进程
    print("\n--- Step 2: 远程音频翻译 ---")
    audio_paths = [data["mp3"] for data in video_data.values()]

    if audio_paths:
        # 构造 audio_translate.py 的命令行参数并直接调用
        audio_argv = [
            *[str(p) for p in audio_paths],
            "--target", *target_codes,
            "--source", source,
            "--server", server_url,
        ]
        if not separate:
            audio_argv.append("--no-separate")
        if detect_flexsed_event:
            audio_argv.append("--detect-flexsed-event")
        audio_argv.extend(["--denoise", denoise])
        audio_argv.extend(["--asr-mode", asr_mode])
        if translation_models:
            audio_argv.extend(["--translation-models", translation_models])
        audio_argv.extend(["--translation-mode", translation_mode])
        audio_argv.extend(["--tts-aware-max-retries", str(tts_aware_max_retries)])
        audio_argv.extend(["--max-audio-slowdown-pct", str(max_audio_slowdown_pct)])
        audio_argv.extend(["--max-audio-speedup-pct", str(max_audio_speedup_pct)])
        audio_argv.extend(["--max-video-slowdown-pct", str(max_video_slowdown_pct)])
        audio_argv.extend(["--max-video-speedup-pct", str(max_video_speedup_pct)])
        if extra_translation_guideline:
            audio_argv.extend(["--extra-translation-guideline", extra_translation_guideline])

        print(f"调用 audio_translate.py (server={server_url})...")
        # 临时替换 sys.argv 以直接调用 audio_translate.main()
        original_argv = sys.argv
        sys.argv = ["audio_translate.py"] + audio_argv
        try:
            from audio_translate import main as audio_translate_main
            audio_translate_main()
        except SystemExit as e:
            # 退出码 130 = 被信号中断（Ctrl+C），属于主动取消，不报错
            if e.code not in (0, 130, None):
                print(f"[错误] audio_translate.py 返回非零退出码: {e.code}")
                sys.exit(e.code)
        finally:
            sys.argv = original_argv

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


# ============================================================
# 命令行入口
# ============================================================

# video pipeline
DEFAULT_MODELS = ['gemini-3.1-flash-lite','deepseek-v4-flash', 'doubao-seed-2-0-lite', 'deepseek-v4-pro','doubao-seed-2-0-pro']

def main():
    p = argparse.ArgumentParser(description="视频翻译：提取音频 → 远程翻译 → 本地视频同步")
    p.add_argument("inputs", nargs="+", help="本地视频文件路径列表（如 一个或者多个mp4）。")
    p.add_argument('--target', '-t', dest='targets', nargs='+', default=['en'], choices=ALL_TTS_LANGUAGE_CODES, help='要翻译输出的目标语言代码，默认：en（English）')
    p.add_argument('--source', '-s', default='zh', choices=ALL_ASR_LANGUAGE_CODES, help='输入音频的源语言代码（例如 en, zh），默认：zh（普通话，也支持中国方言），一般中文和英文视频，此参数可不管')
    p.add_argument('--no-separate', action='store_true', help='跳过人声分离，保留原始音频（默认会运行人声分离以去除背景音）。')
    p.add_argument('--detect-flexsed-event', action='store_true', help='在分离过程中启用 FlexSED 事件检测和下游 LLM 过滤（默认禁用）。')
    p.add_argument('--denoise', choices=['none', 'normal', 'aggressive'], default='aggressive', help='音频降噪类型（需要人声分离）。none=不降噪，normal=标准降噪，aggressive=激进降噪。默认：aggressive')
    p.add_argument('--asr-mode', choices=['basic', 'precise'], default='precise', help='ASR 说话人切分模式: basic=ASR 自带说话人切分, precise=二次精细说话人切分（默认）')
    p.add_argument('--translation-models', default=",".join(DEFAULT_MODELS), help='翻译模型列表，以逗号分隔。空值使用默认模型。理论上可接任意模型，未来可拓展。')
    p.add_argument('--translation-mode', choices=['independent', 'tts_aware'], default='tts_aware', help='翻译模式: independent=纯文本独立翻译, tts_aware=TTS时长感知翻译（翻译+TTS试合成+时长评估+LLM反馈调整）。默认：tts_aware')
    p.add_argument('--extra-translation-guideline', help='包含额外翻译指南（e.g.定制化场景要求）的文本文件路径（可选参数）')
    p.add_argument('--tts-aware-max-retries', type=int, default=3, help='TTS时长感知模式中每句的最大时长调整重试次数（默认: 3）')
    # 视频的伸缩，是音频最后没能伸缩到1，剩下的部分。比如tts合成音频长1.5s，原音频长1s，压缩音频到1.3s后，剩下的0.3s，就是视频的伸缩，画面会变快。
    p.add_argument('--max-audio-slowdown-pct', type=float, default=0.1, help='允许的最大 TTS 音频加速比例（相对原始时长）')
    p.add_argument('--max-audio-speedup-pct', type=float, default=0.2, help='允许的最大 TTS 音频减慢比例（相对原始时长）')
    p.add_argument('--max-video-slowdown-pct', type=float, default=0.1, help='视频片段最大允许减速比例（相对原始时长）')
    p.add_argument('--max-video-speedup-pct', type=float, default=0.2, help='视频片段最大允许加速比例（相对原始时长）')
    p.add_argument('--server', default='http://localhost:8001', help='服务端地址 (默认: http://localhost:8000)')
    args = p.parse_args()

    # 解析输入路径
    video_paths = [Path(p) for p in args.inputs]
    video_paths = [p for p in video_paths if p.exists()]
    if not video_paths:
        print("未找到有效的输入文件")
        sys.exit(1)

    # 检查每个视频是否在独立目录中（不同视频不能共享同一父目录）
    dir_to_videos: dict[str, list[Path]] = {}
    for vp in video_paths:
        dir_key = str(vp.resolve().parent)
        dir_to_videos.setdefault(dir_key, []).append(vp)
    multi_video_dirs = {d: vps for d, vps in dir_to_videos.items() if len(vps) > 1}
    if multi_video_dirs:
        print('[错误] 以下目录中包含多个视频文件，违反"每个视频独立目录"规则：')
        for dir_path, vps in multi_video_dirs.items():
            print(f"  目录: {dir_path}")
            for vp in vps:
                print(f"    - {vp.name}")
        print('请将每个视频移到独立的子目录中，避免翻译中间文件互相覆盖。')
        print('提示：可运行 Tools\\organize_videos_into_folders.py 自动整理视频到独立目录。')
        sys.exit(1)

    try:
        process_video_pipeline(
            video_paths,
            args.targets,
            args.server,
            source=args.source,
            separate=not args.no_separate,
            detect_flexsed_event=args.detect_flexsed_event,
            denoise=args.denoise,
            asr_mode=args.asr_mode,
            translation_models=args.translation_models,
            translation_mode=args.translation_mode,
            tts_aware_max_retries=args.tts_aware_max_retries,
            max_audio_slowdown_pct=args.max_audio_slowdown_pct,
            max_audio_speedup_pct=args.max_audio_speedup_pct,
            extra_translation_guideline=args.extra_translation_guideline,
            max_video_slowdown_pct=args.max_video_slowdown_pct,
            max_video_speedup_pct=args.max_video_speedup_pct,
        )
    except KeyboardInterrupt:
        print("\n\n用户取消，视频翻译流程已中断。")
        sys.exit(130)
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
