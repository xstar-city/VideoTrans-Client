#!/usr/bin/env python3
"""开源版视频翻译入口：本地提取音频 → 远程翻译 → 本地视频音轨替换。

与 audio_translate.py 的纯远程调用模式不同，视频翻译采用
"本地视频操作 + 远程音频翻译" 的混合架构，避免上传大视频文件。

调用链：
    video_translate.py
      ├── Step 1: extract_audio_ffmpeg()       # 本地 ffmpeg
      ├── Step 2: subprocess audio_translate.py # 远程调用
      └── Step 3: mux_audio_into_video()        # 本地 ffmpeg（视频画面不做任何伸缩）

依赖：
    - ffmpeg / ffprobe 在 PATH 中可用
    - requests（audio_translate.py 依赖）
    - Common/ 下的共享模块

示例：
    python video_translate.py "video.mp4" -t en --server <ServerIP>
    python video_translate.py "a.mp4" "b.mp4" -t en --server <ServerIP>
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
)
from remote_client import resolve_server_arg


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
    detect_nonverbal_and_singing: bool = True,
    denoise: str = "aggressive",
    asr_mode: str = "precise",
    translation_models: str = "",
    translation_mode: str = "tts_aware",
    tts_aware_max_retries: int = 10,
    tts_max_audio_slowdown_pct: float = 0.2,
    tts_max_audio_speedup_pct: float = 0.2,
    tts_aware_min_candidate_count: int = 3,
    extra_translation_guideline: str | None = None,
    enable_visual_diarization: bool = False,
    edit_rerun: bool = False,
    stop_after_translation: bool = False,
    new_task: bool = False,
):
    """视频翻译主流程：提取音频 → 远程翻译 → 本地原视频画面 + 新音轨 mux。

    设计约束（"禁止伸缩"简化）：
        - 视频画面保持原速：不切分、不调速、不重新合并
        - 背景音轨保持原始时长，TTS 每段已被服务端强制贴合原段时长
        - 客户端只需把 final.mp3 mux 进原视频即可

    上传策略：
        - enable_visual_diarization=False（默认）：本地 ffmpeg 抽 mp3 → 上传 mp3 给服务端，
          节省上传带宽（mp3 体积 ~ mp4 视频流的 1/10）。
        - enable_visual_diarization=True：跳过本地抽音频，直接上传完整 mp4，
          供服务端在 diarization 阶段结合视觉信号辅助说话人切分。
    """
    use_gpu = detect_gpu_available()
    gpu_status = "GPU 加速" if use_gpu else "CPU 模式"
    upload_mode = "上传视频" if enable_visual_diarization else "上传音频"
    print(f"\n处理 {len(video_paths)} 个视频文件... ({upload_mode}; 音轨合并: {gpu_status})")

    target_codes = normalize_target_language_codes(targets)

    # ── Step 1: 准备上传素材 ───────────────────────────────
    # - 默认走"本地抽 mp3 → 上传 mp3"路径，省带宽
    # - 开启 enable_visual_diarization 则直接上传 mp4，让服务端在 diarization 阶段使用视觉信息
    print("\n--- Step 1: 准备上传素材 ---")
    video_data: dict[Path, dict] = {}

    for video_path in video_paths:
        if not video_path.exists():
            print(f"文件不存在: {video_path}，跳过")
            continue

        # 编辑重跑模式：删除已翻译的目标语言视频，避免被跳过检查跳过
        if edit_rerun:
            for code in target_codes:
                out_video = build_translated_output_path(video_path, video_path, code)
                if out_video.exists():
                    try:
                        out_video.unlink()
                        print(f"[编辑重跑] 删除已翻译视频: {out_video.name}")
                    except OSError as e:
                        print(f"[警告] 无法删除 {out_video.name}: {e}")

        # 检查是否所有目标语言的视频都已存在
        if all(
            build_translated_output_path(video_path, video_path, code).exists()
            for code in target_codes
        ):
            print(f"所有目标视频已存在，跳过: {video_path}")
            continue

        if enable_visual_diarization:
            # 直接以 mp4 作为上传对象
            video_data[video_path] = {"upload": video_path}
            print(f"视觉 diarization 已启用，将直接上传视频: {video_path.name}")
        else:
            # 默认行为：本地抽 mp3
            mp3_path = video_path.with_suffix(".mp3")
            video_data[video_path] = {"upload": mp3_path}

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
    upload_paths = [data["upload"] for data in video_data.values()]

    if upload_paths:
        # 构造 audio_translate.py 的命令行参数并直接调用
        audio_argv = [
            *[str(p) for p in upload_paths],
            "--target", *target_codes,
            "--source", source,
            "--server", server_url,
        ]
        if not separate:
            audio_argv.append("--no-separate")
        if not detect_nonverbal_and_singing:
            audio_argv.append("--no-detect-nonverbal-and-singing")
        audio_argv.extend(["--denoise", denoise])
        audio_argv.extend(["--asr-mode", asr_mode])
        if translation_models:
            audio_argv.extend(["--translation-models", translation_models])
        audio_argv.extend(["--translation-mode", translation_mode])
        audio_argv.extend(["--tts-aware-max-retries", str(tts_aware_max_retries)])
        audio_argv.extend(["--tts-max-audio-slowdown-pct", str(tts_max_audio_slowdown_pct)])
        audio_argv.extend(["--tts-max-audio-speedup-pct", str(tts_max_audio_speedup_pct)])
        audio_argv.extend(["--tts-aware-min-candidate-count", str(tts_aware_min_candidate_count)])
        if extra_translation_guideline:
            audio_argv.extend(["--extra-translation-guideline", extra_translation_guideline])

        if enable_visual_diarization:
            audio_argv.append("--enable-visual-diarization")

        if edit_rerun:
            audio_argv.append("--edit-rerun")

        if stop_after_translation:
            audio_argv.append("--stop-after-translation")

        if new_task:
            audio_argv.append("--new-task")

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

    # ── Step 3: 合并音轨（视频画面保持原速，不做任何切分/调速）──────────
    # stop_after_translation 模式下没有 final.mp3，跳过音轨合并
    if stop_after_translation:
        print("\n--- Step 3: 跳过音轨合并（stop-after-translation 模式）---")
        print("翻译文本和 SRT 字幕已生成，无需 TTS 和音轨合并。")
        return

    print(f"\n--- Step 3: 合并音轨 ---")

    # 合成前同步检查：确保本地文件与服务端一致（所有模式统一执行）
    # 防止服务端重新生成的文件（如 combined.mp3/final.mp3）未被下载到本地
    print("合成前文件同步检查...")
    try:
        from remote_client import RemoteScriptClient
        from audio_translate import _sync_files, _compute_dest_dir, _load_task_id
        sync_client = RemoteScriptClient(server_url)
        for video_path in video_data:
            task_id = _load_task_id(video_path)
            if task_id:
                dest_dir = _compute_dest_dir(video_path)
                _sync_files(sync_client, task_id, video_path.parent,
                           sub_dir=dest_dir, since=0)
    except Exception as e:
        print(f"  [警告] 合成前文件同步检查失败: {e}")

    for code in target_codes:
        print(f"\n处理语言: {code}")
        for video_path, data in list(video_data.items()):
            try:
                out_video = build_translated_output_path(video_path, video_path, code)
                if out_video.exists():
                    print(f"目标视频已存在，跳过: {out_video}")
                    continue

                mp3_path = data["upload"]
                # segments 目录由 upload 文件的 parent 决定（视频/mp3 同父目录），
                # 所以 build_segments_dir(upload) 与 build_segments_dir(video_path) 等价。
                segments_dir = build_segments_dir(mp3_path)
                lang_dir = segments_dir / get_language_dir_name(code)
                final_audio = lang_dir / FINAL_AUDIO_FILENAME

                if not final_audio.exists():
                    print(f"最终音频未找到 ({video_path.name}, {code})，跳过音轨合并")
                    continue

                # 视频不做任何伸缩，直接 mux
                print(f"  合并音轨: {video_path.name} + {final_audio.name} → {out_video.name}...")
                mux_audio_into_video(video_path, final_audio, out_video)
                print(f"  翻译视频已保存: {out_video}")

            except Exception as e:
                print(f"[错误] 处理 {video_path.name} ({code}) 失败: {e}")


# ============================================================
# 命令行入口
# ============================================================

# video pipeline
# 'gemini-3.5-flash'
DEFAULT_MODELS = ['doubao-seed-2-1-turbo', 'deepseek-v4-pro', 'doubao-seed-2-1-pro', 'gemini-3.5-flash']

def main():
    p = argparse.ArgumentParser(description="视频翻译：提取音频 → 远程翻译 → 本地视频同步")
    p.add_argument("inputs", nargs="+", help="本地视频文件路径列表（如 一个或者多个mp4）。")
    p.add_argument('--target', '-t', dest='targets', nargs='+', default=['en'], choices=ALL_TTS_LANGUAGE_CODES, help='要翻译输出的目标语言代码，默认：en（English）')
    p.add_argument('--source', '-s', default='zh', choices=ALL_ASR_LANGUAGE_CODES, help='输入音频的源语言代码（例如 en, zh），默认：zh（普通话，也支持中国方言），一般中文和英文视频，此参数可不管')
    p.add_argument('--separate', action=argparse.BooleanOptionalAction, default=True,
                   help='是否运行人声分离以去除背景音。默认开启；传 --no-separate 关闭，跳过分离直接使用原始音频。')
    p.add_argument('--detect-nonverbal-and-singing', action=argparse.BooleanOptionalAction, default=True,
                   help='检测「非语言人声」（笑/咳/喷嚏/掌声/叹息）与「唱歌」段，自动从 vocals 分流到背景音轨道。'
                        '这些虽是人声但无法翻译，留在 vocals 中会污染下游 ASR。默认开启；'
                        '传 --no-detect-nonverbal-and-singing 关闭。')
    p.add_argument('--denoise', choices=['none', 'normal', 'aggressive'], default='aggressive', help='音频降噪类型（需要人声分离）。none=不降噪，normal=标准降噪，aggressive=激进降噪。默认：aggressive')
    
    p.add_argument('--asr-mode', choices=['basic', 'precise'], default='precise', help='ASR 说话人切分模式: basic=ASR 自带说话人切分, precise=二次精细说话人切分（默认）')
    p.add_argument('--enable-visual-diarization', '-v', action=argparse.BooleanOptionalAction, default=False,
                   help='是否启用视觉辅助说话人切分（视觉 diarization）。默认关闭。'
                        '关闭时本地抽 mp3 上传服务端（带宽友好）；'
                        '开启时直接上传完整 mp4，由服务端在 diarization 阶段结合人脸跟踪/嘴部运动等'
                        '视觉信号辅助说话人切分。')

    p.add_argument('--translation-models', default=",".join(DEFAULT_MODELS), help='翻译模型列表，以逗号分隔。空值使用默认模型。理论上可接任意模型，未来可拓展。')
    p.add_argument('--translation-mode', choices=['independent', 'tts_aware'], default='tts_aware', help='翻译模式: independent=纯文本独立翻译, tts_aware=TTS时长感知翻译（翻译+TTS试合成+时长评估+LLM反馈调整）。默认：tts_aware')
    p.add_argument('--extra-translation-guideline', help='包含额外翻译指南（e.g.定制化场景要求）的文本文件路径（可选参数）')
    p.add_argument('--tts-aware-max-retries', type=int, default=10, help='TTS时长感知模式中每句的最大时长调整重试次数（默认: 10）')
    p.add_argument('--tts-max-audio-slowdown-pct', type=float, default=0.2,
                   help='TTS 合成音频最大减速百分比（合成短于参考时拉伸上限）。默认: 0.2')
    p.add_argument('--tts-max-audio-speedup-pct', type=float, default=0.2,
                   help='TTS 合成音频最大加速百分比（合成长于参考时拉伸上限）。默认: 0.2')
    p.add_argument('--tts-aware-min-candidate-count', type=int, default=3,
                   help='每个片段至少保留的合格候选音频数量（1-10）。默认: 3')

    server_group = p.add_mutually_exclusive_group()
    server_group.add_argument('--server', default='localhost',
                              help='服务端地址（直连模式），支持 IP、域名或完整 URL（如 117.50.47.18 / http://117.50.47.18/ ）。默认: localhost')
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
                        '核心用途：翻译文本后人工介入检查，核查字幕内容和翻译指南，确认无误后再继续后续流程。')
    p.add_argument('--new-task', action='store_true',
                   help='忽略本地保存的 task_id，强制创建新任务。'
                        '当 .vt_task_id 文件指向的任务不在当前服务器时使用。')

    
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
        print('每个视频/音频文件必须在独立目录下，且同一批量任务中各目录名不能重复，')
        print('否则翻译中间结果（segments/、.vt_task_id 等）会互相覆盖导致错乱。')
        print('推荐做法：将每集放入独立子目录，目录名和文件名用集号对应，如：')
        print('  短剧/1/1.mp4')
        print('  短剧/2/2.mp4')
        print('  短剧/3/3.mp4')
        print('解决方法：将每个视频拷贝到新的独立目录下再执行。')
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
        print("\n\n用户取消，视频翻译流程已中断。")
        sys.exit(130)
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
