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
DEFAULT_CHAO_LETTERS = ["˥", "˦", "˧", "˨", "˩", "꜒", "꜓", "꜔", "꜕", "꜖"]
DEFAULT_DIACRITIC_MARKERS = ["◌̀", "◌́", "◌̂", "◌̃", "◌̄", "◌̆", "◌̈", "◌̊", "◌̌", "◌̩", "◌̯"]
REDUPLICATED_CHAR_RE = re.compile(r"(.)\1+")
REDUPLICATED_DIGIT_RE = re.compile(r"(\d)\1+")


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


def normalize_diacritic_marker(marker: str) -> str:
    return marker.replace("◌", "") if marker else ""


def resolve_path(path_value: str | Path, base_dir: Path) -> Path:
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def configured_paths(value: Any, key: str) -> list[str | Path]:
    if isinstance(value, (str, Path)):
        return [value]
    if isinstance(value, list) and all(isinstance(item, (str, Path)) for item in value):
        return value
    raise ValueError(f"{key} must be a path string or a YAML list of path strings")


def rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def display_file_path(path: Path, data_roots: list[Path]) -> str:
    for root in data_roots:
        if root.is_file() and path == root:
            return path.name
        base = root.parent if root.is_file() else root
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.name


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


def add_example(
    examples: dict[str, list[dict[str, Any]]],
    key: str,
    *,
    source: SourceText,
    text: str,
    start: int,
    end: int,
    context_chars: int,
    limit: int,
) -> None:
    bucket = examples.setdefault(key, [])
    if len(bucket) >= limit:
        return
    left = max(0, start - context_chars)
    right = min(len(text), end + context_chars)
    bucket.append(
        {
            "file": source.path,
            "source_type": source.source_type,
            "match": text[start:end],
            "context": text[left:right],
            "span": (start, end),
        }
    )


def markdown_escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def compile_chao_letters_regex(chao_letters: list[str]) -> re.Pattern:
    escaped = [re.escape(letter) for letter in chao_letters if letter]
    if not escaped:
        return re.compile(r"$a")
    return re.compile("|".join(sorted(escaped, key=len, reverse=True)))


def diacritic_hits(text: str, markers: list[str]) -> list[tuple[int, int, str]]:
    normalized_markers = {normalize_diacritic_marker(marker) for marker in markers}
    normalized_markers.discard("")
    hits: list[tuple[int, int, str]] = []
    for idx, char in enumerate(text):
        if char in normalized_markers:
            hits.append((idx, idx + 1, char))
            continue
        decomposed = unicodedata.normalize("NFD", char)
        for mark in decomposed:
            if mark in normalized_markers:
                hits.append((idx, idx + 1, mark))
    return hits


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
    if len(char) == 1 and unicodedata.name(char, None) is None:
        return f"`U+{ord(char):04X}`"
    if char == "`":
        return "`` ` ``"
    return f"`{char}`"


def is_punctuation(char: str) -> bool:
    return char in PUNCTUATION_TO_REPORT or unicodedata.category(char).startswith("P")


