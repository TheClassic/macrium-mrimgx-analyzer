from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .models import AnalyzedImage, AttributionBucket
from .mrimgx import BackupSet, MrimgxError, SnapshotPartitionReader
from .ntfs import NtfsMapper
from .progress import ProgressTracker
from .state_db import AggregateState


@dataclass
class _ParallelWorkerResult:
    image_index: int
    image_summary: AnalyzedImage
    notes: list[str]
    buckets: dict[str, AttributionBucket]


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


def _merge_bucket_maps(
    destination: dict[str, AttributionBucket],
    source: dict[str, AttributionBucket],
) -> None:
    for key, bucket in source.items():
        aggregate = destination.setdefault(key, AttributionBucket(key=key))
        aggregate.stored_bytes += bucket.stored_bytes
        aggregate.changed_bytes += bucket.changed_bytes
        aggregate.changed_ranges += bucket.changed_ranges
        aggregate.blocks += bucket.blocks


def _flush_pending_buckets(
    state: AggregateState | None,
    file_number: int,
    pending_buckets: dict[str, AttributionBucket],
    collected_buckets: dict[str, AttributionBucket] | None = None,
) -> None:
    if not pending_buckets:
        return
    if state is not None:
        state.add_bucket_batch(file_number, pending_buckets)
    else:
        if collected_buckets is None:
            raise ValueError("Collected buckets are required when no aggregate state is provided.")
        _merge_bucket_maps(collected_buckets, pending_buckets)
    pending_buckets.clear()


