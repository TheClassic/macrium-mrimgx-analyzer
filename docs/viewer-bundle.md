# Viewer Bundle Design

## Summary

The compact `.analysis.json` remains an audit/debug output, but it is not intended to be the primary interactive format for very large reports. The interactive path is a viewer-oriented single-file indexed bundle, `.viewpack`, plus the committed static local HTML viewer.

## Why Not Use Giant JSON Directly

- Even compact analysis JSON files are still the wrong primary format for browser drill-down at larger sizes.
- A static local viewer with a file picker is still the desired user experience.
- A single indexed bundle works better than a shard directory for `file://` usage because the viewer can open one file and read subtrees from known offsets.
- The analyzer's canonical state is SQLite, not JSON. The viewer bundle is derived from SQLite after rollups are finalized.

## Planned Output Set

Analysis runs write:

- text report
- full JSON report
- SQLite aggregate-state database
- viewer bundle

JSON and viewer bundle are both enabled by default, with separate disable flags. The SQLite state database is always written unless a future option explicitly changes that policy.

## Planned Viewer Behavior

- Static local HTML viewer with no server requirement.
- Open a viewer bundle using a file picker.
- Start at the root and expand manually.
- Show directories and file leaves.
- Load subtree data dynamically from the bundle instead of parsing the full report JSON.
- Keep synthetic buckets and NTFS metadata in a separate section.
