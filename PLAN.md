# SQLite Aggregate-State Milestone

## Summary

Macrium Analyzer now treats a SQLite database as the canonical aggregate state for each run. The analyzer writes block-attribution results into SQLite during analysis, derives directory rollups from that state after attribution completes, and then emits the durable text report, compact JSON report, and `.viewpack` bundle from SQLite.

This design replaces the older "build one giant in-memory report and serialize it afterward" approach. The goal is lower peak RAM during large multi-image runs while preserving the current viewer and reporting workflow.

## Current Architecture

- Canonical run state lives in `<output-base>.state.sqlite3`.
- Changed-block attribution writes aggregate file, directory-bucket, and synthetic-bucket metrics into SQLite in bounded batches.
- Image occurrence counts are normalized into relational tables instead of being carried around as large in-memory sets.
- Directory rollups are derived in a post-analysis pass from canonical leaf rows rather than updated for every block during attribution.
- Durable outputs are generated sequentially from SQLite:
  - text report
  - compact JSON report
  - `.viewpack` bundle

## Output Design

- The text report stays directory-centric and emphasizes:
  - largest directories
  - most specific impactful directories
  - a condensed tree
  - synthetic and unresolved buckets
- The JSON report is now a compact flat audit/debug artifact rather than a giant nested tree dump.
- The `.viewpack` bundle remains the browser-facing drill-down format.
- The committed static viewer should continue to consume `.viewpack`, not raw SQLite and not the full JSON report.

## Future Work

### Resumable Analysis

- Build on the SQLite state database to support resuming interrupted runs.
- Preserve enough per-run metadata to continue a chain without recomputing completed images.
- Decide later whether resume should be automatic or opt-in.

### Viewer Improvements

- Improve the static local HTML viewer for deeper drill-down and search.
- Keep the viewer rooted in `.viewpack` so it remains browser-friendly under `file://`.
- Continue loading subtrees lazily rather than parsing a giant report up front.

### Multi-Image Identity Tracking

- Extend recurring-contributor analysis to reason about rename and move behavior across a chain.
- Keep the rename/move identity question explicitly open until we decide how strongly to couple history to path vs NTFS identity.

## Validation

- Verify single-image and multi-image runs both produce `.txt`, `.json`, `.viewpack`, and `.state.sqlite3`.
- Verify directory totals and top contributors still reconcile with flat attribution buckets.
- Verify the local viewer still opens `.viewpack` bundles and expands child directories lazily.
- Compare peak RAM during multi-image runs against the prior in-memory-report design.
