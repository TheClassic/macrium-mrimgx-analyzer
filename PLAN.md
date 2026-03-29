# Directory Tree Milestone With Future Follow-Ups

## Summary

Extend the current single-image analyzer so each run produces a hierarchical directory view of contribution to that image's size, not just flat file buckets. The persisted output should be viewer-friendly JSON plus a human-readable text report that emphasizes the deepest high-impact directories. Two future milestones should be tracked explicitly: a static local drill-down viewer backed by an indexed bundle, and multi-image chain analysis.

## Current Milestone

- Keep scope to one target `.mrimgx` at a time.
- Add a hierarchical directory rollup for attributed paths.
- Persist both:
  - flat bucket data for validation/debugging
  - hierarchical directory-tree data for future drill-down experiences
- Make the text report directory-centric:
  - rank impactful directories by stored bytes
  - show a condensed tree that surfaces deep hot paths
  - keep the tree directory-only for now
- Keep NTFS metadata and unresolved bytes in explicit synthetic nodes rather than mixing them with normal folders.
- Continue writing durable `.json` and `.txt` outputs for every run.

## Future Milestone: Static Local Viewer Bundle

- Keep the current full `.analysis.json` as an audit/debug artifact rather than the primary interactive format.
- Add a viewer-oriented single-file indexed bundle such as `.viewpack` that is written during analysis.
- Add a committed static local HTML viewer that opens the bundle with a file picker and reads subtrees on demand.
- Optimize the first interactive experience for drill-down, not treemap graphics.
- Start browsing from the root and expand manually.
- Include both directories and file leaves in the viewer tree.
- Keep synthetic buckets and NTFS metadata in a separate viewer section instead of mixing them into the normal directory/file tree.
- Prefer a single indexed bundle over a shard directory because it works cleanly with a static local viewer.
- For now, plan to emit both full JSON and viewer bundle by default, with separate disable flags later.
- Rationale:
  - current full JSON grows too large for smooth browser-first drill-down at larger sizes
  - static local viewer plus file picker is still desired
  - generating viewer data during analysis avoids reparsing giant JSON files in a later export step

## Future Milestone: Multi-Image Analysis

- Add a mode that analyzes every restore point from the chosen target back to the base/full image.
- Aggregate recurring contributors over time so the same directory can be shown as repeatedly contributing to backup growth.
- Report at least:
  - number of images where a directory appears
  - total stored-byte impact across the analyzed chain
  - first and last image where it contributed
  - per-image contribution timeline
- Leave file identity across rename/move unresolved for now; that remains an explicit open design question for this milestone.

## Test Plan

- Verify single-image `.txt` and `.json` outputs are written and contain both flat and hierarchical data.
- Verify directory totals reconcile with descendant totals and with flat bucket totals.
- Verify deep impactful paths surface prominently in the text report.
- Verify NTFS metadata, unresolved bytes, and non-NTFS partition buckets remain visible and well-grouped.
- For the future viewer milestone, plan acceptance around dynamic subtree loading from the indexed bundle rather than full JSON parsing in the browser.
- For the future multi-image milestone, plan acceptance around recurring-contributor summaries across a restore-point chain.

## Assumptions and Defaults

- This milestone remains single-image only.
- The directory tree is directory-only; no file leaves yet.
- The full JSON report remains the source of truth and audit/debug artifact for future viewer work.
- The future interactive viewer should consume a dedicated indexed bundle rather than the giant JSON directly.
- Rename/move tracking is intentionally deferred to the multi-image milestone.
