# Viewer Bundle Design

## Summary

The interactive path is a viewer-oriented single-file indexed bundle, `.viewpack`, plus the committed static local HTML viewer. The analyzer no longer emits a large durable JSON report.

## Why Not Use Giant JSON Directly

- A static local viewer with a file picker is still the desired user experience.
- A single indexed bundle works better than a shard directory for `file://` usage because the viewer can open one file and read subtrees from known offsets.
- The analyzer's canonical state is in-memory SQLite during the run. The viewer bundle is derived from SQLite after rollups are finalized.

## Planned Output Set

Analysis runs write:

- text report
- viewer bundle

The viewer bundle is enabled by default and the text report remains available as a human-readable companion output.

## Planned Viewer Behavior

- Static local HTML viewer with no server requirement.
- Open a viewer bundle using a file picker.
- Start at the root and expand manually.
- Show directories and file leaves.
- Load subtree data dynamically from the bundle instead of parsing the full report JSON.
- Keep synthetic buckets and NTFS metadata in a separate section.
