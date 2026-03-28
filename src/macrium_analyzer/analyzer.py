from __future__ import annotations

import json
from pathlib import Path

from .models import AnalysisReport, AttributionBucket, DirectoryTreeNode
from .mrimgx import BackupSet, MrimgxError, SnapshotPartitionReader
from .ntfs import NtfsMapper
from .progress import ProgressTracker


def _bucket_key(owner: str) -> str:
    if owner.startswith(".\\$MFT"):
        return "NTFS metadata:$MFT"
    if owner.startswith(".\\$LogFile"):
        return "NTFS metadata:$LogFile"
    if owner.startswith(".\\$Extend\\$UsnJrnl"):
        return "NTFS metadata:$UsnJrnl"
    if owner.startswith(".\\$"):
        return f"NTFS metadata:{owner}"
    return owner


def _is_synthetic_bucket(key: str) -> bool:
    return (
        not key.startswith(".\\")
        or key.startswith(".\\$")
        or key.startswith("NTFS metadata:")
        or key.startswith("Partition bucket:")
        or key in {"Deleted or previous-owner bytes", "Unresolved NTFS bytes"}
    )


def _directory_components_for_bucket(key: str) -> list[str] | None:
    if _is_synthetic_bucket(key):
        return None

    normalized = key
    is_directory_bucket = False
    if normalized.endswith(":$I30"):
        normalized = normalized[: -len(":$I30")]
        is_directory_bucket = True
    elif ":" in normalized:
        normalized = normalized.split(":", 1)[0]

    if not normalized.startswith(".\\"):
        return None

    relative = normalized[2:]
    if not relative:
        return []

    components = [part for part in relative.split("\\") if part]
    if not components:
        return []
    if is_directory_bucket:
        return components
    return components[:-1]


def _accumulate_node(node: DirectoryTreeNode, bucket: AttributionBucket) -> None:
    node.stored_bytes += bucket.stored_bytes
    node.changed_bytes += bucket.changed_bytes
    node.changed_ranges += bucket.changed_ranges
    node.blocks += bucket.blocks


def _build_directory_tree(buckets: list[AttributionBucket]) -> DirectoryTreeNode:
    root = DirectoryTreeNode(name=".", path=".\\")
    synthetic_group = DirectoryTreeNode(name="[special]", path="[special]", kind="synthetic-group")
    nodes: dict[str, DirectoryTreeNode] = {root.path: root, synthetic_group.path: synthetic_group}

    for bucket in buckets:
        components = _directory_components_for_bucket(bucket.key)
        if components is None:
            synthetic_node = synthetic_group
            child_path = f"{synthetic_group.path}\\{bucket.key}"
            child = nodes.get(child_path)
            if child is None:
                child = DirectoryTreeNode(
                    name=bucket.key,
                    path=child_path,
                    kind="synthetic-bucket",
                )
                synthetic_node.children.append(child)
                nodes[child_path] = child
            _accumulate_node(synthetic_group, bucket)
            _accumulate_node(child, bucket)
            continue

        _accumulate_node(root, bucket)
        current = root
        current_path = root.path
        for component in components:
            if current_path == ".\\":
                child_path = f".\\{component}"
            else:
                child_path = f"{current_path}\\{component}"
            child = nodes.get(child_path)
            if child is None:
                child = DirectoryTreeNode(name=component, path=child_path)
                current.children.append(child)
                nodes[child_path] = child
            _accumulate_node(child, bucket)
            current = child
            current_path = child_path

    if synthetic_group.children:
        synthetic_group.children.sort(key=lambda node: (-node.stored_bytes, node.name.lower()))
        root.children.append(synthetic_group)
    _sort_tree(root)
    return root


def _sort_tree(node: DirectoryTreeNode) -> None:
    node.children.sort(key=lambda child: (-child.stored_bytes, child.name.lower()))
    for child in node.children:
        _sort_tree(child)


def _directory_tree_to_dict(node: DirectoryTreeNode, total_stored_bytes: int, total_changed_bytes: int) -> dict:
    stored_percent = 0.0 if total_stored_bytes == 0 else (node.stored_bytes / total_stored_bytes) * 100.0
    logical_percent = 0.0 if total_changed_bytes == 0 else (node.changed_bytes / total_changed_bytes) * 100.0
    return {
        "name": node.name,
        "path": node.path,
        "kind": node.kind,
        "stored_bytes": node.stored_bytes,
        "stored_percent": stored_percent,
        "changed_bytes": node.changed_bytes,
        "changed_percent": logical_percent,
        "changed_ranges": node.changed_ranges,
        "blocks": node.blocks,
        "child_count": len(node.children),
        "children": [
            _directory_tree_to_dict(child, total_stored_bytes, total_changed_bytes)
            for child in node.children
        ],
    }


