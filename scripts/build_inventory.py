#!/usr/bin/env python3
"""Build a transcription character inventory from raw ASR annotation files."""

from __future__ import annotations

import argparse
import fnmatch
import html
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from datetime import date


DEFAULT_CONFIG = Path("config/inventory.yaml")

QUOTE_CHARS = set("\"'`´‘’‚‛“”„‟«»‹›")
PUNCTUATION_TO_REPORT = set(",.;:!?¿¡#()[]{}<>/\\|-_+=*&^%$@~")
TONE_NUMBER_RE = re.compile(r"(?<!\d)([1-5]{2})(?!\d)")


@dataclass(frozen=True)
class SourceText:
    path: Path
    source_type: str
    texts: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Inventory YAML config. Default: {DEFAULT_CONFIG}",
    )
    return parser.parse_args()


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value in {"null", "None", "~"}:
        return None
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def read_simple_yaml(path: Path) -> dict[str, Any]:
    """Read the small YAML subset used by this project config."""
    config: dict[str, Any] = {}
    current_list_key: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line_without_comment = raw_line.split("#", 1)[0].rstrip()
        if not line_without_comment.strip():
            continue

        stripped = line_without_comment.strip()
        if stripped.startswith("- "):
            if current_list_key is None:
                raise ValueError(f"List item without key in {path}: {raw_line}")
            config[current_list_key].append(parse_scalar(stripped[2:]))
            continue

        if ":" not in stripped:
            raise ValueError(f"Unsupported YAML line in {path}: {raw_line}")

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            config[key] = parse_scalar(value)
            current_list_key = None
        else:
            config[key] = []
            current_list_key = key

    return config


def resolve_path(path_value: str | Path, base_dir: Path) -> Path:
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def find_transcription_files(data_root: Path, globs: list[str]) -> list[Path]:
    if data_root.is_file():
        if any(data_root.match(pattern) or fnmatch.fnmatch(data_root.name, pattern) for pattern in globs):
            return [data_root]
        return []

    paths: set[Path] = set()
    for pattern in globs:
        paths.update(path for path in data_root.glob(pattern) if path.is_file())
    return sorted(paths)


def read_txt_texts(path: Path) -> list[str]:
    texts: list[str] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3 and looks_like_time(parts[0]) and looks_like_time(parts[1]):
            text = "\t".join(parts[2:]).strip()
        elif len(parts) >= 4 and looks_like_time(parts[1]) and looks_like_time(parts[2]):
            text = "\t".join(parts[3:]).strip()
        else:
            text = line
        if text:
            texts.append(text)
    return texts


