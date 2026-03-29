from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .models import AnalysisReport, AttributionBucket, DirectoryTreeNode


MAGIC = b"MRVWPK01"
VERSION = 1
HEADER_STRUCT = struct.Struct("<8sIIQQ")


@dataclass
class _ChunkRef:
    offset: int
    length: int


@dataclass
class _FileEntry:
    name: str
    path: str
    bucket_key: str
    stored_bytes: float = 0.0
    changed_bytes: int = 0
    changed_ranges: int = 0
    blocks: int = 0
    image_occurrences: int = 0
    image_file_numbers: list[int] = field(default_factory=list)


def _is_synthetic_bucket(key: str) -> bool:
    return (
        not key.startswith(".\\")
        or key.startswith(".\\$")
        or key.startswith("NTFS metadata:")
        or key.startswith("Partition bucket:")
        or key in {"Deleted or previous-owner bytes", "Unresolved NTFS bytes"}
    )


def _normalize_bucket_path(key: str) -> tuple[str | None, bool]:
    if _is_synthetic_bucket(key):
        return None, False

    normalized = key
    is_directory_bucket = False
    if normalized.endswith(":$I30"):
        normalized = normalized[: -len(":$I30")]
        is_directory_bucket = True
    elif ":" in normalized:
        normalized = normalized.split(":", 1)[0]

    if not normalized.startswith(".\\"):
        return None, False
    return normalized, is_directory_bucket


def _build_file_entries(buckets: list[AttributionBucket]) -> dict[str, list[_FileEntry]]:
    parent_to_entries: dict[str, dict[str, _FileEntry]] = {}
    for bucket in buckets:
        normalized, is_directory_bucket = _normalize_bucket_path(bucket.key)
        if normalized is None or is_directory_bucket:
            continue

        parent_path, _, filename = normalized.rpartition("\\")
        if not parent_path:
            parent_path = ".\\"
        entries = parent_to_entries.setdefault(parent_path, {})
        entry = entries.get(normalized)
        if entry is None:
            entry = _FileEntry(
                name=filename or normalized,
                path=normalized,
                bucket_key=bucket.key,
            )
            entries[normalized] = entry
        entry.stored_bytes += bucket.stored_bytes
        entry.changed_bytes += bucket.changed_bytes
        entry.changed_ranges += bucket.changed_ranges
        entry.blocks += bucket.blocks
        for file_number in bucket.image_file_numbers:
            if file_number not in entry.image_file_numbers:
                entry.image_file_numbers.append(file_number)
        entry.image_file_numbers.sort()
        entry.image_occurrences = len(entry.image_file_numbers)

    result: dict[str, list[_FileEntry]] = {}
    for parent_path, entries in parent_to_entries.items():
        result[parent_path] = sorted(
            entries.values(),
            key=lambda entry: (-entry.stored_bytes, -entry.image_occurrences, entry.path.lower()),
        )
    return result


