# Viewer Bundle Milestone

## Summary

The current full `.analysis.json` remains the audit/debug output, but it is not intended to be the primary interactive format for very large reports. A future viewer milestone should add a viewer-oriented single-file indexed bundle, such as `.viewpack`, plus a committed static local HTML viewer.

## Why Not Use Giant JSON Directly

- Large analysis JSON files are too expensive to parse in-browser for smooth drill-down at larger sizes.
- A static local viewer with a file picker is still the desired user experience.
- A single indexed bundle works better than a shard directory for `file://` usage because the viewer can open one file and read subtrees from known offsets.

## Planned Output Set

Future analysis runs should be able to write:

- text report
- full JSON report
- viewer bundle

For the first viewer milestone, both JSON and viewer bundle are expected to be enabled by default, with separate disable flags.

## Planned Viewer Behavior

- Static local HTML viewer with no server requirement.
- Open a viewer bundle using a file picker.
- Start at the root and expand manually.
- Show directories and file leaves.
- Load subtree data dynamically from the bundle instead of parsing the full report JSON.
- Keep synthetic buckets and NTFS metadata in a separate section.