def analyze_sources(
    sources: list[SourceText],
    *,
    example_limit: int,
    context_chars: int,
    chao_number_regex: re.Pattern,
    chao_letters_regex: re.Pattern,
    diacritic_markers: list[str],
) -> dict[str, Any]:
    char_counts: Counter[str] = Counter()
    uppercase_counts: Counter[str] = Counter()
    quote_counts: Counter[str] = Counter()
    punctuation_counts: Counter[str] = Counter()
    digit_counts: Counter[str] = Counter()
    tone_counts: Counter[str] = Counter()
    chao_letter_counts: Counter[str] = Counter()
    combining_counts: Counter[str] = Counter()
    configured_diacritic_counts: Counter[str] = Counter()
    ipa_modifier_counts: Counter[str] = Counter()
    reduplicated_char_counts: Counter[str] = Counter()
    reduplicated_digit_counts: Counter[str] = Counter()
    per_file_rows: list[dict[str, Any]] = []
    source_type_counts: Counter[str] = Counter()
    char_examples: dict[str, list[dict[str, Any]]] = {}
    marker_examples: dict[str, list[dict[str, Any]]] = {}

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

            for idx, char in enumerate(text):
                add_example(
                    char_examples,
                    char,
                    source=source,
                    text=text,
                    start=idx,
                    end=idx + 1,
                    context_chars=context_chars,
                    limit=example_limit,
                )

            for match in chao_number_regex.finditer(text):
                tone = match.group(0)
                tone_counts[tone] += 1
                file_tone_counts[tone] += 1
                add_example(
                    marker_examples,
                    f"chao_number:{tone}",
                    source=source,
                    text=text,
                    start=match.start(),
                    end=match.end(),
                    context_chars=context_chars,
                    limit=example_limit,
                )

            for match in chao_letters_regex.finditer(text):
                value = match.group(0)
                chao_letter_counts[value] += 1
                add_example(
                    marker_examples,
                    f"chao_letter:{value}",
                    source=source,
                    text=text,
                    start=match.start(),
                    end=match.end(),
                    context_chars=context_chars,
                    limit=example_limit,
                )

            for start, end, mark in diacritic_hits(text, diacritic_markers):
                configured_diacritic_counts[mark] += 1
                add_example(
                    marker_examples,
                    f"diacritic:{mark}",
                    source=source,
                    text=text,
                    start=start,
                    end=end,
                    context_chars=context_chars,
                    limit=example_limit,
                )

            for match in REDUPLICATED_CHAR_RE.finditer(text):
                value = match.group(0)
                reduplicated_char_counts[value] += 1
                add_example(
                    marker_examples,
                    f"reduplicated_char:{value}",
                    source=source,
                    text=text,
                    start=match.start(),
                    end=match.end(),
                    context_chars=context_chars,
                    limit=example_limit,
                )

            for match in REDUPLICATED_DIGIT_RE.finditer(text):
                value = match.group(0)
                reduplicated_digit_counts[value] += 1
                add_example(
                    marker_examples,
                    f"reduplicated_digit:{value}",
                    source=source,
                    text=text,
                    start=match.start(),
                    end=match.end(),
                    context_chars=context_chars,
                    limit=example_limit,
                )

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
        "chao_letter_counts": chao_letter_counts,
        "combining_counts": combining_counts,
        "configured_diacritic_counts": configured_diacritic_counts,
        "ipa_modifier_counts": ipa_modifier_counts,
        "reduplicated_char_counts": reduplicated_char_counts,
        "reduplicated_digit_counts": reduplicated_digit_counts,
        "per_file_rows": per_file_rows,
        "source_type_counts": source_type_counts,
        "char_examples": char_examples,
        "marker_examples": marker_examples,
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


def examples_for_key(
    examples: dict[str, list[dict[str, Any]]],
    key: str,
    *,
    data_roots: list[Path],
) -> str:
    rows = []
    for example in examples.get(key, []):
        rows.append(
            f"`{markdown_escape_cell(example['context'])}` "
            f"({display_file_path(example['file'], data_roots)})"
        )
    return "<br>".join(rows) if rows else "-"


def table_for_counter_with_examples(
    counter: Counter[str],
    examples: dict[str, list[dict[str, Any]]],
    *,
    data_roots: list[Path],
    key_prefix: str = "",
    limit: int | None = None,
) -> list[str]:
    if not counter:
        return ["No matches found."]

    rows = ["| Value | Count | Examples |", "| --- | ---: | --- |"]
    for value, count in counter.most_common(limit):
        key = f"{key_prefix}{value}"
        rows.append(
            f"| {char_display(value) if len(value) == 1 else f'`{value}`'} | "
            f"{count:,} | {examples_for_key(examples, key, data_roots=data_roots)} |"
        )
    return rows


def tone_table(counter: Counter[str]) -> list[str]:
    if not counter:
        return ["No Chao-style tone number sequences found."]
    rows = ["| Tone number | Count |", "| --- | ---: |"]
    for tone, count in sorted(counter.items(), key=lambda item: (-item[1], item[0])):
        rows.append(f"| `{tone}` | {count:,} |")
    return rows


def tone_table_with_examples(
    counter: Counter[str],
    examples: dict[str, list[dict[str, Any]]],
    *,
    data_roots: list[Path],
) -> list[str]:
    if not counter:
        return ["No Chao-style tone number sequences found."]
    rows = ["| Tone number | Count | Examples |", "| --- | ---: | --- |"]
    for tone, count in sorted(counter.items(), key=lambda item: (-item[1], item[0])):
        rows.append(
            f"| `{tone}` | {count:,} | "
            f"{examples_for_key(examples, f'chao_number:{tone}', data_roots=data_roots)} |"
        )
    return rows


