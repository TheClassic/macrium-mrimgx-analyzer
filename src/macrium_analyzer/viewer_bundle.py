from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state_db import AggregateState


MAGIC = b"MRVWPK01"
VERSION = 1
HEADER_STRUCT = struct.Struct("<8sIIQQ")


@dataclass
class _ChunkRef:
    offset: int
    length: int


def _percent(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return (numerator / denominator) * 100.0


def _directory_summary(node: dict[str, Any], total_stored_bytes: int, total_changed_bytes: int) -> dict[str, Any]:
    return {
        "name": node["name"],
        "path": node["path"],
        "kind": "directory",
        "stored_bytes": node["stored_bytes"],
        "stored_percent": _percent(float(node["stored_bytes"]), total_stored_bytes),
        "changed_bytes": node["changed_bytes"],
        "changed_percent": _percent(float(node["changed_bytes"]), total_changed_bytes),
        "changed_ranges": node["changed_ranges"],
        "blocks": node["blocks"],
        "image_occurrences": node["image_occurrences"],
        "image_file_numbers": node["image_file_numbers"],
        "child_count": node["child_count"],
    }


def _file_summary(entry: dict[str, Any], total_stored_bytes: int, total_changed_bytes: int) -> dict[str, Any]:
    return {
        "name": entry["name"],
        "path": entry["path"],
        "kind": "file",
        "bucket_key": entry["bucket_key"],
        "stored_bytes": entry["stored_bytes"],
        "stored_percent": _percent(float(entry["stored_bytes"]), total_stored_bytes),
        "changed_bytes": entry["changed_bytes"],
        "changed_percent": _percent(float(entry["changed_bytes"]), total_changed_bytes),
        "changed_ranges": entry["changed_ranges"],
        "blocks": entry["blocks"],
        "image_occurrences": entry["image_occurrences"],
        "image_file_numbers": entry["image_file_numbers"],
    }


def _special_summary(entry: dict[str, Any], total_stored_bytes: int, total_changed_bytes: int) -> dict[str, Any]:
    return {
        "name": entry["name"],
        "path": f"[special]\\{entry['key']}",
        "kind": "synthetic-bucket",
        "stored_bytes": entry["stored_bytes"],
        "stored_percent": _percent(float(entry["stored_bytes"]), total_stored_bytes),
        "changed_bytes": entry["changed_bytes"],
        "changed_percent": _percent(float(entry["changed_bytes"]), total_changed_bytes),
        "changed_ranges": entry["changed_ranges"],
        "blocks": entry["blocks"],
        "image_occurrences": entry["image_occurrences"],
        "image_file_numbers": entry["image_file_numbers"],
    }


def _special_group_summary(entries: list[dict[str, Any]], total_stored_bytes: int, total_changed_bytes: int) -> dict[str, Any]:
    image_numbers = sorted({number for entry in entries for number in entry["image_file_numbers"]})
    stored_bytes = sum(float(entry["stored_bytes"]) for entry in entries)
    changed_bytes = sum(int(entry["changed_bytes"]) for entry in entries)
    changed_ranges = sum(int(entry["changed_ranges"]) for entry in entries)
    blocks = sum(int(entry["blocks"]) for entry in entries)
    return {
        "name": "[special]",
        "path": "[special]",
        "kind": "synthetic-group",
        "stored_bytes": stored_bytes,
        "stored_percent": _percent(stored_bytes, total_stored_bytes),
        "changed_bytes": changed_bytes,
        "changed_percent": _percent(changed_bytes, total_changed_bytes),
        "changed_ranges": changed_ranges,
        "blocks": blocks,
        "image_occurrences": len(image_numbers),
        "image_file_numbers": image_numbers,
        "child_count": len(entries),
    }


def write_viewer_bundle(state: AggregateState, destination: Path) -> Path:
    summary = state.load_run_summary()
    analyzed_images = state.load_analyzed_images()
    notes = state.load_notes()
    root = state.load_directory_node(".\\")
    if root is None:
        raise RuntimeError("Root directory node is missing from the aggregate state.")

    special_entries = state.load_special_entries()
    total_stored_bytes = int(summary["total_stored_bytes"])
    total_changed_bytes = int(summary["total_changed_bytes"])

    temp_path = destination.with_suffix(destination.suffix + ".tmp")
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    with temp_path.open("wb") as handle:
        handle.write(b"\x00" * HEADER_STRUCT.size)

        def write_chunk(payload: dict[str, Any]) -> _ChunkRef:
            encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            offset = handle.tell()
            handle.write(encoded)
            return _ChunkRef(offset=offset, length=len(encoded))

        def write_directory_chunk(path: str) -> _ChunkRef:
            node = state.load_directory_node(path)
            if node is None:
                raise RuntimeError(f"Directory node {path!r} is missing from the aggregate state.")

            child_summaries = []
            for child in state.load_directory_children(path):
                child_ref = write_directory_chunk(str(child["path"]))
                child_summaries.append(
                    {
                        **_directory_summary(child, total_stored_bytes, total_changed_bytes),
                        "chunk_offset": child_ref.offset,
                        "chunk_length": child_ref.length,
                    }
                )

            payload = {
                "node": _directory_summary(node, total_stored_bytes, total_changed_bytes),
                "directories": child_summaries,
                "files": [
                    _file_summary(entry, total_stored_bytes, total_changed_bytes)
                    for entry in state.load_file_entries(path)
                ],
            }
            return write_chunk(payload)

        root_ref = write_directory_chunk(".\\")
        special_ref = None
        if special_entries:
            special_payload = {
                "node": _special_group_summary(special_entries, total_stored_bytes, total_changed_bytes),
                "buckets": [
                    _special_summary(entry, total_stored_bytes, total_changed_bytes)
                    for entry in special_entries
                ],
            }
            special_ref = write_chunk(special_payload)

        manifest = {
            "format": "macrium-viewpack",
            "version": VERSION,
            "target_file": summary["target_file"],
            "target_file_number": int(summary["target_file_number"]),
            "target_backup_type": summary["target_backup_type"],
            "parent_file_number": (
                None if summary["parent_file_number"] is None else int(summary["parent_file_number"])
            ),
            "requested_image_count": int(summary["requested_image_count"]),
            "analyzed_image_count": int(summary["analyzed_image_count"]),
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
                for image in analyzed_images
            ],
            "totals": {
                "stored_bytes": total_stored_bytes,
                "changed_bytes": total_changed_bytes,
                "bucket_count": int(summary["bucket_count"]),
            },
            "notes": notes,
            "root": {
                "path": ".\\",
                "chunk_offset": root_ref.offset,
                "chunk_length": root_ref.length,
            },
            "special": (
                {
                    "path": "[special]",
                    "chunk_offset": special_ref.offset,
                    "chunk_length": special_ref.length,
                }
                if special_ref is not None
                else None
            ),
            "state_db": str(state.path),
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

    temp_path.replace(destination)
    return destination
