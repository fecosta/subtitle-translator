#!/usr/bin/env python3

import argparse
import subprocess
import sys
from pathlib import Path


VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
}


def candidate_has_portuguese(
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

    return any(
        match.exists()
        for pattern in patterns
        for match in folder.glob(
            pattern
        )
    )


def find_candidates(
    root: Path,
):
    for video in sorted(
        root.rglob("*")
    ):
        if not video.is_file():
            continue

        if (
            video.suffix.lower()
            not in VIDEO_EXTENSIONS
        ):
            continue

        if candidate_has_portuguese(
            video
        ):
            continue

        yield video


def project_python():
    python = (
        Path(__file__).parent
        / ".venv"
        / "bin"
        / "python"
    )

    if not python.exists():
        sys.exit(
            "Project Python not found: "
            f"{python}"
        )

    return python


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Controlled batch "
            "subtitle translation."
        )
    )

    parser.add_argument(
        "root",
        type=Path,
        help="TV library root",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help=(
            "Maximum number of episodes "
            "to translate (default: 3)"
        ),
    )

    parser.add_argument(
        "--show",
        help=(
            "Only process paths "
            "containing this show name"
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Show what would be attempted "
            "without translating"
        ),
    )

    args = parser.parse_args()

    root = (
        args.root
        .expanduser()
        .resolve()
    )

    translator = (
        Path(__file__)
        .with_name(
            "translate_episode.py"
        )
    )

    if not translator.exists():
        sys.exit(
            "translate_episode.py "
            f"not found: {translator}"
        )

    candidates = list(
        find_candidates(
            root
        )
    )

    if args.show:
        needle = (
            args.show.lower()
        )

        candidates = [
            video
            for video in candidates
            if needle
            in str(
                video.relative_to(root)
            ).lower()
        ]

    candidates = candidates[
        :args.limit
    ]

    if not candidates:
        print(
            "No candidates found."
        )
        return

    print(
        "Candidates selected: "
        f"{len(candidates)}"
    )

    print()

    for index, video in enumerate(
        candidates,
        start=1,
    ):
        print(
            "=" * 80
        )

        print(
            f"[{index}/{len(candidates)}] "
            f"{video}"
        )

        print(
            "=" * 80
        )

        if args.dry_run:
            print(
                "DRY RUN - no translation "
                "performed."
            )
            continue

        result = subprocess.run(
            [
                str(
                    project_python()
                ),
                str(translator),
                str(video),
            ],
            text=True,
        )

        if result.returncode != 0:
            print()
            print(
                f"FAILED: {video.name}"
            )

            print(
                "Stopping batch to avoid "
                "continuing after an "
                "unexpected error."
            )

            sys.exit(
                result.returncode
            )

        print()

    if args.dry_run:
        print()
        print(
            "Dry run completed. "
            "No files were modified."
        )

    else:
        print()
        print(
            "Batch completed successfully."
        )


if __name__ == "__main__":
    main()
