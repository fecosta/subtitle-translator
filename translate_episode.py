#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
}


def run(cmd):
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=True,
    )


def find_existing_portuguese(
    video: Path,
):
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
        found.extend(
            folder.glob(pattern)
        )

    return sorted(
        set(found)
    )


def find_external_english(
    video: Path,
):
    base = video.stem
    folder = video.parent

    candidates = [
        folder / f"{base}.eng.srt",
        folder / f"{base}.eng.hi.srt",
        folder / f"{base}.eng.sdh.srt",
    ]

    return next(
        (
            path
            for path in candidates
            if path.exists()
        ),
        None,
    )


def probe_subtitles(
    video: Path,
):
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            (
                "stream=index,codec_name:"
                "stream_tags=language,title"
            ),
            "-of",
            "json",
            str(video),
        ]
    )

    data = json.loads(
        result.stdout
    )

    return data.get(
        "streams",
        [],
    )


def find_embedded_english(
    video: Path,
):
    streams = probe_subtitles(
        video
    )

    for (
        subtitle_position,
        stream,
    ) in enumerate(streams):

        tags = stream.get(
            "tags",
            {},
        )

        language = (
            tags.get(
                "language",
                "",
            )
            .lower()
        )

        if language in {
            "eng",
            "en",
            "english",
        }:
            return {
                "subtitle_position": (
                    subtitle_position
                ),
                "stream_index": (
                    stream.get(
                        "index"
                    )
                ),
                "codec": (
                    stream.get(
                        "codec_name"
                    )
                ),
                "title": (
                    tags.get(
                        "title"
                    )
                ),
            }

    return None


def extract_embedded_english(
    video: Path,
    subtitle_position: int,
    output: Path,
):
    run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-map",
            f"0:s:{subtitle_position}",
            "-c:s",
            "srt",
            str(output),
        ]
    )


def project_python():
    python = (
        Path(__file__).parent
        / ".venv"
        / "bin"
        / "python"
    )

    if not python.exists():
        raise RuntimeError(
            "Project Python not found: "
            f"{python}"
        )

    return python


def call_translator(
    translator: Path,
    source: Path,
    output: Path,
):
    subprocess.run(
        [
            str(
                project_python()
            ),
            str(translator),
            str(source),
            "--output",
            str(output),
        ],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Translate an episode's "
            "English subtitles to "
            "Brazilian Portuguese."
        )
    )

    parser.add_argument(
        "video",
        type=Path,
        help="Episode video file",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Translate even if "
            "Portuguese subtitles "
            "already exist"
        ),
    )

    args = parser.parse_args()

    video = (
        args.video
        .expanduser()
        .resolve()
    )

    if not video.exists():
        sys.exit(
            f"Video does not exist: "
            f"{video}"
        )

    if (
        video.suffix.lower()
        not in VIDEO_EXTENSIONS
    ):
        sys.exit(
            "Unsupported video type: "
            f"{video.suffix}"
        )

    translator = (
        Path(__file__)
        .with_name(
            "translate_srt.py"
        )
    )

    if not translator.exists():
        sys.exit(
            f"Translator not found: "
            f"{translator}"
        )

    print()
    print(
        f"Episode: {video.name}"
    )
    print()

    existing_pt = (
        find_existing_portuguese(
            video
        )
    )

    if (
        existing_pt
        and not args.force
    ):
        print(
            "SKIP: Portuguese subtitle "
            "already exists:"
        )

        for subtitle in existing_pt:
            print(
                f"  {subtitle.name}"
            )

        return

    output = video.with_name(
        f"{video.stem}.pt-BR.srt"
    )

    external_eng = (
        find_external_english(
            video
        )
    )

    if external_eng:
        print(
            "English source: "
            "external subtitle"
        )

        print(
            f"  {external_eng.name}"
        )

        print()

        call_translator(
            translator,
            external_eng,
            output,
        )

        print()
        print(
            f"Created: {output}"
        )

        return

    print(
        "No external English subtitle found."
    )

    print(
        "Checking embedded subtitle tracks..."
    )

    embedded = (
        find_embedded_english(
            video
        )
    )

    if not embedded:
        print(
            "SKIP: No English subtitle found."
        )
        return

    print(
        "English source: embedded stream "
        f"{embedded['stream_index']}"
    )

    print(
        f"Codec: {embedded['codec']}"
    )

    if embedded["title"]:
        print(
            f"Title: {embedded['title']}"
        )

    print()

    with tempfile.TemporaryDirectory(
        prefix="subtitle-translator-"
    ) as tmp_dir:

        extracted = (
            Path(tmp_dir)
            / f"{video.stem}.eng.srt"
        )

        print(
            "Extracting embedded "
            "English subtitle..."
        )

        extract_embedded_english(
            video,
            embedded[
                "subtitle_position"
            ],
            extracted,
        )

        if (
            not extracted.exists()
            or extracted.stat().st_size == 0
        ):
            sys.exit(
                "Extraction failed: "
                "generated subtitle is empty."
            )

        print(
            "Extracted temporary subtitle: "
            f"{extracted.stat().st_size / 1024:.1f} KiB"
        )

        print()
        print(
            "Starting translation..."
        )
        print()

        call_translator(
            translator,
            extracted,
            output,
        )

    print()
    print(
        "Temporary English subtitle removed."
    )

    print(
        f"Created: {output}"
    )


if __name__ == "__main__":
    main()