def looks_like_time(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def read_eaf_texts(path: Path) -> list[str]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []

    texts: list[str] = []
    for element in root.iter():
        if strip_namespace(element.tag) != "ANNOTATION_VALUE":
            continue
        text = "".join(element.itertext()).strip()
        if text:
            texts.append(html.unescape(text))
    return texts


def strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def read_sources(files: list[Path]) -> list[SourceText]:
    sources: list[SourceText] = []
    for path in files:
        suffix = path.suffix.lower()
        if suffix == ".eaf":
            texts = read_eaf_texts(path)
            source_type = "eaf"
        elif suffix == ".txt":
            texts = read_txt_texts(path)
            source_type = "txt"
        else:
            continue
        sources.append(SourceText(path=path, source_type=source_type, texts=texts))
    return sources


def char_name(char: str) -> str:
    if char == " ":
        return "SPACE"
    if char == "\t":
        return "TAB"
    return unicodedata.name(char, "UNKNOWN")


def char_display(char: str) -> str:
    if char == " ":
        return "`SPACE`"
    if char == "\t":
        return "`TAB`"
    if char == "`":
        return "`` ` ``"
    return f"`{char}`"


def is_punctuation(char: str) -> bool:
    return char in PUNCTUATION_TO_REPORT or unicodedata.category(char).startswith("P")


def analyze_sources(sources: list[SourceText]) -> dict[str, Any]:
    char_counts: Counter[str] = Counter()
    uppercase_counts: Counter[str] = Counter()
    quote_counts: Counter[str] = Counter()
    punctuation_counts: Counter[str] = Counter()
    digit_counts: Counter[str] = Counter()
    tone_counts: Counter[str] = Counter()
    combining_counts: Counter[str] = Counter()
    ipa_modifier_counts: Counter[str] = Counter()
    per_file_rows: list[dict[str, Any]] = []
    source_type_counts: Counter[str] = Counter()

    total_annotations = 0
    total_nonempty_chars = 0

    for source in sources:
        source_type_counts[source.source_type] += 1
        file_char_counts: Counter[str] = Counter()
        file_tone_counts: Counter[str] = Counter()
        file_uppercase_counts: Counter[str] = Counter()
        file_quote_counts: Counter[str] = Counter()

        for text in source.texts:
            total_annotations += 1
            total_nonempty_chars += len(text)
            char_counts.update(text)
            file_char_counts.update(text)

            for char in text:
                if char.isupper():
                    uppercase_counts[char] += 1
                    file_uppercase_counts[char] += 1
                if char in QUOTE_CHARS:
                    quote_counts[char] += 1
                    file_quote_counts[char] += 1
                if char.isdigit():
                    digit_counts[char] += 1
                if is_punctuation(char):
                    punctuation_counts[char] += 1
                if unicodedata.category(char) == "Mn":
                    combining_counts[char] += 1
                if char in {"ʰ", "ʲ", "ʷ", "ː", "ˑ", "ˀ", "ˁ", "ˤ", "ʼ"}:
                    ipa_modifier_counts[char] += 1

            for tone in TONE_NUMBER_RE.findall(text):
                tone_counts[tone] += 1
                file_tone_counts[tone] += 1

        per_file_rows.append(
            {
                "path": source.path,
                "source_type": source.source_type,
                "annotations": len(source.texts),
                "unique_chars": len(file_char_counts),
                "tone_numbers": file_tone_counts,
                "uppercase": file_uppercase_counts,
                "quotes": file_quote_counts,
            }
        )

    return {
        "char_counts": char_counts,
        "uppercase_counts": uppercase_counts,
        "quote_counts": quote_counts,
        "punctuation_counts": punctuation_counts,
        "digit_counts": digit_counts,
        "tone_counts": tone_counts,
        "combining_counts": combining_counts,
        "ipa_modifier_counts": ipa_modifier_counts,
        "per_file_rows": per_file_rows,
        "source_type_counts": source_type_counts,
        "total_annotations": total_annotations,
        "total_nonempty_chars": total_nonempty_chars,
    }


def table_for_counter(counter: Counter[str], *, limit: int | None = None) -> list[str]:
    if not counter:
        return ["No matches found."]

    rows = ["| Character | Count | Unicode name |", "| --- | ---: | --- |"]
    items = counter.most_common(limit)
    for char, count in items:
        rows.append(f"| {char_display(char)} | {count:,} | {char_name(char)} |")
    return rows


def tone_table(counter: Counter[str]) -> list[str]:
    if not counter:
        return ["No Chao-style tone number sequences found."]
    rows = ["| Tone number | Count |", "| --- | ---: |"]
    for tone, count in sorted(counter.items(), key=lambda item: (-item[1], item[0])):
        rows.append(f"| `{tone}` | {count:,} |")
    return rows


def build_report(config: dict[str, Any], config_path: Path) -> tuple[str, Path]:
    config_dir = config_path.parent.resolve()
    language = str(config.get("language", "language"))
    title = str(config.get("title", f"{language} Data Inventory"))
    configured_data_root = str(config.get("data_root", "data"))
    data_root = resolve_path(configured_data_root, config_dir)
    report_dir = resolve_path(config.get("report_dir", "reports"), config_dir)
    report_name = str(config.get("report_name", "{language}_data_inventory.md")).format(
        language=language
    )
    globs = config.get("transcription_globs", ["**/*.txt", "**/*.eaf"])
    if not isinstance(globs, list):
        raise ValueError("transcription_globs must be a YAML list")

    files = find_transcription_files(data_root, [str(item) for item in globs])
    sources = read_sources(files)
    analysis = analyze_sources(sources)
    out_path = report_dir / report_name
    file_display_base = data_root.parent if data_root.is_file() else data_root

    source_type_counts: Counter[str] = analysis["source_type_counts"]
    per_file_rows: list[dict[str, Any]] = analysis["per_file_rows"]
    char_counts: Counter[str] = analysis["char_counts"]

    lines = [
        f"# Inventory of Characters for {language}",
        "",
        f"Date: {date.today().isoformat()}",
        "",
        "This report inventories characters used in transcription source files.",
        "It does not inspect processed splits, CER-filtered data, model outputs, or generated manifests.",
        "",
        "## Config",
        "",
        f"- Config file: `{rel(config_path.resolve(), config_dir.parent)}`",
        f"- Data root: `{configured_data_root}`",
        f"- Report path: `{out_path}`",
        "- Transcription globs:",
    ]
    for pattern in globs:
        lines.append(f"  - `{pattern}`")

    lines.extend(
        [
            "",
            "## Source Files",
            "",
            f"- Files scanned: {len(sources):,}",
            f"- `.txt` files: {source_type_counts.get('txt', 0):,}",
            f"- `.eaf` files: {source_type_counts.get('eaf', 0):,}",
            f"- Non-empty transcription entries: {analysis['total_annotations']:,}",
            f"- Transcription characters counted: {analysis['total_nonempty_chars']:,}",
            f"- Unique characters: {len(char_counts):,}",
            "",
            "| File | Type | Entries | Unique chars | Tone numbers | Uppercase | Quotes |",
            "| --- | --- | ---: | ---: | --- | --- | --- |",
        ]
    )

    for row in per_file_rows:
        tone_text = ", ".join(f"{key}:{value}" for key, value in sorted(row["tone_numbers"].items())) or "-"
        upper_text = ", ".join(f"{key}:{value}" for key, value in sorted(row["uppercase"].items())) or "-"
        quote_text = ", ".join(f"{key}:{value}" for key, value in sorted(row["quotes"].items())) or "-"
        lines.append(
            f"| `{rel(row['path'], file_display_base)}` | {row['source_type']} | "
            f"{row['annotations']:,} | {row['unique_chars']:,} | "
            f"{tone_text} | {upper_text} | {quote_text} |"
        )

    lines.extend(["", "## Tone Number Sequences", ""])
    lines.extend(tone_table(analysis["tone_counts"]))

    lines.extend(["", "## Uppercase Letters", ""])
    lines.extend(table_for_counter(analysis["uppercase_counts"]))

    lines.extend(["", "## Quotation Marks And Apostrophes", ""])
    lines.extend(table_for_counter(analysis["quote_counts"]))

    lines.extend(["", "## Digits", ""])
    lines.extend(table_for_counter(analysis["digit_counts"]))

    lines.extend(["", "## Punctuation And Symbols", ""])
    lines.extend(table_for_counter(analysis["punctuation_counts"]))

    lines.extend(["", "## IPA Modifier Characters", ""])
    lines.extend(table_for_counter(analysis["ipa_modifier_counts"]))

    lines.extend(["", "## Combining Diacritics", ""])
    lines.extend(table_for_counter(analysis["combining_counts"]))

    lines.extend(["", "## Complete Character Inventory", ""])
    lines.extend(table_for_counter(char_counts))
    lines.append("")

    return "\n".join(lines), out_path


def main() -> None:
    args = parse_args()
    config_path = args.config.resolve()
    config = read_simple_yaml(config_path)
    report, out_path = build_report(config, config_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
