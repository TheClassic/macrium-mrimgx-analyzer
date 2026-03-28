# Directory Tree Milestone With Future Follow-Ups

## Summary

Extend the current single-image analyzer so each run produces a hierarchical directory view of contribution to that image's size, not just flat file buckets. The persisted output should be viewer-friendly JSON plus a human-readable text report that emphasizes the deepest high-impact directories. Two future milestones should be tracked explicitly: a dynamic expandable viewer, and multi-image chain analysis.

## Current Milestone

- Keep scope to one target `.mrimgx` at a time.
- Add a hierarchical directory rollup for attributed paths.
- Persist both:
  - flat bucket data for validation/debugging
  - hierarchical directory-tree data for future viewers
- Make the text report directory-centric:
  - rank impactful directories by stored bytes
  - show a condensed tree that surfaces deep hot paths
  - keep the tree directory-only for now
- Keep NTFS metadata and unresolved bytes in explicit synthetic nodes rather than mixing them with normal folders.
- Continue writing durable `.json` and `.txt` outputs for every run.

## Future Milestone: Dynamic Viewer

- Build a viewer that reads the persisted hierarchical JSON and supports expanding/collapsing directory nodes.
- Optimize the first interactive experience for drill-down, not treemap graphics.
- Show stored bytes, logical bytes, percent of image, and block counts per directory node.
- Support starting from the most impactful deep directories instead of forcing a top-down root-first browse.
- Reuse the analyzer's persisted JSON shape rather than inventing a second report format.

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
- For future milestones, plan acceptance around expandable navigation and recurring-contributor summaries across a restore-point chain.

## Assumptions and Defaults

- This milestone remains single-image only.
- The directory tree is directory-only; no file leaves yet.
- The JSON report is the source of truth for future viewer work.
- Rename/move tracking is intentionally deferred to the multi-image milestone.
