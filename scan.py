#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi"}

# Text-based subtitle codecs we know can be converted to SRT.
SUPPORTED_TEXT_CODECS = {
    "subrip",
    "srt",
    "ass",
    "ssa",
    "webvtt",
    "text",
    "mov_text",
}

PORTUGUESE_LANGUAGE_CODES = {
    "por",
    "pt",
    "pt-br",
    "pob",
}

ENGLISH_LANGUAGE_CODES = {
    "eng",
    "en",
    "english",
}


def run(cmd):
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=True,
    )


def find_external_portuguese(video):
    base = video.stem
    folder = video.parent

    patterns = [
        f"{base}.pt-BR.srt",
        f"{base}.pt-BR.*.srt",
        f"{base}.pob.srt",
        f"{base}.pob.*.srt",
    ]

    found = []

    for pattern in patterns:
        found.extend(folder.glob(pattern))

    return sorted(set(found))


def find_external_english(video):
    base = video.stem
    folder = video.parent

    patterns = [
        f"{base}.eng.srt",
        f"{base}.eng.hi.srt",
        f"{base}.eng.sdh.srt",
        f"{base}.en.srt",
        f"{base}.en.hi.srt",
        f"{base}.en.sdh.srt",
    ]

    found = []

    for pattern in patterns:
        found.extend(folder.glob(pattern))

    return sorted(set(found))


def probe_subtitles(video):
    result = run([
        "ffprobe",
        "-v", "error",
        "-select_streams", "s",
        "-show_entries",
        "stream=index,codec_name:stream_tags=language,title",
        "-of", "json",
        str(video),
    ])

    data = json.loads(result.stdout)

    return data.get("streams", [])


def normalize_language(value):
    if not value:
        return ""

    return value.strip().lower().replace("_", "-")


def find_embedded_portuguese(streams):
    matches = []

    for stream in streams:
        tags = stream.get("tags", {})
        language = normalize_language(tags.get("language"))

        if language in PORTUGUESE_LANGUAGE_CODES:
            matches.append(stream)

    return matches


def find_embedded_english(streams):
    matches = []

    for subtitle_position, stream in enumerate(streams):
        tags = stream.get("tags", {})
        language = normalize_language(tags.get("language"))

        if language in ENGLISH_LANGUAGE_CODES:
            matches.append({
                "subtitle_position": subtitle_position,
                "stream_index": stream.get("index"),
                "codec": stream.get("codec_name", ""),
                "title": tags.get("title", ""),
            })

    return matches


def episode_label(video, root):
    try:
        relative = video.relative_to(root)
        show = relative.parts[0] if len(relative.parts) > 1 else video.parent.name
    except ValueError:
        show = video.parent.name

    return show, video.stem


def classify(video):
    external_pt = find_external_portuguese(video)

    if external_pt:
        return {
            "status": "SKIP",
            "source": "external Portuguese",
            "detail": external_pt[0].name,
        }

    external_eng = find_external_english(video)

    try:
        streams = probe_subtitles(video)
    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        # External English is still usable even if ffprobe fails.
        if external_eng:
            return {
                "status": "TRANSLATE",
                "source": "external English",
                "detail": external_eng[0].name,
            }

        return {
            "status": "ERROR",
            "source": "ffprobe",
            "detail": str(exc),
        }

    embedded_pt = find_embedded_portuguese(streams)

    if embedded_pt:
        stream = embedded_pt[0]

        return {
            "status": "SKIP",
            "source": "embedded Portuguese",
            "detail": (
                f"stream={stream.get('index')} "
                f"codec={stream.get('codec_name', '?')}"
            ),
        }

    if external_eng:
        return {
            "status": "TRANSLATE",
            "source": "external English",
            "detail": external_eng[0].name,
        }

    embedded_eng = find_embedded_english(streams)

    if not embedded_eng:
        return {
            "status": "NO_SOURCE",
            "source": "-",
            "detail": "no English subtitle found",
        }

    # Prefer a text-based English stream if multiple exist.
    for stream in embedded_eng:
        codec = stream["codec"].lower()

        if codec in SUPPORTED_TEXT_CODECS:
            return {
                "status": "TRANSLATE",
                "source": "embedded English",
                "detail": (
                    f"stream={stream['stream_index']} "
                    f"codec={codec}"
                ),
            }

    stream = embedded_eng[0]

    return {
        "status": "UNSUPPORTED",
        "source": "embedded English",
        "detail": (
            f"stream={stream['stream_index']} "
            f"codec={stream['codec'] or '?'}"
        ),
    }


def find_videos(root):
    for path in sorted(root.rglob("*")):
        if (
            path.is_file()
            and path.suffix.lower() in VIDEO_EXTENSIONS
        ):
            yield path


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Read-only scan of TV episodes for "
            "Brazilian Portuguese subtitle translation candidates."
        )
    )

    parser.add_argument(
        "root",
        type=Path,
        help="TV library root",
    )

    parser.add_argument(
        "--show",
        help="Only scan shows whose folder name contains this text",
    )

    parser.add_argument(
        "--candidates-only",
        action="store_true",
        help="Only display episodes that can be translated",
    )

    parser.add_argument(
        "--limit",
        type=int,
        help="Stop after scanning this many video files",
    )

    args = parser.parse_args()

    root = args.root.expanduser().resolve()

    if not root.exists():
        sys.exit(f"Library does not exist: {root}")

    if not root.is_dir():
        sys.exit(f"Not a directory: {root}")

    videos = list(find_videos(root))

    if args.show:
        needle = args.show.lower()

        videos = [
            video
            for video in videos
            if needle in str(video.parent.relative_to(root)).lower()
        ]

    if args.limit is not None:
        videos = videos[:args.limit]

    if not videos:
        sys.exit("No video files found.")

    print()
    print("Subtitle Translator - Read-only Scan")
    print("=" * 78)
    print(f"Library: {root}")

    if args.show:
        print(f"Show filter: {args.show}")

    print(f"Videos to scan: {len(videos)}")
    print()

    results = []
    counters = Counter()

    for position, video in enumerate(videos, start=1):
        show, episode = episode_label(video, root)

        print(
            f"\rScanning {position}/{len(videos)}...",
            end="",
            flush=True,
        )

        result = classify(video)

        result.update({
            "show": show,
            "episode": episode,
            "video": video,
        })

        results.append(result)
        counters[result["status"]] += 1

    print("\r" + (" " * 40) + "\r", end="")

    print(
        f"{'STATUS':<12} "
        f"{'SHOW':<24} "
        f"{'EPISODE / SOURCE'}"
    )

    print("-" * 100)

    for result in results:
        if (
            args.candidates_only
            and result["status"] != "TRANSLATE"
        ):
            continue

        print(
            f"{result['status']:<12} "
            f"{result['show'][:23]:<24} "
            f"{result['episode']}"
        )

        print(
            f"{'':<12} "
            f"{'':<24} "
            f"-> {result['source']}: {result['detail']}"
        )

    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)

    print(f"Total scanned: {len(results)}")
    print(f"Skip:          {counters['SKIP']}")
    print(f"Translate:     {counters['TRANSLATE']}")
    print(f"No source:     {counters['NO_SOURCE']}")
    print(f"Unsupported:   {counters['UNSUPPORTED']}")
    print(f"Errors:        {counters['ERROR']}")

    print()
    print("No files were modified.")


if __name__ == "__main__":
    main()
