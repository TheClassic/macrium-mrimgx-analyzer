# Macrium Analyzer

Macrium Analyzer is a local Windows-first tool for attributing the size of a Macrium Reflect `.mrimgx` restore point to the files and directories that own the changed blocks stored in that image.

The current implementation supports both single-image and multi-image aggregate analysis for NTFS-backed restore points. It writes durable text, compact JSON, viewer-bundle, and SQLite state outputs, produces a directory-oriented view of contribution, and keeps filesystem metadata and unresolved ownership visible instead of silently dropping them.

## What It Does

- parse Macrium Reflect X `.mrimgx` metadata and block indexes directly
- resolve changed blocks for a selected restore point within its backup set
- attribute stored backup bytes to current NTFS owners at file and directory level
- aggregate stored-byte impact across a selected restore-point window
- keep a SQLite aggregate-state database as the canonical run artifact
- write both flat attribution buckets and hierarchical directory rollups
- emit a `.viewpack` bundle for the local static viewer
- emit a JSON progress file plus an append-only progress log while analysis runs

## Current Limitations

- encrypted backups are not supported
- full-backup attribution is not supported yet
- rename/move identity across a backup chain is still unresolved
- non-NTFS partitions are surfaced in partition-level buckets instead of exact file attribution

## Quickstart

1. Install the package in editable mode:

```powershell
python -m pip install -e .
```

2. Analyze a restore point:

```powershell
macrium-analyzer analyze-mrimgx `
  --file "D:\Backups\example.mrimgx" `
  --image-count 4 `
  --progress-file ".\run-status.json" `
  --output-base ".\example.analysis"
```

3. Review the durable outputs written to disk:

- `example.analysis.json`
- `example.analysis.txt`
- `example.analysis.viewpack`
- `example.analysis.state.sqlite3`
- `run-status.json`
- `run-status.json.log`

4. Tail the append-only progress log while the analysis runs:

```powershell
Get-Content ".\run-status.json.log" -Wait
```

## Output Shape

The compact JSON report includes:

- summary metadata
- analyzed restore points
- flat directory records
- flat file records
- synthetic buckets
- notes

The SQLite state database is the canonical aggregate state for a run. The text report, compact JSON report, and `.viewpack` bundle are all derived from it.

The text report focuses on:

- largest directories
- most specific impactful directories
- a condensed directory tree
- synthetic buckets for filesystem metadata and unresolved ownership

The viewer bundle is intended for the committed local static viewer in `viewer/`, which reads the bundle lazily instead of parsing the full JSON report in-browser.

## Format References

This repo does not vendor the upstream Macrium format-reference project.

- upstream reference project: `https://github.com/macriumsoftware/mrimg_file_layout`
- local notes about external references: `docs/REFERENCES.md`

## Roadmap

- resumable analysis built on top of the SQLite state database
- performance and parallel-processing design work for faster large-chain analysis
- richer viewer drill-down and search within the local static HTML viewer
- deeper handling for metadata-heavy NTFS buckets such as `$MFT`, `$LogFile`, and `$UsnJrnl`