def analyze_file(
    target_path: Path,
    *,
    include_parent_ownership: bool = False,
    progress: ProgressTracker | None = None,
) -> AnalysisReport:
    notes: list[str] = []
    tracker = progress or ProgressTracker()
    tracker.begin("discover", "Opening backup set metadata.", target_file=str(target_path))
    with BackupSet.from_target_file(target_path) as backup_set:
        target_layout = backup_set.target_layout()
        parent_file_number = backup_set.parent_file_number(target_layout.file_number)
        if parent_file_number is None:
            raise MrimgxError("Full backups are not yet supported by the attribution analyzer.")
        tracker.update(
            "discover",
            "Resolved target and parent restore points.",
            target_file_number=target_layout.file_number,
            parent_file_number=parent_file_number,
            backup_type=target_layout.backup_type,
        )

        current_snapshot = backup_set.build_snapshot(target_layout.file_number)
        parent_snapshot = (
            backup_set.build_snapshot(parent_file_number)
            if include_parent_ownership
            else None
        )

        buckets: dict[str, AttributionBucket] = {}
        total_stored_bytes = 0
        total_changed_bytes = 0
        notes.append(
            "Fast block-attribution mode is enabled. Stored bytes are attributed using changed-block ownership overlap, not exact byte-level diffs against the parent."
        )

        changed_block_map = {
            partition_key: backup_set.changed_block_indexes(target_layout.file_number, partition_key)
            for partition_key in current_snapshot.partitions
        }
        total_changed_block_count = sum(len(blocks) for blocks in changed_block_map.values())
        analyzed_blocks_completed = 0

        partition_items = list(current_snapshot.partitions.items())
        for partition_index, (partition_key, current_partition) in enumerate(partition_items, start=1):
            source = current_partition.source
            changed_block_indexes = changed_block_map[partition_key]
            if not changed_block_indexes:
                continue
            tracker.update(
                "partition",
                "Preparing partition analysis.",
                partition_index=partition_index,
                partition_total=len(partition_items),
                disk_number=source.disk_number,
                partition_number=source.partition_number,
                fs_type=source.fs_type,
                changed_block_total=len(changed_block_indexes),
            )

            current_mapper = None
            parent_mapper = None
            if source.is_ntfs:
                tracker.update(
                    "mapper",
                    "Building current NTFS ownership map.",
                    partition_index=partition_index,
                    partition_total=len(partition_items),
                    disk_number=source.disk_number,
                    partition_number=source.partition_number,
                )
                current_map_reader = SnapshotPartitionReader(
                    backup_set,
                    current_snapshot,
                    partition_key,
                    max_cached_blocks=16,
                )
                current_mapper = NtfsMapper(current_map_reader).build()
                current_map_reader.clear_cache()
                if include_parent_ownership:
                    if parent_snapshot is None:
                        raise MrimgxError("Parent snapshot was not built.")
                    tracker.update(
                        "mapper",
                        "Building parent NTFS ownership map.",
                        partition_index=partition_index,
                        partition_total=len(partition_items),
                        disk_number=source.disk_number,
                        partition_number=source.partition_number,
                    )
                    parent_map_reader = SnapshotPartitionReader(
                        backup_set,
                        parent_snapshot,
                        partition_key,
                        max_cached_blocks=16,
                    )
                    parent_mapper = NtfsMapper(parent_map_reader).build()
                    parent_map_reader.clear_cache()
                else:
                    notes.append(
                        "Parent ownership fallback is disabled for efficiency; bytes no longer owned in the target snapshot are bucketed as deleted or previous-owner bytes."
                    )
            else:
                notes.append(
                    f"Partition disk {source.disk_number} partition {source.partition_number} is {source.fs_type or 'unknown'}; exact file attribution is NTFS-only, so this partition stays bucketed."
                )

            for block_counter, block_index in enumerate(changed_block_indexes, start=1):
                tracker.update(
                    "analyze",
                    "Attributing changed blocks.",
                    partition_index=partition_index,
                    partition_total=len(partition_items),
                    disk_number=source.disk_number,
                    partition_number=source.partition_number,
                    changed_blocks_completed=block_counter,
                    changed_block_total=len(changed_block_indexes),
                    changed_block_index=block_index,
                    progress_completed=analyzed_blocks_completed,
                    progress_total=total_changed_block_count,
                )
                resolved_block = current_partition.resolved_data_blocks[block_index]
                stored_bytes = resolved_block.compressed_length
                total_stored_bytes += stored_bytes
                block_base_offset = source.block_disk_offset(block_index)
                block_end_offset = block_base_offset + source.block_size
                total_changed_bytes += source.block_size
                block_buckets: dict[str, int] = {}
                owner_extents = current_mapper.find_overlaps(block_base_offset, block_end_offset) if current_mapper else []
                if not owner_extents and parent_mapper:
                    owner_extents = parent_mapper.find_overlaps(block_base_offset, block_end_offset)
                if not owner_extents:
                    key = (
                        f"Partition bucket: disk {source.disk_number} partition {source.partition_number} {source.fs_type or 'unknown'}"
                        if not source.is_ntfs
                        else (
                            "Deleted or previous-owner bytes"
                            if not include_parent_ownership
                            else "Unresolved NTFS bytes"
                        )
                    )
                    block_buckets[key] = source.block_size
                else:
                    covered = 0
                    for extent in owner_extents:
                        overlap_start = max(block_base_offset, extent.start)
                        overlap_end = min(block_end_offset, extent.end)
                        if overlap_end <= overlap_start:
                            continue
                        key = _bucket_key(extent.owner)
                        overlap_length = overlap_end - overlap_start
                        block_buckets[key] = block_buckets.get(key, 0) + overlap_length
                        covered += overlap_length
                    if covered < source.block_size:
                        unresolved_key = (
                            "Deleted or previous-owner bytes"
                            if source.is_ntfs and not include_parent_ownership
                            else "Unresolved NTFS bytes"
                        )
                        if not source.is_ntfs:
                            unresolved_key = (
                                f"Partition bucket: disk {source.disk_number} partition {source.partition_number} {source.fs_type or 'unknown'}"
                            )
                        block_buckets[unresolved_key] = block_buckets.get(unresolved_key, 0) + (source.block_size - covered)

                for key, changed_bytes in block_buckets.items():
                    bucket = buckets.setdefault(key, AttributionBucket(key=key))
                    bucket.changed_bytes += changed_bytes
                    bucket.changed_ranges += 1
                    bucket.blocks += 1
                    bucket.stored_bytes += stored_bytes * (changed_bytes / source.block_size)
                analyzed_blocks_completed += 1
                tracker.update(
                    "analyze",
                    "Attributing changed blocks.",
                    partition_index=partition_index,
                    partition_total=len(partition_items),
                    disk_number=source.disk_number,
                    partition_number=source.partition_number,
                    changed_blocks_completed=block_counter,
                    changed_block_total=len(changed_block_indexes),
                    changed_block_index=block_index,
                    progress_completed=analyzed_blocks_completed,
                    progress_total=total_changed_block_count,
                )

        ordered_buckets = sorted(buckets.values(), key=lambda bucket: bucket.stored_bytes, reverse=True)
        directory_tree = _build_directory_tree(ordered_buckets)
        report = AnalysisReport(
            target_file=target_path,
            target_file_number=target_layout.file_number,
            target_backup_type=target_layout.backup_type,
            parent_file_number=parent_file_number,
            total_stored_bytes=total_stored_bytes,
            total_changed_bytes=total_changed_bytes,
            buckets=ordered_buckets,
            directory_tree=directory_tree,
            notes=sorted(set(notes)),
        )
        tracker.finish(
            phase="done",
            message="Analysis complete.",
            target_file=str(target_path),
            target_file_number=report.target_file_number,
            parent_file_number=report.parent_file_number,
            total_stored_bytes=report.total_stored_bytes,
            total_changed_bytes=report.total_changed_bytes,
            bucket_count=len(report.buckets),
            progress_completed=total_changed_block_count,
            progress_total=total_changed_block_count,
        )
        return report


def report_to_json(report: AnalysisReport) -> str:
    return json.dumps(
        {
            "target_file": str(report.target_file),
            "target_file_number": report.target_file_number,
            "target_backup_type": report.target_backup_type,
            "parent_file_number": report.parent_file_number,
            "total_stored_bytes": report.total_stored_bytes,
            "total_changed_bytes": report.total_changed_bytes,
            "buckets": [
                {
                    "key": bucket.key,
                    "stored_bytes": bucket.stored_bytes,
                    "changed_bytes": bucket.changed_bytes,
                    "changed_ranges": bucket.changed_ranges,
                    "blocks": bucket.blocks,
                }
                for bucket in report.buckets
            ],
            "directory_tree": _directory_tree_to_dict(
                report.directory_tree,
                report.total_stored_bytes,
                report.total_changed_bytes,
            ),
            "notes": report.notes,
        },
        indent=2,
    )
