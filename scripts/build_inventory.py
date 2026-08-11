#!/usr/bin/env python3
"""Build a transcription character inventory from raw ASR annotation files."""

from __future__ import annotations

import argparse
import csv
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

import regex as unicode_regex


DEFAULT_CONFIG = Path("config/inventory.yaml")

QUOTE_CHARS = set("\"'`´‘’‚‛“”„‟«»‹›")
PUNCTUATION_TO_REPORT = set(",.;:!?¿¡#()[]{}<>/\\|-_+=*&^%$@~")
TONE_NUMBER_RE = re.compile(r"(?<!\d)([1-5]{2})(?!\d)")
DEFAULT_CHAO_LETTERS = ["˥", "˦", "˧", "˨", "˩"]
DEFAULT_DIACRITIC_MARKERS = ["◌̀", "◌́", "◌̂", "◌̃", "◌̄", "◌̆", "◌̈", "◌̊", "◌̌", "◌̩", "◌̯"]
IPA_MODIFIER_CHARS = frozenset({"ʰ", "ʲ", "ʷ", "ː", "ˑ", "ˀ", "ˁ", "ˤ", "ʼ"})
IPA_TIE_BARS = frozenset({"͡", "͜"})
UNATTACHED_MODIFIER = "(unattached)"
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
    if value.startswith("[") and value.endswith("]"):
        return parse_inline_list(value)
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


def parse_inline_list(value: str) -> list[Any]:
    """Parse the small inline-list subset supported by the project config."""
    inner = value[1:-1].strip()
    if not inner:
        return []

    items: list[Any] = []
    start = 0
    quote: str | None = None
    for index, char in enumerate(inner):
        if char in {'"', "'"}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        elif char == "," and quote is None:
            items.append(parse_scalar(inner[start:index]))
            start = index + 1
    if quote is not None:
        raise ValueError(f"Unterminated quote in inline list: {value}")
    items.append(parse_scalar(inner[start:]))
    return items


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


def configured_selector(value: Any, key: str) -> list[str]:
    if isinstance(value, str):
        selectors = [value.strip()]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        selectors = [item.strip() for item in value]
    else:
        raise ValueError(f"{key} must be a name, 'all', or a YAML list of names")

    if not selectors or any(not item for item in selectors):
        raise ValueError(f"{key} must not be empty")
    if len(selectors) > 1 and any(item.casefold() == "all" for item in selectors):
        raise ValueError(f"{key}: 'all' must be used by itself")

    folded = [item.casefold() for item in selectors]
    if len(folded) != len(set(folded)):
        raise ValueError(f"{key} contains duplicate names")
    return selectors


def display_selector(selector: list[str] | None) -> str:
    if selector is None:
        return "<not configured>"
    if len(selector) == 1:
        return selector[0]
    return "[" + ", ".join(selector) + "]"


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


def select_name(
    available_names: list[str],
    requested_names: str | list[str],
    *,
    path: Path,
    selector_kind: str,
    allow_all: bool,
) -> list[str]:
    requested_names = configured_selector(requested_names, selector_kind)
    if allow_all and requested_names[0].casefold() == "all":
        return available_names

    selected: set[str] = set()
    for requested_name in requested_names:
        matches = [
            name for name in available_names
            if name.casefold() == requested_name.casefold()
        ]
        if not matches:
            available = ", ".join(repr(name) for name in available_names) or "<none>"
            raise ValueError(
                f"{path}: requested {selector_kind} {requested_name!r} was not found; "
                f"available names: {available}"
            )
        if len(matches) > 1:
            matches_text = ", ".join(repr(name) for name in matches)
            raise ValueError(
                f"{path}: requested {selector_kind} {requested_name!r} is ambiguous "
                f"under case-insensitive matching: {matches_text}"
            )
        selected.add(matches[0])
    return [name for name in available_names if name in selected]