def _analyze_single_restore_point(
    backup_set: BackupSet,
    file_number: int,
    *,
    include_parent_ownership: bool,
    progress: ProgressTracker | None,
    image_index: int | None,
    image_total: int | None,
    total_changed_block_count: int = 0,
    analyzed_blocks_completed: int = 0,
    notes: set[str],
    state: AggregateState | None,
) -> tuple[AnalyzedImage, int, dict[str, AttributionBucket]]:
    layout = backup_set.files[file_number]
    parent_file_number = backup_set.parent_file_number(file_number)
    collected_buckets: dict[str, AttributionBucket] = {}

    if progress is not None and image_index is not None and image_total is not None:
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

    image_stored_bytes = 0
    image_changed_bytes = 0
    changed_block_count = 0
    image_bucket_keys: set[str] = set()
    pending_buckets: dict[str, AttributionBucket] = {}
    flush_threshold = 4096

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
        if progress is not None and image_index is not None and image_total is not None:
            progress.update(
                "aggregate",
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
                state_db_path=(state.db_label if state is not None else ":worker:"),
            )

        current_mapper = None
        parent_mapper = None
        if source.is_ntfs:
            if progress is not None and image_index is not None and image_total is not None:
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
                    state_db_path=(state.db_label if state is not None else ":worker:"),
                )
            current_map_reader = SnapshotPartitionReader(
                backup_set,
                current_snapshot,
                partition_key,
                max_cached_blocks=16,
            )
            current_mapper = NtfsMapper(current_map_reader).build()
            current_map_reader.clear_cache()
            if include_parent_ownership and parent_snapshot is not None:
                if progress is not None and image_index is not None and image_total is not None:
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
                        state_db_path=(state.db_label if state is not None else ":worker:"),
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
            if progress is not None and image_index is not None and image_total is not None:
                progress.update(
                    "aggregate",
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
                    state_db_path=(state.db_label if state is not None else ":worker:"),
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
                bucket = pending_buckets.setdefault(key, AttributionBucket(key=key))
                bucket.changed_bytes += changed_bytes
                bucket.changed_ranges += 1
                bucket.blocks += 1
                bucket.stored_bytes += stored_bytes * (changed_bytes / source.block_size)
                image_bucket_keys.add(key)

            if len(pending_buckets) >= flush_threshold:
                _flush_pending_buckets(state, file_number, pending_buckets, collected_buckets)

            analyzed_blocks_completed += 1
            if progress is not None and image_index is not None and image_total is not None:
                progress.update(
                    "aggregate",
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
                    state_db_path=(state.db_label if state is not None else ":worker:"),
                )

    _flush_pending_buckets(state, file_number, pending_buckets, collected_buckets)
    image_summary = AnalyzedImage(
        file_path=layout.file_path,
        file_number=file_number,
        backup_type=layout.backup_type,
        parent_file_number=parent_file_number,
        total_stored_bytes=image_stored_bytes,
        total_changed_bytes=image_changed_bytes,
        changed_block_count=changed_block_count,
        bucket_count=len(image_bucket_keys),
    )
    return image_summary, analyzed_blocks_completed, collected_buckets


def _analyze_single_restore_point_worker(
    target_path: str,
    file_number: int,
    *,
    include_parent_ownership: bool,
    image_index: int,
    image_total: int,
) -> _ParallelWorkerResult:
    notes: set[str] = set()
    with BackupSet.from_target_file(Path(target_path)) as backup_set:
        image_summary, _ignored_completed, buckets = _analyze_single_restore_point(
            backup_set,
            file_number,
            include_parent_ownership=include_parent_ownership,
            progress=None,
            image_index=image_index,
            image_total=image_total,
            notes=notes,
            state=None,
        )
    return _ParallelWorkerResult(
        image_index=image_index,
        image_summary=image_summary,
        notes=sorted(notes),
        buckets=buckets,
    )


def analyze_file(
    target_path: Path,
    *,
    state_db_path: Path | None = None,
    in_memory_state: bool = False,
    include_parent_ownership: bool = False,
    progress: ProgressTracker | None = None,
    image_count: int = 1,
    parallel_images: int = 6,
) -> AggregateState:
    tracker = progress or ProgressTracker()
    state_label = ":memory:" if in_memory_state else str(state_db_path)
    tracker.begin(
        "discover",
        "Opening backup set metadata.",
        target_file=str(target_path),
        requested_image_count=image_count,
        state_db_path=state_label,
    )
    with BackupSet.from_target_file(target_path) as backup_set:
        target_layout = backup_set.target_layout()
        selected_file_numbers = _select_file_numbers(backup_set, target_layout.file_number, image_count)
        if target_layout.file_number not in selected_file_numbers:
            raise MrimgxError("Target file was not selected for analysis.")

        state = AggregateState.create(
            state_db_path,
            target_file=target_path,
            target_file_number=target_layout.file_number,
            target_backup_type=target_layout.backup_type,
            parent_file_number=backup_set.parent_file_number(target_layout.file_number),
            requested_image_count=image_count,
            in_memory=in_memory_state,
        )
        per_image_changed_block_count = {
            file_number: _count_changed_blocks(backup_set, file_number)
            for file_number in selected_file_numbers
        }
        total_changed_block_count = sum(per_image_changed_block_count.values())
        tracker.update(
            "state-init",
            "Initialized aggregate state database.",
            target_file_number=target_layout.file_number,
            parent_file_number=backup_set.parent_file_number(target_layout.file_number),
            backup_type=target_layout.backup_type,
            requested_image_count=image_count,
            analyzed_image_count=len(selected_file_numbers),
            selected_file_numbers=selected_file_numbers,
            progress_completed=0,
            progress_total=total_changed_block_count,
            state_db_path=state.db_label,
        )

        notes: set[str] = set()
        analyzed_blocks_completed = 0

        worker_count = min(max(int(parallel_images), 1), len(selected_file_numbers))
        if worker_count > 1 and len(selected_file_numbers) > 1:
            tracker.update(
                "aggregate",
                "Launching parallel image workers.",
                image_total=len(selected_file_numbers),
                progress_completed=0,
                progress_total=total_changed_block_count,
                parallel_workers=worker_count,
                state_db_path=state.db_label,
            )
            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                future_map = {
                    executor.submit(
                        _analyze_single_restore_point_worker,
                        str(target_path),
                        file_number,
                        include_parent_ownership=include_parent_ownership,
                        image_index=image_index,
                        image_total=len(selected_file_numbers),
                    ): (image_index, file_number)
                    for image_index, file_number in enumerate(selected_file_numbers, start=1)
                }
                for future in as_completed(future_map):
                    image_index, file_number = future_map[future]
                    result = future.result()
                    state.add_bucket_batch(result.image_summary.file_number, result.buckets)
                    state.add_analyzed_image(result.image_index, result.image_summary)
                    notes.update(result.notes)
                    analyzed_blocks_completed += per_image_changed_block_count[file_number]
                    tracker.update(
                        "aggregate",
                        "Merged parallel worker results.",
                        image_index=image_index,
                        image_total=len(selected_file_numbers),
                        image_file_number=file_number,
                        progress_completed=analyzed_blocks_completed,
                        progress_total=total_changed_block_count,
                        parallel_workers=worker_count,
                        state_db_path=state.db_label,
                    )
        else:
            for image_index, file_number in enumerate(selected_file_numbers, start=1):
                image_summary, analyzed_blocks_completed, _ignored_buckets = _analyze_single_restore_point(
                    backup_set,
                    file_number,
                    include_parent_ownership=include_parent_ownership,
                    progress=tracker,
                    image_index=image_index,
                    image_total=len(selected_file_numbers),
                    total_changed_block_count=total_changed_block_count,
                    analyzed_blocks_completed=analyzed_blocks_completed,
                    notes=notes,
                    state=state,
                )
                state.add_analyzed_image(image_index, image_summary)

        for note in sorted(notes):
            state.record_note(note)
        state.finalize_run()

        tracker.update(
            "rollup",
            "Building directory rollups from aggregate state.",
            progress_completed=total_changed_block_count,
            progress_total=total_changed_block_count,
            state_db_path=state.db_label,
        )
        state.finalize_directory_rollups()
        tracker.update(
            "rollup",
            "Directory rollups complete.",
            progress_completed=total_changed_block_count,
            progress_total=total_changed_block_count,
            state_db_path=state.db_label,
        )

    return state
