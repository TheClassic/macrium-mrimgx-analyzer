from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


PartitionKey = tuple[int, int]


@dataclass(frozen=True)
class DataBlockRef:
    file_position: int
    md5_hash: bytes
    compressed_length: int
    file_number: int


@dataclass(frozen=True)
class DeltaBlockRef:
    block_index: int
    block: DataBlockRef


@dataclass
class PartitionLayout:
    disk_number: int
    partition_number: int
    partition_index: int
    bytes_per_sector: int
    block_size: int
    block_count: int
    start: int
    end: int
    length: int
    boot_sector_offset: int
    fs_type: str
    fs_start: int
    fs_end: int
    lcn0_offset: int
    sectors_per_cluster: int
    total_clusters: int
    mft_offset: int
    mft_record_size: int
    reserved_sectors_byte_length: int
    volume_guid: str
    volume_label: str
    data_blocks: list[DataBlockRef] = field(default_factory=list)
    delta_data_blocks: list[DeltaBlockRef] = field(default_factory=list)
    reserved_sector_blocks: list[DataBlockRef] = field(default_factory=list)

    @property
    def key(self) -> PartitionKey:
        return (self.disk_number, self.partition_number)

    @property
    def cluster_size(self) -> int:
        if self.sectors_per_cluster <= 0:
            return 0
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def cluster0_disk_offset(self) -> int:
        return self.start + (self.lcn0_offset - self.fs_start)

    @property
    def is_ntfs(self) -> bool:
        return self.fs_type == "NTFS"

    def block_disk_offset(self, block_index: int) -> int:
        return self.cluster0_disk_offset + (self.block_size * block_index)

    def snapshot_view(self) -> "PartitionLayout":
        return replace(
            self,
            data_blocks=[],
            delta_data_blocks=[],
            reserved_sector_blocks=[],
        )


@dataclass
class BackupFileLayout:
    file_path: Path
    image_id: str
    file_number: int
    increment_number: int
    backup_type: str
    delta_index: bool
    split_file: bool
    merged_files: list[int]
    compression_level: str
    compression_method: str
    encryption_enabled: bool
    json_data: dict[str, Any]
    partitions: dict[PartitionKey, PartitionLayout]


@dataclass
class SnapshotPartition:
    source: PartitionLayout
    resolved_data_blocks: list[DataBlockRef]


@dataclass
class ResolvedSnapshot:
    file_number: int
    backup_type: str
    partitions: dict[PartitionKey, SnapshotPartition]


@dataclass
class Extent:
    start: int
    end: int
    owner: str


@dataclass
class NtfsRecord:
    record_number: int
    in_use: bool = False
    is_directory: bool = False
    parent_record_number: int | None = None
    display_name: str | None = None
    data_attributes: list[tuple[str, list[tuple[int | None, int]], int, int]] = field(default_factory=list)


@dataclass
class AttributionBucket:
    key: str
    stored_bytes: float = 0.0
    changed_bytes: int = 0
    changed_ranges: int = 0
    blocks: int = 0


@dataclass
class DirectoryTreeNode:
    name: str
    path: str
    kind: str = "directory"
    stored_bytes: float = 0.0
    changed_bytes: int = 0
    changed_ranges: int = 0
    blocks: int = 0
    children: list["DirectoryTreeNode"] = field(default_factory=list)


@dataclass
class AnalysisReport:
    target_file: Path
    target_file_number: int
    target_backup_type: str
    parent_file_number: int | None
    total_stored_bytes: int
    total_changed_bytes: int
    buckets: list[AttributionBucket]
    directory_tree: DirectoryTreeNode
    notes: list[str]
