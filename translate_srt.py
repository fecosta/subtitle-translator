#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from openai import OpenAI


CHECKPOINT_VERSION = 1

BLOCK_RE = re.compile(
    r"(?ms)^\s*(\d+)\s*\n"
    r"(\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+"
    r"\d{2}:\d{2}:\d{2},\d{3}[^\n]*)\n"
    r"(.*?)(?=\n{2,}\d+\s*\n|\Z)"
)


def parse_srt(text: str):
    entries = []

    for match in BLOCK_RE.finditer(text.strip()):
        entries.append(
            {
                "number": int(match.group(1)),
                "timestamp": match.group(2).strip(),
                "text": match.group(3).strip(),
            }
        )

    return entries


def render_srt(entries):
    chunks = []

    for entry in entries:
        chunks.append(
            f"{entry['number']}\n"
            f"{entry['timestamp']}\n"
            f"{entry['text']}"
        )

    return "\n\n".join(chunks) + "\n"


def split_chunks(entries, size=40):
    for i in range(0, len(entries), size):
        yield entries[i:i + size]


def make_translation_input(entries):
    parts = []

    for entry in entries:
        parts.append(
            f"[[ID:{entry['number']}]]\n"
            f"{entry['text']}"
        )

    return "\n\n".join(parts)


def source_hash(text):
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def checkpoint_path_for(output_path):
    return output_path.with_name(
        output_path.name + ".checkpoint.json"
    )


def save_json_atomic(path, data):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )

            f.write("\n")

        os.replace(
            temp_name,
            path,
        )

    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass

        raise


def save_checkpoint(
    path,
    source_sha256,
    model,
    chunk_size,
    translations,
):
    data = {
        "version": CHECKPOINT_VERSION,
        "source_sha256": source_sha256,
        "model": model,
        "chunk_size": chunk_size,
        "translations": {
            str(entry_id): text
            for entry_id, text
            in sorted(translations.items())
        },
    }

    save_json_atomic(
        path,
        data,
    )


def load_checkpoint(
    path,
    source_sha256,
    model,
    chunk_size,
    valid_ids,
):
    if not path.exists():
        return {}

    print(
        f"Checkpoint found: {path}"
    )

    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
    except (
        json.JSONDecodeError,
        OSError,
    ) as exc:
        print(
            "Warning: checkpoint could not "
            f"be read and will be ignored: {exc}"
        )
        return {}

    if data.get("version") != CHECKPOINT_VERSION:
        print(
            "Warning: checkpoint version does "
            "not match. Starting fresh."
        )
        return {}

    if data.get("source_sha256") != source_sha256:
        print(
            "Warning: checkpoint belongs to a "
            "different source subtitle. "
            "Starting fresh."
        )
        return {}

    if data.get("model") != model:
        print(
            "Warning: checkpoint was created "
            f"with model {data.get('model')!r}, "
            f"but current model is {model!r}. "
            "Starting fresh."
        )
        return {}

    if data.get("chunk_size") != chunk_size:
        print(
            "Warning: checkpoint used chunk size "
            f"{data.get('chunk_size')}, "
            f"but current chunk size is {chunk_size}. "
            "Starting fresh."
        )
        return {}

    raw_translations = data.get(
        "translations",
        {},
    )

    restored = {}

    for raw_id, text in raw_translations.items():
        try:
            entry_id = int(raw_id)
        except (TypeError, ValueError):
            continue

        if entry_id not in valid_ids:
            continue

        if not isinstance(text, str):
            continue

        if not text.strip():
            continue

        restored[entry_id] = text.strip()

    print(
        "Checkpoint restored: "
        f"{len(restored)} translated entries."
    )

    return restored


def parse_translation_output(
    text,
    expected_entries,
):
    pattern = re.compile(
        r"(?ms)^\[\[ID:(\d+)\]\]\s*\n"
        r"(.*?)(?=\n{2,}\[\[ID:\d+\]\]|\Z)"
    )

    expected_ids = {
        entry["number"]
        for entry in expected_entries
    }

    translated = {}
    duplicates = []
    unexpected = []

    for match in pattern.finditer(
        text.strip()
    ):
        entry_id = int(
            match.group(1)
        )

        translated_text = (
            match.group(2).strip()
        )

        # Luna occasionally continues with IDs
        # that were not part of the request.
        # Ignore them safely.
        if entry_id not in expected_ids:
            unexpected.append(
                entry_id
            )
            continue

        if entry_id in translated:
            duplicates.append(
                entry_id
            )
            continue

        translated[entry_id] = (
            translated_text
        )

    missing = sorted(
        expected_ids - set(translated)
    )

    if duplicates:
        raise ValueError(
            "Duplicate translation IDs: "
            f"{sorted(set(duplicates))}"
        )

    if missing:
        raise ValueError(
            "Missing translation IDs: "
            f"{missing}"
        )

    if unexpected:
        print(
            "Warning: model returned "
            "unexpected IDs; ignored: "
            f"{unexpected}"
        )

    return translated


