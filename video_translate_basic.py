#!/usr/bin/env python3
"""基本模式视频翻译快捷脚本。

预置最优参数，适合大多数场景：
- ASR 模式：basic（ASR 自带说话人切分）
- 翻译模式：independent（无需 TTS 时长感知）
- 音频变速：禁用（保持原速）

使用方式：
    python video_translate_solo.py "video.mp4" -t en --server http://<IP>:8000 -s
    python video_translate_solo.py "a.mp4" "b.mp4" -t en ja --server http://<IP>:8000 -s

与 video_translate.py 的区别仅在于默认参数，所有完整参数仍可通过命令行覆盖。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


from video_translate import process_video_pipeline


def main():
    p = argparse.ArgumentParser(description="基本模式视频翻译（预置最优参数：basic ASR + independent 翻译 + 禁用音频变速）")
    # ── 用户常用参数 ──
    p.add_argument("inputs", nargs="+", help="本地视频文件路径")
    p.add_argument("--target", "-t", dest="targets", nargs="+", default=["en"], help="目标语言代码，默认：en")
    p.add_argument("--source", "-s", default="zh", help="源语言代码，默认：zh")
    p.add_argument('--separate', action=argparse.BooleanOptionalAction, default=True,
                   help='是否运行人声分离以去除背景音。默认开启；传 --no-separate 关闭，跳过分离直接使用原始音频。')
    p.add_argument("--server", default="http://localhost:8000", help="服务端地址，默认：http://localhost:8000")
    # ── 可覆盖的预置参数 ──
    p.add_argument("--denoise", choices=["none", "normal", "aggressive"], default="aggressive", help="音频降噪类型，默认：aggressive")
    p.add_argument("--translation-models", default="", help="翻译模型列表，空值使用默认模型")
    p.add_argument("--extra-translation-guideline", default=None, help="额外翻译指南文本文件路径")

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

    try:
        process_video_pipeline(
            video_paths,
            args.targets,
            args.server,
            source=args.source,
            separate=args.separate,
            # ── 基本模式预置参数 ──
            detect_nonverbal_and_singing=False,  # 基本模式无需「非语言人声 + 唱歌」检测
            denoise=args.denoise,
            asr_mode="basic",               # 基本模式使用 ASR 自带说话人切分
            translation_models=args.translation_models,
            translation_mode="independent",   # 基本模式无需 TTS 时长感知
            tts_aware_max_retries=0,          # independent 模式不使用
            max_audio_slowdown_pct=0.0,       # 禁用音频变速
            max_audio_speedup_pct=0.0,        # 禁用音频变速
            extra_translation_guideline=args.extra_translation_guideline,
            max_video_slowdown_pct=0.1,
            max_video_speedup_pct=0.2,
        )
    except KeyboardInterrupt:
        print("\n\n用户取消，视频翻译流程已中断。")
        sys.exit(130)
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
