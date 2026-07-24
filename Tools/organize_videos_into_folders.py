#!/usr/bin/env python3
"""递归扫描视频，统一转为 mp4，并将每个视频放入其专属文件夹。

用于准备阶段：
1) 递归扫描根目录下的候选源视频。
2) 将每个视频移入 `parent/<video_stem>/<video_filename>`。
3) 将非 mp4 视频转换为 `parent/<video_stem>/<video_stem>.mp4`。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# 添加父目录到 sys.path，使 Common 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Common.config import PIPELINE_DERIVED_STEM_MARKERS

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IGNORED_STEM_MARKERS = PIPELINE_DERIVED_STEM_MARKERS


def is_candidate_video(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        return False
    stem_lower = path.stem.lower()
    return not any(marker in stem_lower for marker in IGNORED_STEM_MARKERS)


def discover_source_videos(root_dir: Path) -> list[Path]:
    discovered: dict[Path, Path] = {}
    for path in sorted(root_dir.rglob("*")):
        if not is_candidate_video(path):
            continue
        normalized_path = path.with_suffix(".mp4") if path.suffix.lower() != ".mp4" else path
        if normalized_path in discovered:
            continue
        discovered[normalized_path] = path
    return list(discovered.values())


def run_command(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        executable = Path(cmd[0]).name if cmd else "command"
        raise FileNotFoundError(f"所需可执行文件未找到: {executable}") from exc
    except subprocess.CalledProcessError as exc:
        command_text = " ".join(str(part) for part in cmd)
        raise RuntimeError(f"Command failed with exit code {exc.returncode}: {command_text}") from exc


def ensure_video_in_own_folder(video_path: Path, dry_run: bool = False) -> tuple[Path, bool]:
    if video_path.parent.name == video_path.stem:
        return video_path, False

    target_dir = video_path.parent / video_path.stem
    target_path = target_dir / video_path.name

    if target_path.exists():
        if target_path.resolve() == video_path.resolve():
            return target_path, False
        raise FileExistsError(f"Target video already exists: {target_path}")

    if dry_run:
        return target_path, True

    target_dir.mkdir(exist_ok=True)
    shutil.move(str(video_path), str(target_path))
    return target_path, True


def ensure_video_is_mp4(video_path: Path, dry_run: bool = False) -> tuple[Path, str]:
    if video_path.suffix.lower() == ".mp4":
        return video_path, "already-mp4"

    target_path = video_path.with_suffix(".mp4")
    if target_path.exists():
        if dry_run:
            return target_path, "would-reuse-mp4"
        video_path.unlink()
        return target_path, "reused-mp4"

    if dry_run:
        return target_path, "would-convert"

    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(target_path),
        ]
    )
    video_path.unlink()
    return target_path, "converted"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan videos, place each into its own folder, and standardize to mp4."
    )
    parser.add_argument("input_dir", help="Root directory containing videos.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without moving files.")
    args = parser.parse_args()

    root_dir = Path(args.input_dir).resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise NotADirectoryError(f"Input directory does not exist: {root_dir}")

    videos = discover_source_videos(root_dir)
    if not videos:
        print("No candidate source videos found.")
        return

    videos_need_work = [video for video in videos if video.parent.name != video.stem or video.suffix.lower() != ".mp4"]
    if not videos_need_work:
        print(f"Found {len(videos)} candidate video(s).")
        print("All videos are already organized into dedicated folders and standardized as mp4. Nothing to do.")
        return

    moved_count = 0
    converted_count = 0
    reused_count = 0
    skipped_count = 0

    print(f"Found {len(videos)} candidate video(s).")
    for video_path in videos:
        organized_path, moved = ensure_video_in_own_folder(video_path, dry_run=args.dry_run)
        final_path, status = ensure_video_is_mp4(organized_path, dry_run=args.dry_run)

        changed = moved or status != "already-mp4"
        if moved:
            moved_count += 1
            action = "[DRY-RUN] Would move" if args.dry_run else "Moved"
            print(f"{action}: {video_path} -> {organized_path}")

        if status in {"would-convert", "converted"}:
            converted_count += 1
            action = "[DRY-RUN] Would convert to mp4" if args.dry_run else "Converted to mp4"
            print(f"{action}: {organized_path} -> {final_path}")
        elif status in {"would-reuse-mp4", "reused-mp4"}:
            reused_count += 1
            action = "[DRY-RUN] Would reuse existing mp4" if args.dry_run else "Reused existing mp4"
            print(f"{action}: {organized_path} -> {final_path}")

        if not changed:
            skipped_count += 1
            print(f"Skipped (already organized): {video_path}")

    print("\n汇总:")
    print(f"  扫描总数: {len(videos)}")
    print(f"  {'将移动' if args.dry_run else '已移动'}: {moved_count}")
    print(f"  {'将转换为 mp4' if args.dry_run else '已转换为 mp4'}: {converted_count}")
    print(f"  {'将使用已有 mp4' if args.dry_run else '已使用已有 mp4'}: {reused_count}")
    print(f"  已跳过: {skipped_count}")


if __name__ == "__main__":
    main()
