from __future__ import annotations

import json
from pathlib import Path

from .models import AnalysisReport, AnalyzedImage, AttributionBucket, DirectoryTreeNode
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
    node_image_numbers: dict[str, set[int]] = {root.path: set(), synthetic_group.path: set()}

    def remember_image_numbers(path: str, bucket: AttributionBucket) -> None:
        image_numbers = node_image_numbers.setdefault(path, set())
        image_numbers.update(bucket.image_file_numbers)

    for bucket in buckets:
        components = _directory_components_for_bucket(bucket.key)
        if components is None:
            child_path = f"{synthetic_group.path}\\{bucket.key}"
            child = nodes.get(child_path)
            if child is None:
                child = DirectoryTreeNode(
                    name=bucket.key,
                    path=child_path,
                    kind="synthetic-bucket",
                )
                synthetic_group.children.append(child)
                nodes[child_path] = child
            _accumulate_node(synthetic_group, bucket)
            _accumulate_node(child, bucket)
            remember_image_numbers(synthetic_group.path, bucket)
            remember_image_numbers(child_path, bucket)
            continue

        _accumulate_node(root, bucket)
        remember_image_numbers(root.path, bucket)
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
            remember_image_numbers(child_path, bucket)
            current = child
            current_path = child_path

    for path, node in nodes.items():
        image_file_numbers = sorted(node_image_numbers.get(path, set()))
        node.image_occurrences = len(image_file_numbers)
        node.image_file_numbers = image_file_numbers

    if synthetic_group.children:
        synthetic_group.children.sort(key=lambda node: (-node.stored_bytes, node.name.lower()))
        root.children.append(synthetic_group)
    _sort_tree(root)
    return root


def _sort_tree(node: DirectoryTreeNode) -> None:
    node.children.sort(
        key=lambda child: (
            -child.stored_bytes,
            -child.image_occurrences,
            child.name.lower(),
        )
    )
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
        "image_occurrences": node.image_occurrences,
        "image_file_numbers": node.image_file_numbers,
        "child_count": len(node.children),
        "children": [
            _directory_tree_to_dict(child, total_stored_bytes, total_changed_bytes)
            for child in node.children
        ],
    }


def _select_file_numbers(backup_set: BackupSet, target_file_number: int, requested_image_count: int) -> list[int]:
    if requested_image_count <= 0:
        raise MrimgxError("Image count must be at least 1.")

    selected: list[int] = []
    current = target_file_number
    while True:
        selected.append(current)
        if len(selected) >= requested_image_count:
            break
        parent = backup_set.parent_file_number(current)
        if parent is None:
            break
        current = parent
    return list(reversed(selected))


def _count_changed_blocks(backup_set: BackupSet, file_number: int) -> int:
    layout = backup_set.files[file_number]
    return sum(
        len(backup_set.changed_block_indexes(file_number, partition_key))
        for partition_key in layout.partitions
    )


def _merge_image_buckets(
    aggregate: dict[str, AttributionBucket],
    image_buckets: dict[str, AttributionBucket],
) -> None:
    for key, image_bucket in image_buckets.items():
        bucket = aggregate.setdefault(key, AttributionBucket(key=key))
        bucket.stored_bytes += image_bucket.stored_bytes
        bucket.changed_bytes += image_bucket.changed_bytes
        bucket.changed_ranges += image_bucket.changed_ranges
        bucket.blocks += image_bucket.blocks
        for file_number in image_bucket.image_file_numbers:
            if file_number not in bucket.image_file_numbers:
                bucket.image_file_numbers.append(file_number)
        bucket.image_file_numbers.sort()
        bucket.image_occurrences = len(bucket.image_file_numbers)


