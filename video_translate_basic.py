#!/usr/bin/env python3
"""基本模式视频翻译快捷脚本。

预置最优参数，适合单人视频（无需复杂 diarization）：
- ASR 模式：basic（ASR 自带说话人切分）
- 翻译模式：tts_aware（TTS 时长感知翻译，确保合成语音贴合原段时长）
- 非语言人声/唱歌检测：关闭（单人场景默认不需要）

视频画面/背景音轨保持原样，不做任何伸缩。

使用方式：
    python video_translate_basic.py "video.mp4" -t en --server <IP>
    python video_translate_basic.py "a.mp4" "b.mp4" -t en ja --server <IP>

与 video_translate.py 的区别仅在于默认参数（basic ASR、无 diarization、无非语言检测），
所有完整参数仍可通过命令行覆盖。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


from Common.asr_languages import ALL_ASR_LANGUAGE_CODES
from Common.tts_languages import ALL_TTS_LANGUAGE_CODES
from remote_client import resolve_server_arg
from video_translate import DEFAULT_MODELS, process_video_pipeline


def main():
    p = argparse.ArgumentParser(description="基本模式视频翻译（预置最优参数：basic ASR + tts_aware 翻译，适合单人视频）")
    # ── 用户常用参数 ──
    p.add_argument("inputs", nargs="+", help="本地视频文件路径")
    p.add_argument("--target", "-t", dest="targets", nargs="+", default=["en"],
                   choices=ALL_TTS_LANGUAGE_CODES, help="目标语言代码，默认：en")
    p.add_argument("--source", "-s", default="zh",
                   choices=ALL_ASR_LANGUAGE_CODES, help="源语言代码，默认：zh")
    p.add_argument('--separate', action=argparse.BooleanOptionalAction, default=True,
                   help='是否运行人声分离以去除背景音。默认开启；传 --no-separate 关闭，跳过分离直接使用原始音频。')
    server_group = p.add_mutually_exclusive_group()
    server_group.add_argument("--server", default="localhost",
                              help="服务端地址（直连模式），支持 IP、域名或完整 URL。默认：localhost")
    server_group.add_argument("--scheduler", default=None,
                              help="调度器地址（IP/域名/URL），指定后由调度器自动分配空闲服务端。"
                                   "与 --server 互斥。")
    # ── 可覆盖的预置参数 ──
    p.add_argument("--denoise", choices=["none", "normal", "aggressive"], default="aggressive", help="音频降噪类型，默认：aggressive")
    p.add_argument("--translation-models", default=",".join(DEFAULT_MODELS), help="翻译模型列表，以逗号分隔。默认使用与完整版相同的模型列表。")
    p.add_argument("--extra-translation-guideline", default=None, help="额外翻译指南文本文件路径")
    # ── 工作流参数 ──
    p.add_argument('--stop-after-translation', action='store_true',
                   help='翻译完成后停止流水线，跳过 TTS / 音频合并 / 最终混音。'
                        '翻译完成后始终生成 full_translation.srt 字幕文件。'
                        '核心用途：翻译文本后人工介入检查，确认无误后再继续后续流程。')
    p.add_argument('--new-task', '-n', action='store_true',
                   help='强制从头重新翻译：删除本地已翻译视频、segments 目录和 .vt_task_id 文件，'
                        '在服务端创建全新任务。用于需要完全重跑的场景。')

    args = p.parse_args()

    # 解析输入路径
    video_paths = [Path(p) for p in args.inputs]
    video_paths = [p for p in video_paths if p.exists()]
    if not video_paths:
        print("未找到有效的输入文件")
        sys.exit(1)

    # 检查每个视频是否在独立目录中
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
        sys.exit(1)

    # 解析服务端地址：--scheduler 由调度器分配空闲节点，--server 直连（老模式）
    try:
        server_url = resolve_server_arg(args.server, scheduler=args.scheduler)
    except (ConnectionError, RuntimeError) as e:
        print(f"[错误] {e}")
        sys.exit(1)

    try:
        process_video_pipeline(
            video_paths,
            args.targets,
            server_url,
            source=args.source,
            separate=args.separate,
            # ── 基本模式预置参数 ──
            detect_nonverbal_and_singing=False,  # 单人场景默认不需要
            denoise=args.denoise,
            asr_mode="basic",               # 基本模式使用 ASR 自带说话人切分
            translation_models=args.translation_models,
            translation_mode="tts_aware",   # TTS 时长感知翻译
            extra_translation_guideline=args.extra_translation_guideline,
            # ── 工作流参数 ──
            stop_after_translation=args.stop_after_translation,
            new_task=args.new_task,
        )
    except KeyboardInterrupt:
        print("\n\n用户取消，视频翻译流程已中断。")
        sys.exit(130)
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
