"""Command line interface for macrium-analyzer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .analyzer import analyze_file
from .bootstrap import add_local_deps_to_path
from .progress import ProgressTracker
from .state_db import AggregateState
from .viewer_bundle import write_viewer_bundle


def _format_bytes(value: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(round(size))} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PiB"


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _default_output_base(target_file: str) -> Path:
    target_path = Path(target_file)
    return Path.cwd() / f"{target_path.stem}.analysis"


def _output_paths(output_base: Path) -> tuple[Path, Path]:
    base_text = str(output_base)
    return Path(base_text + ".json"), Path(base_text + ".txt")


def _default_viewer_output_path(output_base: Path) -> Path:
    return Path(str(output_base) + ".viewpack")


def _default_state_db_path(output_base: Path) -> Path:
    return Path(str(output_base) + ".state.sqlite3")


def _default_progress_log_path(progress_file: str | None) -> Path | None:
    if not progress_file:
        return None
    return Path(str(progress_file) + ".log")


def _depth_for_path(path: str) -> int:
    if path == ".\\":
        return 0
    return len([part for part in path[2:].split("\\") if part])


def _share(value: float, total: int) -> float:
    if total == 0:
        return 0.0
    return (value / total) * 100.0


def _collapse_directory_chain(state: AggregateState, node: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    parts = [str(node["name"])]
    current = node
    while True:
        actual_children = state.load_directory_children(str(current["path"]), limit=2)
        if len(actual_children) != 1:
            break
        current = actual_children[0]
        parts.append(str(current["name"]))
    return "\\".join(parts), current


def _render_condensed_tree(state: AggregateState, *, max_children: int, max_depth: int) -> list[str]:
    lines: list[str] = []

    def walk(path: str, depth: int) -> None:
        if depth >= max_depth:
            return
        children = state.load_directory_children(path, limit=max_children)
        for child in children:
            label, collapsed = _collapse_directory_chain(state, child)
            indent = "  " * depth
            line = (
                f"{indent}- {label}: stored={_format_bytes(float(collapsed['stored_bytes']))}, "
                f"logical={_format_bytes(int(collapsed['changed_bytes']))}, blocks={int(collapsed['blocks'])}"
            )
            if int(collapsed["image_occurrences"]) > 0:
                line += f", images={int(collapsed['image_occurrences'])}"
            lines.append(line)
            walk(str(collapsed["path"]), depth + 1)

    walk(".\\", 0)
    return lines


def _render_directory_rank(
    entries: list[dict[str, Any]],
    *,
    total_stored_bytes: int,
    total_changed_bytes: int,
    analyzed_image_count: int,
) -> list[str]:
    lines: list[str] = []
    for node in entries:
        stored_bytes = float(node["stored_bytes"])
        changed_bytes = int(node["changed_bytes"])
        line = (
            f"  {node['path']}: stored={_format_bytes(stored_bytes)} "
            f"({_share(stored_bytes, total_stored_bytes):.1f}%), "
            f"logical={_format_bytes(changed_bytes)} "
            f"({_share(changed_bytes, total_changed_bytes):.1f}%), "
            f"depth={_depth_for_path(str(node['path']))}, blocks={int(node['blocks'])}"
        )
        if analyzed_image_count > 1:
            line += f", images={int(node['image_occurrences'])}/{analyzed_image_count}"
        lines.append(line)
    return lines or ["  (no directory nodes found)"]


def _render_synthetic_section(state: AggregateState, *, analyzed_image_count: int) -> list[str]:
    entries = state.load_special_entries()
    if not entries:
        return ["  (no synthetic buckets)"]

    lines: list[str] = []
    for entry in entries:
        line = (
            f"  {entry['key']}: stored={_format_bytes(float(entry['stored_bytes']))}, "
            f"logical={_format_bytes(int(entry['changed_bytes']))}, blocks={int(entry['blocks'])}"
        )
        if analyzed_image_count > 1:
            line += f", images={int(entry['image_occurrences'])}/{analyzed_image_count}"
        lines.append(line)
    return lines


def _render_analyzed_images(state: AggregateState) -> list[str]:
    lines: list[str] = []
    for image in state.load_analyzed_images():
        line = (
            f"  file #{image.file_number} ({image.backup_type})"
            f": stored={_format_bytes(image.total_stored_bytes)}, "
            f"logical={_format_bytes(image.total_changed_bytes)}, "
            f"changed_blocks={image.changed_block_count}, "
            f"buckets={image.bucket_count}"
        )
        if image.parent_file_number is not None:
            line += f", parent=#{image.parent_file_number}"
        lines.append(line)
    return lines or ["  (no analyzed images)"]


def _render_text_report(state: AggregateState, *, top_count: int) -> str:
    summary = state.load_run_summary()
    analyzed_image_count = int(summary["analyzed_image_count"])
    total_stored_bytes = int(summary["total_stored_bytes"])
    total_changed_bytes = int(summary["total_changed_bytes"])

    lines: list[str] = []
    lines.append(f"Target: {summary['target_file']}")
    if analyzed_image_count == 1:
        lines.append(
            "Restore point: "
            f"{summary['target_backup_type']} file #{summary['target_file_number']} "
            f"(parent #{summary['parent_file_number']})"
        )
        lines.append(f"Stored bytes in analyzed file: {_format_bytes(total_stored_bytes)}")
        lines.append(f"Logical bytes covered by changed blocks: {_format_bytes(total_changed_bytes)}")
    else:
        lines.append(
            "Restore point window: "
            f"{analyzed_image_count} image(s) ending at "
            f"{summary['target_backup_type']} file #{summary['target_file_number']} "
            f"(requested {summary['requested_image_count']})"
        )
        lines.append(f"Aggregate stored bytes across analyzed images: {_format_bytes(total_stored_bytes)}")
        lines.append(f"Aggregate logical bytes across changed blocks: {_format_bytes(total_changed_bytes)}")
        lines.append("")
        lines.append("Analyzed restore points:")
        lines.extend(_render_analyzed_images(state))

    lines.append("")
    lines.append("Largest directories:")
    lines.extend(
        _render_directory_rank(
            state.load_top_directories(top_count),
            total_stored_bytes=total_stored_bytes,
            total_changed_bytes=total_changed_bytes,
            analyzed_image_count=analyzed_image_count,
        )
    )

    lines.append("")
    lines.append("Most specific impactful directories:")
    lines.extend(
        _render_directory_rank(
            state.load_top_leaf_directories(top_count),
            total_stored_bytes=total_stored_bytes,
            total_changed_bytes=total_changed_bytes,
            analyzed_image_count=analyzed_image_count,
        )
    )

    lines.append("")
    lines.append("Condensed directory tree:")
    tree_lines = _render_condensed_tree(state, max_children=3, max_depth=4)
    lines.extend(tree_lines if tree_lines else ["  (no directory tree nodes found)"])

    lines.append("")
    lines.append("Synthetic and unresolved buckets:")
    lines.extend(_render_synthetic_section(state, analyzed_image_count=analyzed_image_count))

    lines.append("")
    lines.append("Flat attribution buckets:")
    buckets = state.load_flat_buckets(limit=top_count)
    if not buckets:
        lines.append("  (no changed buckets found)")
    else:
        for bucket in buckets:
            changed_bytes = int(bucket["changed_bytes"])
            stored_bytes = float(bucket["stored_bytes"])
            line = (
                f"  {bucket['key']}: stored={_format_bytes(stored_bytes)} "
                f"({_share(stored_bytes, total_stored_bytes):.1f}%), "
                f"logical={_format_bytes(changed_bytes)} "
                f"({_share(changed_bytes, total_changed_bytes):.1f}%), "
                f"blocks={int(bucket['blocks'])}, ranges={int(bucket['changed_ranges'])}"
            )
            if analyzed_image_count > 1:
                line += f", images={int(bucket['image_occurrences'])}/{analyzed_image_count}"
            lines.append(line)

    notes = state.load_notes()
    if notes:
        lines.append("")
        lines.append("Notes:")
        for note in notes:
            lines.append(f"  - {note}")
    return "\n".join(lines) + "\n"


def _atomic_write_text(path: Path, content: str) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def _add_analyze_mrimgx(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subcommands.add_parser(
        "analyze-mrimgx",
        help="Attribute a .mrimgx restore point to changed on-disk files.",
    )
    parser.add_argument("--file", required=True, help="Path to the target .mrimgx file.")
    parser.add_argument("--top", type=int, default=20, help="How many attribution buckets to print in the text summary.")
    parser.add_argument(
        "--image-count",
        type=int,
        default=1,
        help="How many images to analyze ending at the target file. Parent images are resolved automatically.",
    )
    parser.add_argument(
        "--progress-file",
        help="Optional path to a JSON status file that is updated while analysis runs.",
    )
    parser.add_argument(
        "--progress-log",
        help="Optional append-only text log for progress updates. Defaults to <progress-file>.log when --progress-file is set.",
    )
    parser.add_argument(
        "--output-base",
        help="Base path for durable report files. The CLI always writes <base>.txt, <base>.state.sqlite3, and by default also writes <base>.json and <base>.viewpack. Defaults to <target-stem>.analysis in the working directory.",
    )
    parser.add_argument(
        "--state-db",
        help="Optional path for the canonical SQLite aggregate state. Defaults to <output-base>.state.sqlite3.",
    )
    parser.add_argument(
        "--viewer-output",
        help="Optional path for the viewer bundle output. Defaults to <output-base>.viewpack.",
    )
    parser.add_argument(
        "--no-json-output",
        action="store_true",
        help="Do not write the durable JSON report file.",
    )
    parser.add_argument(
        "--no-viewer-output",
        action="store_true",
        help="Do not write the durable viewer bundle file.",
    )
    parser.add_argument(
        "--with-parent-ownership",
        action="store_true",
        help="Build a second NTFS ownership map for the parent snapshot. This is more complete but uses much more memory.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the JSON report.",
    )
    parser.set_defaults(handler=_handle_analyze_mrimgx)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="macrium-analyzer")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.required = False
    _add_analyze_mrimgx(subcommands)
    return parser


def _handle_analyze_mrimgx(args: argparse.Namespace) -> int:
    log_path = Path(args.progress_log) if args.progress_log else _default_progress_log_path(args.progress_file)
    tracker = ProgressTracker(
        path=Path(args.progress_file) if args.progress_file else None,
        log_path=log_path,
    )

    top_count = max(args.top, 0)
    output_base = Path(args.output_base) if args.output_base else _default_output_base(args.file)
    json_path, text_path = _output_paths(output_base)
    state_db_path = Path(args.state_db) if args.state_db else _default_state_db_path(output_base)
    viewer_output_path = Path(args.viewer_output) if args.viewer_output else _default_viewer_output_path(output_base)

    try:
        analyze_file(
            Path(args.file),
            state_db_path=state_db_path,
            include_parent_ownership=bool(args.with_parent_ownership),
            progress=tracker,
            image_count=int(args.image_count),
        )
    except Exception as exc:
        tracker.fail(
            "Analysis failed.",
            error=str(exc),
            target_file=str(args.file),
            state_db_file=str(state_db_path),
        )
        raise

    state = AggregateState.open(state_db_path)
    try:
        tracker.update(
            "write-text",
            "Writing text report.",
            state_db_file=str(state_db_path),
        )
        text_report = _render_text_report(state, top_count=top_count)
        _atomic_write_text(text_path, text_report)

        if not args.no_json_output:
            tracker.update(
                "write-json",
                "Writing compact JSON report.",
                state_db_file=str(state_db_path),
            )
            state.write_json_report(json_path)

        if not args.no_viewer_output:
            tracker.update(
                "write-viewpack",
                "Writing viewer bundle.",
                state_db_file=str(state_db_path),
            )
            write_viewer_bundle(state, viewer_output_path)

        tracker.finish(
            phase="done",
            message="Analysis complete.",
            output_text_file=str(text_path),
            output_json_file=(str(json_path) if not args.no_json_output else None),
            output_viewer_file=(str(viewer_output_path) if not args.no_viewer_output else None),
            state_db_file=str(state_db_path),
        )

        elapsed_seconds = None
        if tracker.path and tracker.path.exists():
            try:
                payload = json.loads(tracker.path.read_text(encoding="utf-8"))
                elapsed_seconds = payload.get("elapsed_seconds")
            except Exception:
                elapsed_seconds = None

        if args.json:
            if args.no_json_output:
                state.write_json_report_to_handle(sys.stdout)
                print()
            else:
                sys.stdout.write(json_path.read_text(encoding="utf-8"))
                sys.stdout.write("\n")
        else:
            print(text_report, end="")
        print(f"Text report written to: {text_path}")
        if not args.no_json_output:
            print(f"JSON report written to: {json_path}")
        if not args.no_viewer_output:
            print(f"Viewer bundle written to: {viewer_output_path}")
        print(f"State database written to: {state_db_path}")
        if log_path is not None:
            print(f"Progress log written to: {log_path}")
        if isinstance(elapsed_seconds, (int, float)):
            print(f"Elapsed time: {_format_duration(float(elapsed_seconds))}")
    finally:
        state.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    add_local_deps_to_path()
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
