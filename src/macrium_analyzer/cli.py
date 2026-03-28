"""Command line interface for macrium-analyzer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .analyzer import analyze_file, report_to_json
from .bootstrap import add_local_deps_to_path
from .models import AnalysisReport, DirectoryTreeNode
from .progress import ProgressTracker


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


def _default_progress_log_path(progress_file: str | None) -> Path | None:
    if not progress_file:
        return None
    return Path(str(progress_file) + ".log")


def _collect_directory_nodes(root: DirectoryTreeNode) -> list[tuple[int, DirectoryTreeNode]]:
    nodes: list[tuple[int, DirectoryTreeNode]] = []

    def walk(node: DirectoryTreeNode, depth: int) -> None:
        for child in node.children:
            if child.kind == "directory":
                nodes.append((depth + 1, child))
                walk(child, depth + 1)

    walk(root, 0)
    return nodes


def _collect_specific_directories(root: DirectoryTreeNode) -> list[tuple[int, DirectoryTreeNode]]:
    leaves: list[tuple[int, DirectoryTreeNode]] = []

    def walk(node: DirectoryTreeNode, depth: int) -> None:
        actual_children = [child for child in node.children if child.kind == "directory"]
        if node.kind == "directory" and depth > 0 and not actual_children:
            leaves.append((depth, node))
            return
        for child in actual_children:
            walk(child, depth + 1)

    walk(root, 0)
    return leaves


def _collapse_directory_chain(node: DirectoryTreeNode) -> tuple[str, DirectoryTreeNode]:
    parts = [node.name]
    current = node
    while True:
        actual_children = [child for child in current.children if child.kind == "directory"]
        synthetic_children = [child for child in current.children if child.kind != "directory"]
        if len(actual_children) != 1 or synthetic_children:
            break
        current = actual_children[0]
        parts.append(current.name)
    return "\\".join(parts), current


def _render_condensed_tree(root: DirectoryTreeNode, *, max_children: int, max_depth: int) -> list[str]:
    lines: list[str] = []

    def walk(node: DirectoryTreeNode, depth: int) -> None:
        if depth >= max_depth:
            return
        actual_children = [child for child in node.children if child.kind == "directory"]
        for child in actual_children[:max_children]:
            label, collapsed = _collapse_directory_chain(child)
            indent = "  " * depth
            line = (
                f"{indent}- {label}: stored={_format_bytes(collapsed.stored_bytes)}, "
                f"logical={_format_bytes(collapsed.changed_bytes)}, blocks={collapsed.blocks}"
            )
            if collapsed.image_occurrences > 0:
                line += f", images={collapsed.image_occurrences}"
            lines.append(line)
            walk(collapsed, depth + 1)

    walk(root, 0)
    return lines


def _render_directory_rank(
    entries: list[tuple[int, DirectoryTreeNode]],
    *,
    total_stored_bytes: int,
    total_changed_bytes: int,
    top_count: int,
    analyzed_image_count: int,
) -> list[str]:
    lines: list[str] = []
    for depth, node in entries[:top_count]:
        stored_share = 0.0 if total_stored_bytes == 0 else (node.stored_bytes / total_stored_bytes) * 100.0
        changed_share = 0.0 if total_changed_bytes == 0 else (node.changed_bytes / total_changed_bytes) * 100.0
        line = (
            f"  {node.path}: stored={_format_bytes(node.stored_bytes)} "
            f"({stored_share:.1f}%), logical={_format_bytes(node.changed_bytes)} "
            f"({changed_share:.1f}%), depth={depth}, blocks={node.blocks}"
        )
        if analyzed_image_count > 1:
            line += f", images={node.image_occurrences}/{analyzed_image_count}"
        lines.append(line)
    if not lines:
        lines.append("  (no directory nodes found)")
    return lines


def _render_synthetic_section(root: DirectoryTreeNode, *, analyzed_image_count: int) -> list[str]:
    special = next((child for child in root.children if child.kind == "synthetic-group"), None)
    if special is None:
        return ["  (no synthetic buckets)"]
    lines: list[str] = []
    for child in special.children:
        line = (
            f"  {child.name}: stored={_format_bytes(child.stored_bytes)}, "
            f"logical={_format_bytes(child.changed_bytes)}, blocks={child.blocks}"
        )
        if analyzed_image_count > 1:
            line += f", images={child.image_occurrences}/{analyzed_image_count}"
        lines.append(line)
    return lines or ["  (no synthetic buckets)"]


def _render_analyzed_images(report: AnalysisReport) -> list[str]:
    lines: list[str] = []
    for image in report.analyzed_images:
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


def _render_text_report(report: AnalysisReport, *, top_count: int) -> str:
    lines: list[str] = []
    root = report.directory_tree
    analyzed_image_count = len(report.analyzed_images)
    largest_directories = sorted(
        _collect_directory_nodes(root),
        key=lambda item: (-item[1].stored_bytes, -item[0], item[1].path.lower()),
    )
    specific_directories = sorted(
        _collect_specific_directories(root),
        key=lambda item: (-item[1].stored_bytes, -item[0], item[1].path.lower()),
    )

    lines.append(f"Target: {report.target_file}")
    if analyzed_image_count == 1:
        lines.append(
            "Restore point: "
            f"{report.target_backup_type} file #{report.target_file_number} "
            f"(parent #{report.parent_file_number})"
        )
        lines.append(f"Stored bytes in analyzed file: {_format_bytes(report.total_stored_bytes)}")
        lines.append(f"Logical bytes covered by changed blocks: {_format_bytes(report.total_changed_bytes)}")
    else:
        lines.append(
            "Restore point window: "
            f"{analyzed_image_count} image(s) ending at "
            f"{report.target_backup_type} file #{report.target_file_number} "
            f"(requested {report.requested_image_count})"
        )
        lines.append(f"Aggregate stored bytes across analyzed images: {_format_bytes(report.total_stored_bytes)}")
        lines.append(f"Aggregate logical bytes across changed blocks: {_format_bytes(report.total_changed_bytes)}")
        lines.append("")
        lines.append("Analyzed restore points:")
        lines.extend(_render_analyzed_images(report))
    lines.append("")
    lines.append("Largest directories:")
    lines.extend(
        _render_directory_rank(
            largest_directories,
            total_stored_bytes=report.total_stored_bytes,
            total_changed_bytes=report.total_changed_bytes,
            top_count=top_count,
            analyzed_image_count=analyzed_image_count,
        )
    )
    lines.append("")
    lines.append("Most specific impactful directories:")
    lines.extend(
        _render_directory_rank(
            specific_directories,
            total_stored_bytes=report.total_stored_bytes,
            total_changed_bytes=report.total_changed_bytes,
            top_count=top_count,
            analyzed_image_count=analyzed_image_count,
        )
    )
    lines.append("")
    lines.append("Condensed directory tree:")
    tree_lines = _render_condensed_tree(root, max_children=3, max_depth=4)
    lines.extend(tree_lines if tree_lines else ["  (no directory tree nodes found)"])
    lines.append("")
    lines.append("Synthetic and unresolved buckets:")
    lines.extend(_render_synthetic_section(root, analyzed_image_count=analyzed_image_count))
    lines.append("")
    lines.append("Flat attribution buckets:")
    if not report.buckets:
        lines.append("  (no changed buckets found)")
    else:
        for bucket in report.buckets[:top_count]:
            changed_share = 0.0 if report.total_changed_bytes == 0 else (bucket.changed_bytes / report.total_changed_bytes) * 100.0
            stored_share = 0.0 if report.total_stored_bytes == 0 else (bucket.stored_bytes / report.total_stored_bytes) * 100.0
            line = (
                f"  {bucket.key}: stored={_format_bytes(bucket.stored_bytes)} "
                f"({stored_share:.1f}%), logical={_format_bytes(bucket.changed_bytes)} "
                f"({changed_share:.1f}%), blocks={bucket.blocks}, ranges={bucket.changed_ranges}"
            )
            if analyzed_image_count > 1:
                line += f", images={bucket.image_occurrences}/{analyzed_image_count}"
            lines.append(line)
    if report.notes:
        lines.append("")
        lines.append("Notes:")
        for note in report.notes:
            lines.append(f"  - {note}")
    return "\n".join(lines) + "\n"


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
        help="Base path for durable report files. The CLI writes both <base>.json and <base>.txt. Defaults to <target-stem>.analysis in the working directory.",
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
    try:
        report = analyze_file(
            Path(args.file),
            include_parent_ownership=bool(args.with_parent_ownership),
            progress=tracker,
            image_count=int(args.image_count),
        )
    except Exception as exc:
        tracker.fail("Analysis failed.", error=str(exc), target_file=str(args.file))
        raise

    top_count = max(args.top, 0)
    output_base = Path(args.output_base) if args.output_base else _default_output_base(args.file)
    json_path, text_path = _output_paths(output_base)
    json_text = report_to_json(report)
    text_report = _render_text_report(report, top_count=top_count)
    json_path.write_text(json_text + "\n", encoding="utf-8")
    text_path.write_text(text_report, encoding="utf-8")
    tracker.finish(
        phase="done",
        message="Analysis complete.",
        output_json_file=str(json_path),
        output_text_file=str(text_path),
    )

    elapsed_seconds = None
    if tracker.path and tracker.path.exists():
        try:
            payload = json.loads(tracker.path.read_text(encoding="utf-8"))
            elapsed_seconds = payload.get("elapsed_seconds")
        except Exception:
            elapsed_seconds = None

    if args.json:
        print(json_text)
    else:
        print(text_report, end="")
    print(f"JSON report written to: {json_path}")
    print(f"Text report written to: {text_path}")
    if log_path is not None:
        print(f"Progress log written to: {log_path}")
    if isinstance(elapsed_seconds, (int, float)):
        print(f"Elapsed time: {_format_duration(float(elapsed_seconds))}")
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