def read_csv_texts(path: Path, columns: str | list[str]) -> list[str]:
    try:
        handle = path.open(encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ValueError(f"{path}: could not open CSV file: {exc}") from exc

    with handle:
        try:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or []
            selected = select_name(
                fieldnames,
                columns,
                path=path,
                selector_kind="CSV column",
                allow_all=True,
            )
            texts = []
            for row_number, row in enumerate(reader, start=2):
                if None in row:
                    raise ValueError(
                        f"{path}:{row_number}: CSV row has more values than headers"
                    )
                for selected_column in selected:
                    value = row.get(selected_column)
                    if value is None:
                        raise ValueError(
                            f"{path}:{row_number}: CSV row has no value for column "
                            f"{selected_column!r}"
                        )
                    text = value.strip()
                    if text:
                        texts.append(text)
            return texts
        except csv.Error as exc:
            raise ValueError(f"{path}: malformed CSV: {exc}") from exc


def looks_like_time(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def read_eaf_texts(path: Path, tier_names: str | list[str]) -> list[str]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ValueError(f"{path}: could not parse EAF XML: {exc}") from exc

    tiers = [
        element for element in root.iter()
        if strip_namespace(element.tag) == "TIER"
    ]
    tier_ids = [tier.get("TIER_ID", "") for tier in tiers]
    selected_ids = set(
        select_name(
            tier_ids,
            tier_names,
            path=path,
            selector_kind="EAF tier",
            allow_all=True,
        )
    )
    texts: list[str] = []
    for tier in tiers:
        if tier.get("TIER_ID", "") not in selected_ids:
            continue
        for element in tier.iter():
            if strip_namespace(element.tag) != "ANNOTATION_VALUE":
                continue
            text = "".join(element.itertext()).strip()
            if text:
                texts.append(html.unescape(text))
    return texts


def read_textgrid_content(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{path}: could not read TextGrid: {exc}") from exc

    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        encoding = "utf-8"
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{path}: TextGrid is not valid UTF-8 or BOM-marked UTF-16: {exc}"
        ) from exc


TEXTGRID_ITEM_RE = re.compile(r"^\s*item\s+\[\d+\]:\s*$")
TEXTGRID_CLASS_RE = re.compile(r'^\s*class\s*=\s*"((?:""|[^"])*)"\s*$')
TEXTGRID_NAME_RE = re.compile(r'^\s*name\s*=\s*"((?:""|[^"])*)"\s*$')
TEXTGRID_VALUE_RE = re.compile(
    r'^\s*(?:text|mark)\s*=\s*"((?:""|[^"])*)"\s*$'
)


def unescape_textgrid_string(value: str) -> str:
    return value.replace('""', '"')


def read_textgrid_texts(path: Path, tier_names: str | list[str]) -> list[str]:
    content = read_textgrid_content(path)
    lines = content.splitlines()
    item_starts = [
        index for index, line in enumerate(lines)
        if TEXTGRID_ITEM_RE.match(line)
    ]
    if not item_starts:
        raise ValueError(
            f"{path}: unsupported or malformed TextGrid; expected Praat long "
            "text-format tier blocks"
        )

    tiers: list[tuple[str, str, list[str]]] = []
    for position, start in enumerate(item_starts):
        end = item_starts[position + 1] if position + 1 < len(item_starts) else len(lines)
        block = lines[start:end]
        tier_class = ""
        name = ""
        values: list[str] = []
        for line in block:
            if not tier_class:
                match = TEXTGRID_CLASS_RE.match(line)
                if match:
                    tier_class = unescape_textgrid_string(match.group(1))
                    continue
            if not name:
                match = TEXTGRID_NAME_RE.match(line)
                if match:
                    name = unescape_textgrid_string(match.group(1))
                    continue
            match = TEXTGRID_VALUE_RE.match(line)
            if match:
                value = unescape_textgrid_string(match.group(1)).strip()
                if value:
                    values.append(value)
        if tier_class not in {"IntervalTier", "TextTier"} or not name:
            raise ValueError(
                f"{path}: malformed TextGrid tier beginning at line {start + 1}"
            )
        tiers.append((name, tier_class, values))

    available_names = [name for name, _, _ in tiers]
    selected_names = set(
        select_name(
            available_names,
            tier_names,
            path=path,
            selector_kind="TextGrid tier",
            allow_all=True,
        )
    )
    return [
        value
        for name, _, values in tiers
        if name in selected_names
        for value in values
    ]


def strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def read_sources(
    files: list[Path],
    *,
    csv_text_column: list[str] | None,
    textgrid_tier: list[str],
    eaf_tier: list[str],
) -> list[SourceText]:
    sources: list[SourceText] = []
    for path in files:
        suffix = path.suffix.lower()
        if suffix == ".eaf":
            texts = read_eaf_texts(path, eaf_tier)
            source_type = "eaf"
        elif suffix == ".txt":
            texts = read_txt_texts(path)
            source_type = "txt"
        elif suffix == ".csv":
            if not csv_text_column:
                raise ValueError(
                    f"{path}: csv_text_column must be configured when scanning CSV files"
                )
            texts = read_csv_texts(path, csv_text_column)
            source_type = "csv"
        elif suffix == ".textgrid":
            texts = read_textgrid_texts(path, textgrid_tier)
            source_type = "textgrid"
        else:
            raise ValueError(
                f"{path}: unsupported transcription file type {path.suffix!r}; "
                "supported types are .txt, .csv, .eaf, and .TextGrid"
            )
        sources.append(SourceText(path=path, source_type=source_type, texts=texts))
    return sources


def add_example(
    examples: dict[Any, list[dict[str, Any]]],
    key: Any,
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
    if not chao_letters:
        raise ValueError("chao_letters must contain at least one primitive letter")
    if any(len(letter) != 1 for letter in chao_letters):
        raise ValueError("Each chao_letters entry must be one non-empty character")
    if len(chao_letters) != len(set(chao_letters)):
        raise ValueError("chao_letters contains duplicate primitive letters")
    return re.compile(f"[{''.join(re.escape(letter) for letter in chao_letters)}]+")


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
    if len(char) == 1 and unicodedata.category(char).startswith("M"):
        return f"`◌{char}`"
    if len(char) == 1 and unicodedata.name(char, None) is None:
        return f"`U+{ord(char):04X}`"
    if char == "`":
        return "`` ` ``"
    return f"`{char}`"


def is_punctuation(char: str) -> bool:
    return char in PUNCTUATION_TO_REPORT or unicodedata.category(char).startswith("P")


def _is_modifier_cluster(value: str) -> bool:
    return bool(value) and value[0] in IPA_MODIFIER_CHARS and all(
        char in IPA_MODIFIER_CHARS or unicodedata.category(char).startswith("M")
        for char in value
    )


def _is_attachment_boundary(value: str) -> bool:
    return not value or value.isspace() or all(is_punctuation(char) for char in value)


def ipa_modifier_attachments(text: str) -> list[tuple[str, str, str, int, int]]:
    """Infer modifier attachments from Unicode graphemes and local IPA context."""
    clusters = [
        (match.group(0), match.start(), match.end())
        for match in unicode_regex.finditer(r"\X", text)
    ]
    cluster_at: list[int] = [0] * len(text)
    for cluster_index, (_, start, end) in enumerate(clusters):
        for position in range(start, end):
            cluster_at[position] = cluster_index

    attachments: list[tuple[str, str, str, int, int]] = []
    for modifier_position, modifier in enumerate(text):
        if modifier not in IPA_MODIFIER_CHARS:
            continue
        current_index = cluster_at[modifier_position]
        _, modifier_start, modifier_end = clusters[current_index]
        base_index = current_index - 1
        unattached_start = modifier_start
        while base_index >= 0 and _is_modifier_cluster(clusters[base_index][0]):
            unattached_start = clusters[base_index][1]
            base_index -= 1
        if base_index < 0 or _is_attachment_boundary(clusters[base_index][0]):
            attachments.append(
                (
                    modifier,
                    UNATTACHED_MODIFIER,
                    text[unattached_start:modifier_end],
                    unattached_start,
                    modifier_end,
                )
            )
            continue

        base, base_start, _ = clusters[base_index]
        if (
            base_index > 0
            and any(tie_bar in clusters[base_index - 1][0] for tie_bar in IPA_TIE_BARS)
            and not _is_attachment_boundary(clusters[base_index - 1][0])
        ):
            tied_part, tied_start, _ = clusters[base_index - 1]
            base = tied_part + base
            base_start = tied_start
        attachments.append(
            (modifier, base, text[base_start:modifier_end], base_start, modifier_end)
        )
    return attachments


def _diacritic_base(sequence: str) -> str:
    """Return the written base while retaining structural IPA tie bars."""
    decomposed = unicodedata.normalize("NFD", sequence)
    base = "".join(
        char
        for char in decomposed
        if char in IPA_TIE_BARS or not unicodedata.category(char).startswith("M")
    )
    return unicodedata.normalize("NFC", base)


def diacritic_attachments(
    text: str,
    hits: list[tuple[int, int, str]],
) -> list[tuple[str, str, str, int, int]]:
    """Infer bases and source graphemes for configured or literal diacritic hits."""
    if not hits:
        return []

    clusters = [
        (match.group(0), match.start(), match.end())
        for match in unicode_regex.finditer(r"\X", text)
    ]
    cluster_at: list[int] = [0] * len(text)
    for cluster_index, (_, start, end) in enumerate(clusters):
        for position in range(start, end):
            cluster_at[position] = cluster_index

    attachments: list[tuple[str, str, str, int, int]] = []
    for hit_start, _, mark in hits:
        current_index = cluster_at[hit_start]
        current, sequence_start, sequence_end = clusters[current_index]
        previous_has_tie = (
            current_index > 0
            and any(tie_bar in clusters[current_index - 1][0] for tie_bar in IPA_TIE_BARS)
        )
        current_has_tie = any(tie_bar in current for tie_bar in IPA_TIE_BARS)

        if previous_has_tie and not _is_attachment_boundary(
            clusters[current_index - 1][0]
        ):
            sequence_start = clusters[current_index - 1][1]
        if (
            current_has_tie
            and current_index + 1 < len(clusters)
            and not _is_attachment_boundary(clusters[current_index + 1][0])
        ):
            sequence_end = clusters[current_index + 1][2]

        sequence = text[sequence_start:sequence_end]
        if mark in IPA_TIE_BARS:
            has_left_base = any(
                not unicodedata.category(char).startswith("M")
                for char in text[clusters[current_index][1]:hit_start]
            )
            has_right_base = (
                current_index + 1 < len(clusters)
                and not _is_attachment_boundary(clusters[current_index + 1][0])
            )
            base = (
                _diacritic_base(sequence)
                if has_left_base and has_right_base
                else UNATTACHED_MODIFIER
            )
        else:
            base = _diacritic_base(sequence)
            if _is_attachment_boundary(base):
                base = UNATTACHED_MODIFIER

        if base == UNATTACHED_MODIFIER:
            sequence = "".join(
                char
                for char in current
                if unicodedata.category(char).startswith("M")
            ) or mark

        attachments.append((mark, base, sequence, sequence_start, sequence_end))
    return attachments


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
    combining_diacritic_attachment_counts: Counter[tuple[str, str, str]] = Counter()
    configured_diacritic_attachment_counts: Counter[tuple[str, str, str]] = Counter()
    ipa_modifier_counts: Counter[str] = Counter()
    ipa_modifier_attachment_counts: Counter[tuple[str, str, str]] = Counter()
    reduplicated_char_counts: Counter[str] = Counter()
    reduplicated_digit_counts: Counter[str] = Counter()
    per_file_rows: list[dict[str, Any]] = []
    source_type_counts: Counter[str] = Counter()
    char_examples: dict[str, list[dict[str, Any]]] = {}
    marker_examples: dict[str, list[dict[str, Any]]] = {}
    modifier_attachment_examples: dict[
        tuple[str, str, str], list[dict[str, Any]]
    ] = {}
    combining_diacritic_attachment_examples: dict[
        tuple[str, str, str], list[dict[str, Any]]
    ] = {}
    configured_diacritic_attachment_examples: dict[
        tuple[str, str, str], list[dict[str, Any]]
    ] = {}

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
                if char in IPA_MODIFIER_CHARS:
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

            for modifier, base, sequence, start, end in ipa_modifier_attachments(text):
                attachment = (modifier, base, sequence)
                ipa_modifier_attachment_counts[attachment] += 1
                add_example(
                    modifier_attachment_examples,
                    attachment,
                    source=source,
                    text=text,
                    start=start,
                    end=end,
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

            configured_hits = diacritic_hits(text, diacritic_markers)
            for start, end, mark in configured_hits:
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

            for mark, base, sequence, start, end in diacritic_attachments(
                text, configured_hits
            ):
                attachment = (mark, base, sequence)
                configured_diacritic_attachment_counts[attachment] += 1
                add_example(
                    configured_diacritic_attachment_examples,
                    attachment,
                    source=source,
                    text=text,
                    start=start,
                    end=end,
                    context_chars=context_chars,
                    limit=example_limit,
                )

            combining_hits = [
                (index, index + 1, char)
                for index, char in enumerate(text)
                if unicodedata.category(char) == "Mn"
            ]
            for mark, base, sequence, start, end in diacritic_attachments(
                text, combining_hits
            ):
                attachment = (mark, base, sequence)
                combining_diacritic_attachment_counts[attachment] += 1
                add_example(
                    combining_diacritic_attachment_examples,
                    attachment,
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
        "combining_diacritic_attachment_counts": combining_diacritic_attachment_counts,
        "configured_diacritic_attachment_counts": configured_diacritic_attachment_counts,
        "ipa_modifier_counts": ipa_modifier_counts,
        "ipa_modifier_attachment_counts": ipa_modifier_attachment_counts,
        "reduplicated_char_counts": reduplicated_char_counts,
        "reduplicated_digit_counts": reduplicated_digit_counts,
        "per_file_rows": per_file_rows,
        "source_type_counts": source_type_counts,
        "char_examples": char_examples,
        "marker_examples": marker_examples,
        "modifier_attachment_examples": modifier_attachment_examples,
        "combining_diacritic_attachment_examples": combining_diacritic_attachment_examples,
        "configured_diacritic_attachment_examples": configured_diacritic_attachment_examples,
        "total_annotations": total_annotations,
        "total_nonempty_chars": total_nonempty_chars,
    }


def table_for_counter(counter: Counter[str], *, limit: int | None = None) -> list[str]:
    if not counter:
        return ["No matches found."]

    rows = ["| Character | Count | Unicode name |"]
    items = counter.most_common(limit)
    for char, count in items:
        rows.append(f"| {char_display(char)} | {count:,} | {char_name(char)} |")
    return rows


def examples_for_key(
    examples: dict[Any, list[dict[str, Any]]],
    key: Any,
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


def _attachment_value_display(value: str) -> str:
    if value == UNATTACHED_MODIFIER:
        return value
    return char_display(value) if len(value) == 1 else f"`{markdown_escape_cell(value)}`"


def attachment_table(
    counter: Counter[tuple[str, str, str]],
    examples: dict[tuple[str, str, str], list[dict[str, Any]]],
    *,
    data_roots: list[Path],
    first_column: str,
    empty_message: str,
) -> list[str]:
    if not counter:
        return [empty_message]
    rows = [f"| {first_column} | Modified grapheme | Sequence | Count | Examples |"]
    rows.append("| --- | --- | --- | ---: | --- |")
    for attachment, count in sorted(
        counter.items(), key=lambda item: (-item[1], *item[0])
    ):
        modifier, base, sequence = attachment
        rows.append(
            f"| {char_display(modifier)} | {_attachment_value_display(base)} | "
            f"{_attachment_value_display(sequence)} | {count:,} | "
            f"{examples_for_key(examples, attachment, data_roots=data_roots)} |"
        )
    return rows


def ipa_modifier_attachment_table(
    counter: Counter[tuple[str, str, str]],
    examples: dict[tuple[str, str, str], list[dict[str, Any]]],
    *,
    data_roots: list[Path],
) -> list[str]:
    return attachment_table(
        counter,
        examples,
        data_roots=data_roots,
        first_column="Modifier",
        empty_message="No IPA modifier attachments found.",
    )


def diacritic_attachment_table(
    counter: Counter[tuple[str, str, str]],
    examples: dict[tuple[str, str, str], list[dict[str, Any]]],
    *,
    data_roots: list[Path],
) -> list[str]:
    return attachment_table(
        counter,
        examples,
        data_roots=data_roots,
        first_column="Diacritic",
        empty_message="No diacritic attachments found.",
    )


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

    rows = ["| Value | Count | Examples |"]
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
    rows = ["| Tone number | Count |"]
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
    rows = ["| Tone number | Count | Examples |"]
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
    csv_text_column_value = config.get("csv_text_column")
    csv_text_column = (
        configured_selector(csv_text_column_value, "csv_text_column")
        if csv_text_column_value is not None else None
    )
    textgrid_tier = configured_selector(
        config.get("textgrid_tier", "all"), "textgrid_tier"
    )
    eaf_tier = configured_selector(config.get("eaf_tier", "all"), "eaf_tier")
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
    sources = read_sources(
        files,
        csv_text_column=csv_text_column,
        textgrid_tier=textgrid_tier,
        eaf_tier=eaf_tier,
    )
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
        "",
        "## Config",
        "",
        f"- Config file: `{rel(config_path.resolve(), config_dir.parent)}`",
        f"- Data roots: `{', '.join(str(root) for root in data_roots)}`",
        f"- Report path: `{out_path}`",
        f"- Example limit per item: {example_limit}",
        f"- Example context characters per side: {context_chars}",
        f"- CSV text column: `{display_selector(csv_text_column)}`",
        f"- TextGrid tier: `{display_selector(textgrid_tier)}`",
        f"- EAF tier: `{display_selector(eaf_tier)}`",
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
            f"- `.csv` files: {source_type_counts.get('csv', 0):,}",
            f"- `.eaf` files: {source_type_counts.get('eaf', 0):,}",
            f"- `.TextGrid` files: {source_type_counts.get('textgrid', 0):,}",
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

    lines.extend(["", "## Chao Tone Letter Contours", ""])
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

    lines.extend(["", "## IPA Modifier Attachments", ""])
    lines.extend(
        ipa_modifier_attachment_table(
            analysis["ipa_modifier_attachment_counts"],
            analysis["modifier_attachment_examples"],
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

    lines.extend(["", "## Configured Diacritic Attachments", ""])
    lines.extend(
        diacritic_attachment_table(
            analysis["configured_diacritic_attachment_counts"],
            analysis["configured_diacritic_attachment_examples"],
            data_roots=data_roots,
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

    lines.extend(["", "## Combining Diacritic Attachments", ""])
    lines.extend(
        diacritic_attachment_table(
            analysis["combining_diacritic_attachment_counts"],
            analysis["combining_diacritic_attachment_examples"],
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
