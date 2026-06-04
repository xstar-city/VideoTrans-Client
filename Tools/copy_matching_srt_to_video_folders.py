#!/usr/bin/env python3
"""将匹配的 SRT 字幕文件复制到每个视频的专属文件夹中。

本脚本与 `Tools/organize_videos_into_folders.py` 的视频发现/过滤逻辑保持一致，
用于后续准备阶段：
1) 递归扫描根目录下的候选源视频。
2) 递归扫描字幕目录下的 `.srt` 文件。
3) 查找文件名主干与视频主干匹配的字幕文件。
4) 将字幕文件复制到视频所属的文件夹中。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

VIDEO_SUFFIXES = {".mp4"}
IGNORED_STEM_MARKERS = (
    "_translated_",
    "_match_audio",
    "_vocals",
    "_others",
)


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


def discover_srt_files(srt_dir: Path) -> tuple[dict[str, Path], dict[str, list[Path]]]:
    matched: dict[str, Path] = {}
    duplicates: dict[str, list[Path]] = {}

    for path in sorted(srt_dir.rglob("*.srt")):
        if not path.is_file():
            continue
        stem_key = path.stem.lower()
        if stem_key in matched:
            duplicates.setdefault(stem_key, [matched[stem_key]]).append(path)
            continue
        matched[stem_key] = path

    return matched, duplicates


def copy_matching_srt(
    video_path: Path,
    srt_path: Path,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
) -> tuple[Path, str]:
    target_path = video_path.parent / srt_path.name
    target_exists = target_path.exists()

    if target_exists:
        if target_path.resolve() == srt_path.resolve():
            return target_path, "already-linked"
        if not overwrite:
            return target_path, "exists"
        if dry_run:
            return target_path, "would-overwrite"

    if dry_run:
        return target_path, "would-copy"

    shutil.copy2(str(srt_path), str(target_path))
    return target_path, "overwritten" if overwrite and target_exists else "copied"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy matching SRT files into each video's dedicated folder."
    )
    parser.add_argument("video_dir", help="Root directory containing videos in dedicated folders.")
    parser.add_argument("srt_dir", help="Root directory containing SRT files.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without copying files.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing SRT files in video folders.")
    args = parser.parse_args()

    video_dir = Path(args.video_dir).resolve()
    srt_dir = Path(args.srt_dir).resolve()

    if not video_dir.exists() or not video_dir.is_dir():
        raise NotADirectoryError(f"Video directory does not exist: {video_dir}")
    if not srt_dir.exists() or not srt_dir.is_dir():
        raise NotADirectoryError(f"SRT directory does not exist: {srt_dir}")

    videos = discover_source_videos(video_dir)
    if not videos:
        print("未找到候选源视频。")
        return

    srt_map, duplicate_srts = discover_srt_files(srt_dir)
    if not srt_map and not duplicate_srts:
        print("未找到 SRT 字幕文件。")
        return

    if duplicate_srts:
        print("警告: 发现重名字幕文件，这些名称将被跳过:")
        for stem_key, paths in duplicate_srts.items():
            path_text = " | ".join(str(path) for path in paths)
            print(f"  {stem_key}: {path_text}")
            srt_map.pop(stem_key, None)

    copied_count = 0
    skipped_count = 0
    missing_count = 0

    print(f"Found {len(videos)} candidate video(s).")
    print(f"Indexed {len(srt_map)} unique SRT file(s).")

    for video_path in videos:
        matched_srt = srt_map.get(video_path.stem.lower())
        if matched_srt is None:
            missing_count += 1
            print(f"Missing SRT: {video_path}")
            continue

        target_path, status = copy_matching_srt(
            video_path,
            matched_srt,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )

        if status in {"would-copy", "would-overwrite", "copied", "overwritten"}:
            copied_count += 1
            if status == "would-copy":
                action = "[DRY-RUN] Would copy"
            elif status == "would-overwrite":
                action = "[DRY-RUN] Would overwrite"
            elif status == "overwritten":
                action = "Overwritten"
            else:
                action = "Copied"
            print(f"{action}: {matched_srt} -> {target_path}")
            continue

        skipped_count += 1
        if status == "already-linked":
            print(f"Skipped (same file): {target_path}")
        else:
            print(f"Skipped (target exists): {target_path}")

    print("\n汇总:")
    print(f"  视频总数: {len(videos)}")
    print(f"  {'将复制/覆盖' if args.dry_run else '已复制/覆盖'}: {copied_count}")
    print(f"  缺少字幕: {missing_count}")
    print(f"  已跳过: {skipped_count}")


if __name__ == "__main__":
    main()