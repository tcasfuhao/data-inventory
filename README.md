# Data Inventory Model

This folder contains the standalone transcription inventory step for ASR model folders.

The script reads the folder as an input dataset/model project, but writes reports here.

The inventory is based on raw transcription files such as `.txt`, `.csv`, `.eaf`, and Praat long text-format `.TextGrid` files.

## Conda environment

Technically, no environment is required to run this project, however, later stages use a shared `tcas_asr_python3.10` Conda environment, so for standardisation principles, we recomment the environment being made with Python 3.10 and activated:

```bash
conda create --name tcas_asr_python3.10 python=3.10
conda activate tcas_asr_python3.10
python -m pip install -r requirements.txt
```

## Config

Edit `config/inventory.yaml` to choose:

- `data_root`: where the raw transcription data is stored
- `language`: the language/report prefix
- `report_name`: the output filename, for example `{language}_data_inventory.md`
- `transcription_globs`: which files to scan
- `csv_text_column`: one or more CSV columns containing transcriptions, or `all` (not the default)
- `textgrid_tier`: one or more TextGrid tier names, or `all` (the default)
- `eaf_tier`: one or more EAF `TIER_ID` values, or `all` (the default)
- `chao_letters`: the unique, single-character primitives used to discover Chao tone contours

Column and tier names are matched case-insensitively. For example:

```yaml
transcription_globs:
  - "*.csv"
csv_text_column: FORM
textgrid_tier: [segments, notes]
eaf_tier: all
```

Multiple names can use an inline list such as `[segments, notes]` or a block list:

```yaml
csv_text_column:
  - FORM
  - PHONETIC
```

For CSV files, `all` reads every non-empty data cell as a separate transcription entry; CSV header names are not counted. For TextGrid and EAF files, `all` reads every tier. `all` must be used by itself, not alongside explicit names.

The transcription globs choose which files to scan. Each matched file's extension then selects the appropriate CSV, TextGrid, EAF, or plain-text reader, so mixed file formats can be scanned in one run while retaining their format-specific selectors.

If a selected column or tier is missing, the script stops and reports both the requested name and the names available in that file.

### Chao tone contours

Configure the five primitive Chao tone letters rather than enumerating contours:

```yaml
chao_letters: ["˥", "˦", "˧", "˨", "˩"]
```

The inventory treats every maximal adjacent run of these characters as one observed contour. This includes single levels, transitions such as `˥˦`, repeated levels such as `˥˥`, and contours of any length such as `˧˩˦`. Only contours present in the scanned transcription data appear in the report.

## Run

```bash
python scripts/build_inventory.py --config config/inventory.yaml
```