def _percent(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return (numerator / denominator) * 100.0


def _node_summary(node: DirectoryTreeNode, total_stored_bytes: int, total_changed_bytes: int) -> dict:
    child_count = (
        len([child for child in node.children if child.kind == "directory"])
        if node.kind == "directory"
        else len(node.children)
    )
    return {
        "name": node.name,
        "path": node.path,
        "kind": node.kind,
        "stored_bytes": node.stored_bytes,
        "stored_percent": _percent(node.stored_bytes, total_stored_bytes),
        "changed_bytes": node.changed_bytes,
        "changed_percent": _percent(node.changed_bytes, total_changed_bytes),
        "changed_ranges": node.changed_ranges,
        "blocks": node.blocks,
        "image_occurrences": node.image_occurrences,
        "image_file_numbers": node.image_file_numbers,
        "child_count": child_count,
    }


def _file_entry_to_dict(entry: _FileEntry, total_stored_bytes: int, total_changed_bytes: int) -> dict:
    return {
        "name": entry.name,
        "path": entry.path,
        "kind": "file",
        "bucket_key": entry.bucket_key,
        "stored_bytes": entry.stored_bytes,
        "stored_percent": _percent(entry.stored_bytes, total_stored_bytes),
        "changed_bytes": entry.changed_bytes,
        "changed_percent": _percent(entry.changed_bytes, total_changed_bytes),
        "changed_ranges": entry.changed_ranges,
        "blocks": entry.blocks,
        "image_occurrences": entry.image_occurrences,
        "image_file_numbers": entry.image_file_numbers,
    }


def _special_bucket_to_dict(bucket: DirectoryTreeNode, total_stored_bytes: int, total_changed_bytes: int) -> dict:
    return {
        "name": bucket.name,
        "path": bucket.path,
        "kind": bucket.kind,
        "stored_bytes": bucket.stored_bytes,
        "stored_percent": _percent(bucket.stored_bytes, total_stored_bytes),
        "changed_bytes": bucket.changed_bytes,
        "changed_percent": _percent(bucket.changed_bytes, total_changed_bytes),
        "changed_ranges": bucket.changed_ranges,
        "blocks": bucket.blocks,
        "image_occurrences": bucket.image_occurrences,
        "image_file_numbers": bucket.image_file_numbers,
    }


def write_viewer_bundle(report: AnalysisReport, destination: Path) -> Path:
    file_entries = _build_file_entries(report.buckets)
    chunk_refs: dict[str, _ChunkRef] = {}
    root = report.directory_tree
    special_group = next((child for child in root.children if child.kind == "synthetic-group"), None)
    total_stored_bytes = report.total_stored_bytes
    total_changed_bytes = report.total_changed_bytes

    temp_path = destination.with_suffix(destination.suffix + ".tmp")
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    with temp_path.open("wb") as handle:
        handle.write(b"\x00" * HEADER_STRUCT.size)

        def write_chunk(payload: dict) -> _ChunkRef:
            encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            offset = handle.tell()
            handle.write(encoded)
            return _ChunkRef(offset=offset, length=len(encoded))

        def write_directory_chunk(node: DirectoryTreeNode) -> _ChunkRef:
            actual_children = [child for child in node.children if child.kind == "directory"]
            child_summaries = []
            for child in actual_children:
                child_ref = write_directory_chunk(child)
                chunk_refs[child.path] = child_ref
                child_summaries.append(
                    {
                        **_node_summary(child, total_stored_bytes, total_changed_bytes),
                        "chunk_offset": child_ref.offset,
                        "chunk_length": child_ref.length,
                    }
                )

            payload = {
                "node": _node_summary(node, total_stored_bytes, total_changed_bytes),
                "directories": child_summaries,
                "files": [
                    _file_entry_to_dict(entry, total_stored_bytes, total_changed_bytes)
                    for entry in file_entries.get(node.path, [])
                ],
            }
            return write_chunk(payload)

        root_ref = write_directory_chunk(root)
        chunk_refs[root.path] = root_ref
        special_ref = None
        if special_group is not None:
            special_payload = {
                "node": _node_summary(special_group, total_stored_bytes, total_changed_bytes),
                "buckets": [
                    _special_bucket_to_dict(child, total_stored_bytes, total_changed_bytes)
                    for child in special_group.children
                ],
            }
            special_ref = write_chunk(special_payload)

        manifest = {
            "format": "macrium-viewpack",
            "version": VERSION,
            "target_file": str(report.target_file),
            "target_file_number": report.target_file_number,
            "target_backup_type": report.target_backup_type,
            "parent_file_number": report.parent_file_number,
            "requested_image_count": report.requested_image_count,
            "analyzed_image_count": len(report.analyzed_images),
            "analyzed_images": [
                {
                    "file_path": str(image.file_path),
                    "file_number": image.file_number,
                    "backup_type": image.backup_type,
                    "parent_file_number": image.parent_file_number,
                    "total_stored_bytes": image.total_stored_bytes,
                    "total_changed_bytes": image.total_changed_bytes,
                    "changed_block_count": image.changed_block_count,
                    "bucket_count": image.bucket_count,
                }
                for image in report.analyzed_images
            ],
            "totals": {
                "stored_bytes": report.total_stored_bytes,
                "changed_bytes": report.total_changed_bytes,
                "bucket_count": len(report.buckets),
            },
            "notes": report.notes,
            "root": {
                "path": root.path,
                "chunk_offset": root_ref.offset,
                "chunk_length": root_ref.length,
            },
            "special": (
                {
                    "path": special_group.path,
                    "chunk_offset": special_ref.offset,
                    "chunk_length": special_ref.length,
                }
                if special_group is not None and special_ref is not None
                else None
            ),
        }
        manifest_bytes = json.dumps(manifest, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        manifest_offset = handle.tell()
        handle.write(manifest_bytes)
        handle.seek(0)
        handle.write(
            HEADER_STRUCT.pack(
                MAGIC,
                VERSION,
                HEADER_STRUCT.size,
                manifest_offset,
                len(manifest_bytes),
            )
        )

    os.replace(temp_path, destination)
    return destination