def build_report(config: dict[str, Any], config_path: Path) -> tuple[str, Path]:
    config_dir = config_path.parent.resolve()
    language = str(config.get("language", "language"))
    title = str(config.get("title", f"{language} Data Inventory"))
    configured_data_roots = config.get("data_roots")
    if configured_data_roots is None:
        configured_data_roots = config.get("data_root", "data")
        configured_data_root_key = "data_root"
    else:
        configured_data_root_key = "data_roots"
    configured_data_roots = configured_paths(
        configured_data_roots,
        configured_data_root_key,
    )
    data_roots = [
        resolve_path(path, config_dir)
        for path in configured_data_roots
    ]

    report_dir = resolve_path(config.get("report_dir", "reports"), config_dir)
    report_name = str(config.get("report_name", "{language}_data_inventory.md")).format(
        language=language
    )
    globs = config.get("transcription_globs", ["**/*.txt", "**/*.eaf"])
    if not isinstance(globs, list):
        raise ValueError("transcription_globs must be a YAML list")
    example_limit = int(config.get("example_limit", 3))
    context_chars = int(config.get("context_chars", 24))
    chao_number_regex = re.compile(str(config.get("chao_number_regex", r"(?<!\d)[1-5]{2,3}(?!\d)")))
    chao_letters = config.get("chao_letters", DEFAULT_CHAO_LETTERS)
    if not isinstance(chao_letters, list):
        raise ValueError("chao_letters must be a YAML list")
    diacritic_markers = config.get("diacritic_markers", DEFAULT_DIACRITIC_MARKERS)
    if not isinstance(diacritic_markers, list):
        raise ValueError("diacritic_markers must be a YAML list")

    files = []
    for root in data_roots:
        files.extend(find_transcription_files(root, [str(item) for item in globs]))
    # Remove duplicates and sort
    files = sorted(set(files))
    sources = read_sources(files)
    analysis = analyze_sources(
        sources,
        example_limit=example_limit,
        context_chars=context_chars,
        chao_number_regex=chao_number_regex,
        chao_letters_regex=compile_chao_letters_regex([str(item) for item in chao_letters]),
        diacritic_markers=[str(item) for item in diacritic_markers],
    )
    out_path = report_dir / report_name

    source_type_counts: Counter[str] = analysis["source_type_counts"]
    per_file_rows: list[dict[str, Any]] = analysis["per_file_rows"]
    char_counts: Counter[str] = analysis["char_counts"]
    char_examples: dict[str, list[dict[str, Any]]] = analysis["char_examples"]
    marker_examples: dict[str, list[dict[str, Any]]] = analysis["marker_examples"]

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
        f"- Data roots: `{', '.join(str(root) for root in data_roots)}`",
        f"- Report path: `{out_path}`",
        f"- Example limit per item: {example_limit}",
        f"- Example context characters per side: {context_chars}",
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
            f"| `{display_file_path(row['path'], data_roots)}` | {row['source_type']} | "
            f"{row['annotations']:,} | {row['unique_chars']:,} | "
            f"{tone_text} | {upper_text} | {quote_text} |"
        )

    lines.extend(["", "## Chao Tone Number Sequences", ""])
    lines.extend(tone_table_with_examples(analysis["tone_counts"], marker_examples, data_roots=data_roots))

    lines.extend(["", "## Chao Tone Letters", ""])
    lines.extend(
        table_for_counter_with_examples(
            analysis["chao_letter_counts"],
            marker_examples,
            data_roots=data_roots,
            key_prefix="chao_letter:",
        )
    )

    lines.extend(["", "## Uppercase Letters", ""])
    lines.extend(
        table_for_counter_with_examples(
            analysis["uppercase_counts"],
            char_examples,
            data_roots=data_roots,
        )
    )

    lines.extend(["", "## Quotation Marks And Apostrophes", ""])
    lines.extend(
        table_for_counter_with_examples(
            analysis["quote_counts"],
            char_examples,
            data_roots=data_roots,
        )
    )

    lines.extend(["", "## Digits", ""])
    lines.extend(
        table_for_counter_with_examples(
            analysis["digit_counts"],
            char_examples,
            data_roots=data_roots,
        )
    )

    lines.extend(["", "## Punctuation And Symbols", ""])
    lines.extend(
        table_for_counter_with_examples(
            analysis["punctuation_counts"],
            char_examples,
            data_roots=data_roots,
        )
    )

    lines.extend(["", "## IPA Modifier Characters", ""])
    lines.extend(
        table_for_counter_with_examples(
            analysis["ipa_modifier_counts"],
            char_examples,
            data_roots=data_roots,
        )
    )

    lines.extend(["", "## Configured Diacritic Markers", ""])
    lines.extend(
        table_for_counter_with_examples(
            analysis["configured_diacritic_counts"],
            marker_examples,
            data_roots=data_roots,
            key_prefix="diacritic:",
        )
    )

    lines.extend(["", "## Combining Diacritic Characters", ""])
    lines.extend(
        table_for_counter_with_examples(
            analysis["combining_counts"],
            char_examples,
            data_roots=data_roots,
        )
    )

    lines.extend(["", "## Reduplicated Character Runs", ""])
    lines.extend(
        table_for_counter_with_examples(
            analysis["reduplicated_char_counts"],
            marker_examples,
            data_roots=data_roots,
            key_prefix="reduplicated_char:",
        )
    )

    lines.extend(["", "## Reduplicated Digit Runs", ""])
    lines.extend(
        table_for_counter_with_examples(
            analysis["reduplicated_digit_counts"],
            marker_examples,
            data_roots=data_roots,
            key_prefix="reduplicated_digit:",
        )
    )

    lines.extend(["", "## Complete Character Inventory", ""])
    lines.extend(
        table_for_counter_with_examples(
            char_counts,
            char_examples,
            data_roots=data_roots,
        )
    )
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
