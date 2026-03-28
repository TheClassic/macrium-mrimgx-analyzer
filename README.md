# Macrium Analyzer

Macrium Analyzer is a local Windows-first tool for attributing the size of a Macrium Reflect `.mrimgx` restore point to the files and directories that own the changed blocks stored in that image.

The current implementation focuses on single-image analysis for NTFS-backed restore points. It writes durable text and JSON reports, produces a directory-oriented view of contribution, and keeps filesystem metadata and unresolved ownership visible instead of silently dropping them.

## What It Does

- parse Macrium Reflect X `.mrimgx` metadata and block indexes directly
- resolve changed blocks for a selected restore point within its backup set
- attribute stored backup bytes to current NTFS owners at file and directory level
- write both flat attribution buckets and hierarchical directory trees
- emit a JSON progress file plus an append-only progress log while analysis runs

## Current Limitations

- encrypted backups are not supported
- full-backup attribution is not supported yet
- exact multi-image recurring-contributor analysis is planned, but not implemented yet
- the directory tree is directory-only for now; file leaves remain in the flat bucket view
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
  --progress-file ".\run-status.json" `
  --output-base ".\example.analysis"
```

3. Review the durable outputs written to disk:

- `example.analysis.json`
- `example.analysis.txt`
- `run-status.json`
- `run-status.json.log`

4. Tail the append-only progress log while the analysis runs:

```powershell
Get-Content ".\run-status.json.log" -Wait
```

## Output Shape

The JSON report includes:

- top-level restore-point metadata
- flat attribution buckets for validation and debugging
- a hierarchical directory tree intended to support future interactive viewers

The text report focuses on:

- largest directories
- most specific impactful directories
- a condensed directory tree
- synthetic buckets for filesystem metadata and unresolved ownership

## Format References

This repo does not vendor the upstream Macrium format-reference project.

- upstream reference project: `https://github.com/macriumsoftware/mrimg_file_layout`
- local notes about external references: `docs/REFERENCES.md`

## Roadmap

- dynamic expandable viewer over the persisted directory-tree JSON
- multi-image analysis to identify recurring contributors across a backup chain
- deeper handling for metadata-heavy NTFS buckets such as `$MFT`, `$LogFile`, and `$UsnJrnl`