def _analyze_single_restore_point(
    backup_set: BackupSet,
    file_number: int,
    *,
    include_parent_ownership: bool,
    progress: ProgressTracker,
    image_index: int,
    image_total: int,
    total_changed_block_count: int,
    analyzed_blocks_completed: int,
    notes: set[str],
) -> tuple[AnalyzedImage, dict[str, AttributionBucket], int]:
    layout = backup_set.files[file_number]
    parent_file_number = backup_set.parent_file_number(file_number)

    progress.update(
        "discover",
        "Resolved restore point and parent.",
        image_index=image_index,
        image_total=image_total,
        image_file_number=file_number,
        target_file_number=backup_set.target_file_number,
        parent_file_number=parent_file_number,
        backup_type=layout.backup_type,
    )

    current_snapshot = backup_set.build_snapshot(file_number)
    parent_snapshot = (
        backup_set.build_snapshot(parent_file_number)
        if include_parent_ownership and parent_file_number is not None
        else None
    )

    image_buckets: dict[str, AttributionBucket] = {}
    image_stored_bytes = 0
    image_changed_bytes = 0
    changed_block_count = 0

    notes.add(
        "Fast block-attribution mode is enabled. Stored bytes are attributed using changed-block ownership overlap, not exact byte-level diffs against the parent."
    )
    if parent_file_number is None:
        notes.add(
            "Full restore points use current snapshot ownership only because no parent image exists."
        )

    changed_block_map = {
        partition_key: backup_set.changed_block_indexes(file_number, partition_key)
        for partition_key in current_snapshot.partitions
    }

    partition_items = list(current_snapshot.partitions.items())
    for partition_index, (partition_key, current_partition) in enumerate(partition_items, start=1):
        source = current_partition.source
        changed_block_indexes = changed_block_map[partition_key]
        if not changed_block_indexes:
            continue
        progress.update(
            "partition",
            "Preparing partition analysis.",
            image_index=image_index,
            image_total=image_total,
            image_file_number=file_number,
            partition_index=partition_index,
            partition_total=len(partition_items),
            disk_number=source.disk_number,
            partition_number=source.partition_number,
            fs_type=source.fs_type,
            changed_block_total=len(changed_block_indexes),
            progress_completed=analyzed_blocks_completed,
            progress_total=total_changed_block_count,
        )

        current_mapper = None
        parent_mapper = None
        if source.is_ntfs:
            progress.update(
                "mapper",
                "Building current NTFS ownership map.",
                image_index=image_index,
                image_total=image_total,
                image_file_number=file_number,
                partition_index=partition_index,
                partition_total=len(partition_items),
                disk_number=source.disk_number,
                partition_number=source.partition_number,
                progress_completed=analyzed_blocks_completed,
                progress_total=total_changed_block_count,
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
                if parent_snapshot is not None:
                    progress.update(
                        "mapper",
                        "Building parent NTFS ownership map.",
                        image_index=image_index,
                        image_total=image_total,
                        image_file_number=file_number,
                        partition_index=partition_index,
                        partition_total=len(partition_items),
                        disk_number=source.disk_number,
                        partition_number=source.partition_number,
                        progress_completed=analyzed_blocks_completed,
                        progress_total=total_changed_block_count,
                    )
                    parent_map_reader = SnapshotPartitionReader(
                        backup_set,
                        parent_snapshot,
                        partition_key,
                        max_cached_blocks=16,
                    )
                    parent_mapper = NtfsMapper(parent_map_reader).build()
                    parent_map_reader.clear_cache()
            elif parent_file_number is not None:
                notes.add(
                    "Parent ownership fallback is disabled for efficiency; bytes no longer owned in the target snapshot are bucketed as deleted or previous-owner bytes."
                )
        else:
            notes.add(
                f"Partition disk {source.disk_number} partition {source.partition_number} is {source.fs_type or 'unknown'}; exact file attribution is NTFS-only, so this partition stays bucketed."
            )

        for block_counter, block_index in enumerate(changed_block_indexes, start=1):
            progress.update(
                "analyze",
                "Attributing changed blocks.",
                image_index=image_index,
                image_total=image_total,
                image_file_number=file_number,
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
            image_stored_bytes += stored_bytes
            block_base_offset = source.block_disk_offset(block_index)
            block_end_offset = block_base_offset + source.block_size
            image_changed_bytes += source.block_size
            changed_block_count += 1

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
                        if (not include_parent_ownership and parent_file_number is not None)
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
                        if source.is_ntfs and not include_parent_ownership and parent_file_number is not None
                        else "Unresolved NTFS bytes"
                    )
                    if not source.is_ntfs:
                        unresolved_key = (
                            f"Partition bucket: disk {source.disk_number} partition {source.partition_number} {source.fs_type or 'unknown'}"
                        )
                    block_buckets[unresolved_key] = block_buckets.get(unresolved_key, 0) + (source.block_size - covered)

            for key, changed_bytes in block_buckets.items():
                bucket = image_buckets.setdefault(key, AttributionBucket(key=key))
                bucket.changed_bytes += changed_bytes
                bucket.changed_ranges += 1
                bucket.blocks += 1
                bucket.stored_bytes += stored_bytes * (changed_bytes / source.block_size)
                if file_number not in bucket.image_file_numbers:
                    bucket.image_file_numbers.append(file_number)
                    bucket.image_occurrences = len(bucket.image_file_numbers)

            analyzed_blocks_completed += 1
            progress.update(
                "analyze",
                "Attributing changed blocks.",
                image_index=image_index,
                image_total=image_total,
                image_file_number=file_number,
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

    image_summary = AnalyzedImage(
        file_path=layout.file_path,
        file_number=file_number,
        backup_type=layout.backup_type,
        parent_file_number=parent_file_number,
        total_stored_bytes=image_stored_bytes,
        total_changed_bytes=image_changed_bytes,
        changed_block_count=changed_block_count,
        bucket_count=len(image_buckets),
    )
    return image_summary, image_buckets, analyzed_blocks_completed


def analyze_file(
    target_path: Path,
    *,
    include_parent_ownership: bool = False,
    progress: ProgressTracker | None = None,
    image_count: int = 1,
) -> AnalysisReport:
    tracker = progress or ProgressTracker()
    tracker.begin(
        "discover",
        "Opening backup set metadata.",
        target_file=str(target_path),
        requested_image_count=image_count,
    )
    with BackupSet.from_target_file(target_path) as backup_set:
        target_layout = backup_set.target_layout()
        selected_file_numbers = _select_file_numbers(backup_set, target_layout.file_number, image_count)
        if target_layout.file_number not in selected_file_numbers:
            raise MrimgxError("Target file was not selected for analysis.")

        total_changed_block_count = sum(
            _count_changed_blocks(backup_set, file_number)
            for file_number in selected_file_numbers
        )
        tracker.update(
            "discover",
            "Resolved restore point chain.",
            target_file_number=target_layout.file_number,
            parent_file_number=backup_set.parent_file_number(target_layout.file_number),
            backup_type=target_layout.backup_type,
            requested_image_count=image_count,
            analyzed_image_count=len(selected_file_numbers),
            selected_file_numbers=selected_file_numbers,
            progress_completed=0,
            progress_total=total_changed_block_count,
        )

        notes: set[str] = set()
        analyzed_images: list[AnalyzedImage] = []
        aggregate_buckets: dict[str, AttributionBucket] = {}
        total_stored_bytes = 0
        total_changed_bytes = 0
        analyzed_blocks_completed = 0

        for image_index, file_number in enumerate(selected_file_numbers, start=1):
            image_summary, image_buckets, analyzed_blocks_completed = _analyze_single_restore_point(
                backup_set,
                file_number,
                include_parent_ownership=include_parent_ownership,
                progress=tracker,
                image_index=image_index,
                image_total=len(selected_file_numbers),
                total_changed_block_count=total_changed_block_count,
                analyzed_blocks_completed=analyzed_blocks_completed,
                notes=notes,
            )
            analyzed_images.append(image_summary)
            total_stored_bytes += image_summary.total_stored_bytes
            total_changed_bytes += image_summary.total_changed_bytes
            _merge_image_buckets(aggregate_buckets, image_buckets)

        ordered_buckets = sorted(
            aggregate_buckets.values(),
            key=lambda bucket: (-bucket.stored_bytes, -bucket.image_occurrences, bucket.key.lower()),
        )
        directory_tree = _build_directory_tree(ordered_buckets)
        report = AnalysisReport(
            target_file=target_path,
            target_file_number=target_layout.file_number,
            target_backup_type=target_layout.backup_type,
            parent_file_number=backup_set.parent_file_number(target_layout.file_number),
            requested_image_count=image_count,
            analyzed_images=analyzed_images,
            total_stored_bytes=total_stored_bytes,
            total_changed_bytes=total_changed_bytes,
            buckets=ordered_buckets,
            directory_tree=directory_tree,
            notes=sorted(notes),
        )
        tracker.finish(
            phase="done",
            message="Analysis complete.",
            target_file=str(target_path),
            target_file_number=report.target_file_number,
            parent_file_number=report.parent_file_number,
            requested_image_count=report.requested_image_count,
            analyzed_image_count=len(report.analyzed_images),
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
            "total_stored_bytes": report.total_stored_bytes,
            "total_changed_bytes": report.total_changed_bytes,
            "buckets": [
                {
                    "key": bucket.key,
                    "stored_bytes": bucket.stored_bytes,
                    "changed_bytes": bucket.changed_bytes,
                    "changed_ranges": bucket.changed_ranges,
                    "blocks": bucket.blocks,
                    "image_occurrences": bucket.image_occurrences,
                    "image_file_numbers": bucket.image_file_numbers,
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