def translate_chunk(
    client,
    model,
    entries,
):
    input_text = make_translation_input(
        entries
    )

    instructions = """
Translate the supplied English subtitle dialogue into natural Brazilian Portuguese.

Rules:
- Translate into Brazilian Portuguese (pt-BR), not European Portuguese.
- Preserve meaning, tone, humor, slang, profanity, and conversational style.
- Prefer natural spoken Brazilian Portuguese instead of overly literal translation.
- Keep character names and proper nouns unchanged unless there is a standard Portuguese form.
- Preserve formatting tags such as <i>, </i>, <b>, </b> when present.
- Preserve meaningful line breaks when practical.
- Do not translate the [[ID:number]] markers.
- Return every provided ID exactly once.
- Never invent, continue, predict, or output an ID that was not provided in the input.
- Stop immediately after translating the final provided ID.
- Do not add explanations, markdown fences, notes, titles, or commentary.
- Output only blocks in this exact format:

[[ID:123]]
Translated subtitle text

[[ID:124]]
Translated subtitle text
""".strip()

    response = client.responses.create(
        model=model,
        instructions=instructions,
        input=input_text,
        store=False,
    )

    output = (
        response.output_text.strip()
    )

    return parse_translation_output(
        output,
        entries,
    )


def translate_chunk_resilient(
    client,
    model,
    entries,
    on_success=None,
):
    try:
        translations = translate_chunk(
            client,
            model,
            entries,
        )

        if on_success:
            on_success(
                translations
            )

        return translations

    except ValueError as exc:
        if len(entries) <= 10:
            raise

        midpoint = len(entries) // 2

        left = entries[:midpoint]
        right = entries[midpoint:]

        print()
        print(
            f"Chunk validation failed: {exc}"
        )

        print(
            "Retrying as smaller chunks "
            f"{left[0]['number']}-"
            f"{left[-1]['number']} "
            "and "
            f"{right[0]['number']}-"
            f"{right[-1]['number']}..."
        )

        translations = {}

        translations.update(
            translate_chunk_resilient(
                client,
                model,
                left,
                on_success=on_success,
            )
        )

        translations.update(
            translate_chunk_resilient(
                client,
                model,
                right,
                on_success=on_success,
            )
        )

        return translations


def validate(
    original_entries,
    translated_entries,
):
    if len(original_entries) != len(
        translated_entries
    ):
        raise ValueError(
            "Entry count mismatch: "
            f"original={len(original_entries)}, "
            f"translated={len(translated_entries)}"
        )

    for original, translated in zip(
        original_entries,
        translated_entries,
    ):
        if (
            original["number"]
            != translated["number"]
        ):
            raise ValueError(
                "Sequence mismatch: "
                f"{original['number']} != "
                f"{translated['number']}"
            )

        if (
            original["timestamp"]
            != translated["timestamp"]
        ):
            raise ValueError(
                "Timestamp changed at entry "
                f"{original['number']}"
            )

        if not translated["text"].strip():
            raise ValueError(
                "Empty translated text at "
                f"entry {original['number']}"
            )


def output_path_for(
    input_path: Path,
):
    name = input_path.name

    patterns = [
        r"\.eng\.hi\.srt$",
        r"\.eng\.sdh\.srt$",
        r"\.eng\.srt$",
    ]

    for pattern in patterns:
        if re.search(
            pattern,
            name,
            flags=re.IGNORECASE,
        ):
            new_name = re.sub(
                pattern,
                ".pt-BR.srt",
                name,
                flags=re.IGNORECASE,
            )

            return input_path.with_name(
                new_name
            )

    return input_path.with_name(
        input_path.stem
        + ".pt-BR.srt"
    )


