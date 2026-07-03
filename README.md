# Data Inventory Model

This folder contains the standalone transcription inventory step for ASR model
folders.

It is intentionally separate from `../yonghe-qiang/`: the script reads that
folder as an input dataset/model project, but writes reports here.

The inventory is based on raw transcription files such as `.txt` and `.eaf`.
It does not inspect processed splits, CER-filtered data, model outputs, or
generated manifests.

## Config

Edit `config/inventory.yaml` to choose:

- `data_root`: where the raw transcription data is stored
- `language`: the language/report prefix
- `report_name`: the output filename, for example `{language}_data_inventory.md`
- `transcription_globs`: which files to scan

## Run

From `asr-models/data-inventory-model/`:

```bash
python scripts/build_inventory.py --config config/inventory.yaml
```

The report includes:

- files scanned
- non-empty transcription entry counts
- complete character inventory
- Chao-style tone number sequences such as `33`, `53`, `55`
- uppercase letters
- quotation marks and apostrophes
- digits, punctuation, IPA modifier characters, and combining diacritics