def write_final_output(
    output_path,
    translated_text,
    original_entries,
):
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp_fd, tmp_name = (
        tempfile.mkstemp(
            prefix=(
                output_path.name + "."
            ),
            suffix=".tmp",
            dir=str(
                output_path.parent
            ),
            text=True,
        )
    )

    try:
        with os.fdopen(
            tmp_fd,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as f:
            f.write(
                translated_text
            )

        tmp_text = (
            Path(tmp_name).read_text(
                encoding="utf-8"
            )
        )

        tmp_entries = parse_srt(
            tmp_text
        )

        validate(
            original_entries,
            tmp_entries,
        )

        os.replace(
            tmp_name,
            output_path,
        )

    except Exception:
        try:
            os.unlink(
                tmp_name
            )
        except FileNotFoundError:
            pass

        raise


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Translate an English SRT "
            "subtitle to Brazilian Portuguese."
        )
    )

    parser.add_argument(
        "input",
        type=Path,
        help="English .srt subtitle file",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output path",
    )

    parser.add_argument(
        "--chunk-size",
        type=int,
        default=40,
        help=(
            "Subtitle entries per translation "
            "request (default: 40)"
        ),
    )

    parser.add_argument(
        "--model",
        default=os.environ.get(
            "SUBTITLE_MODEL",
            "gpt-6-luna",
        ),
        help="OpenAI model",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an existing "
            "output file"
        ),
    )

    args = parser.parse_args()

    input_path = (
        args.input
        .expanduser()
        .resolve()
    )

    if not input_path.exists():
        sys.exit(
            "Input file does not exist: "
            f"{input_path}"
        )

    if (
        input_path.suffix.lower()
        != ".srt"
    ):
        sys.exit(
            "Input must be an .srt file."
        )

    if args.chunk_size <= 0:
        sys.exit(
            "--chunk-size must be greater "
            "than zero."
        )

    output_path = (
        args.output
        .expanduser()
        .resolve()
        if args.output
        else output_path_for(
            input_path
        )
    )

    if (
        output_path.exists()
        and not args.force
    ):
        sys.exit(
            f"Output already exists: "
            f"{output_path}\n"
            "Use --force only if you "
            "intentionally want to replace it."
        )

    api_key = os.environ.get(
        "OPENAI_API_KEY"
    )

    if not api_key:
        sys.exit(
            "OPENAI_API_KEY is not set.\n"
            "Run: source "
            "~/subtitle-translator/env.sh"
        )

    source_text = (
        input_path.read_text(
            encoding="utf-8-sig",
            errors="strict",
        )
    )

    original_entries = parse_srt(
        source_text
    )

    if not original_entries:
        sys.exit(
            "No valid SRT entries "
            "were found."
        )

    valid_ids = {
        entry["number"]
        for entry in original_entries
    }

    sha256 = source_hash(
        source_text
    )

    checkpoint_path = (
        checkpoint_path_for(
            output_path
        )
    )

    print(
        f"Input:      {input_path}"
    )

    print(
        f"Output:     {output_path}"
    )

    print(
        f"Checkpoint: {checkpoint_path}"
    )

    print(
        f"Entries:    "
        f"{len(original_entries)}"
    )

    print(
        f"Model:      {args.model}"
    )

    print(
        f"Chunk:      {args.chunk_size}"
    )

    print()

    translated_by_id = (
        load_checkpoint(
            checkpoint_path,
            sha256,
            args.model,
            args.chunk_size,
            valid_ids,
        )
    )

    if translated_by_id:
        completed = len(
            translated_by_id
        )

        remaining = (
            len(original_entries)
            - completed
        )

        print(
            f"Resuming: {completed} "
            "entries already translated, "
            f"{remaining} remaining."
        )

        print()

    client = OpenAI(
        api_key=api_key
    )

    def checkpoint_success(
        new_translations,
    ):
        translated_by_id.update(
            new_translations
        )

        save_checkpoint(
            checkpoint_path,
            sha256,
            args.model,
            args.chunk_size,
            translated_by_id,
        )

        print(
            "Checkpoint saved: "
            f"{len(translated_by_id)}/"
            f"{len(original_entries)} "
            "entries complete."
        )

    chunks = list(
        split_chunks(
            original_entries,
            args.chunk_size,
        )
    )

    for chunk_number, chunk in enumerate(
        chunks,
        start=1,
    ):
        missing_entries = [
            entry
            for entry in chunk
            if entry["number"]
            not in translated_by_id
        ]

        if not missing_entries:
            print(
                f"Chunk "
                f"{chunk_number}/{len(chunks)} "
                f"(entries "
                f"{chunk[0]['number']}-"
                f"{chunk[-1]['number']}): "
                "already complete, skipping."
            )
            continue

        print(
            f"Translating chunk "
            f"{chunk_number}/{len(chunks)} "
            f"(entries "
            f"{missing_entries[0]['number']}-"
            f"{missing_entries[-1]['number']}, "
            f"{len(missing_entries)} remaining)..."
        )

        translate_chunk_resilient(
            client,
            args.model,
            missing_entries,
            on_success=checkpoint_success,
        )

    missing_final = [
        entry["number"]
        for entry in original_entries
        if entry["number"]
        not in translated_by_id
    ]

    if missing_final:
        raise ValueError(
            "Translation finished with "
            "missing IDs: "
            f"{missing_final}"
        )

    translated_entries = []

    for original in original_entries:
        translated_entries.append(
            {
                "number": (
                    original["number"]
                ),
                "timestamp": (
                    original["timestamp"]
                ),
                "text": (
                    translated_by_id[
                        original["number"]
                    ]
                ),
            }
        )

    validate(
        original_entries,
        translated_entries,
    )

    translated_text = render_srt(
        translated_entries
    )

    write_final_output(
        output_path,
        translated_text,
        original_entries,
    )

    # Only remove the checkpoint after
    # the final SRT has been successfully
    # written and validated.
    try:
        checkpoint_path.unlink()
    except FileNotFoundError:
        pass

    print()
    print(
        "Translation completed successfully."
    )

    print(
        f"Created: {output_path}"
    )

    print(
        "Validated entries: "
        f"{len(translated_entries)}"
    )

    print(
        "Checkpoint removed."
    )


if __name__ == "__main__":
    main()
