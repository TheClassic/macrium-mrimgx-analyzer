from __future__ import annotations

import hashlib
import json
import struct
from collections import OrderedDict
from pathlib import Path

import zstandard

from .models import (
    BackupFileLayout,
    DataBlockRef,
    DeltaBlockRef,
    PartitionKey,
    PartitionLayout,
    ResolvedSnapshot,
    SnapshotPartition,
)


JSON_HEADER = b"$JSON   "
BITMAP_HEADER = b"$BITMAP "
TRACK0_HEADER = b"$TRACK0 "
INDEX_HEADER = b"$INDEX  "
MAGIC_BYTES = b"MACRIUM_FILE"

FOOTER_STRUCT = struct.Struct("<Q12s")
METADATA_HEADER_STRUCT = struct.Struct("<8sI16sB3s")
DATA_BLOCK_STRUCT = struct.Struct("<q16sIH")
DELTA_BLOCK_STRUCT = struct.Struct("<q16sIHI")

_ZSTD = zstandard.ZstdDecompressor()


class MrimgxError(RuntimeError):
    pass


def _parse_data_block(payload: bytes, offset: int) -> DataBlockRef:
    file_position, md5_hash, block_length, file_number = DATA_BLOCK_STRUCT.unpack_from(payload, offset)
    return DataBlockRef(
        file_position=file_position,
        md5_hash=md5_hash,
        compressed_length=block_length,
        file_number=file_number,
    )


def _read_metadata_block(
    file_obj,
    *,
    flags_byte: int,
    block_length: int,
    expected_md5: bytes,
) -> bytes:
    if block_length == 0:
        return b""

    payload = file_obj.read(block_length)
    if len(payload) != block_length:
        raise MrimgxError("Unexpected end of file while reading metadata block.")

    actual_md5 = hashlib.md5(payload, usedforsecurity=False).digest()
    if actual_md5 != expected_md5:
        raise MrimgxError("Metadata block hash mismatch.")

    encrypted = bool(flags_byte & 0b100)
    compressed = bool(flags_byte & 0b010)

    if encrypted:
        raise NotImplementedError("Encrypted backups are not implemented yet.")

    if compressed:
        return _ZSTD.decompress(payload)

    return payload


def _skip_disk_metadata(file_obj) -> None:
    found_track0 = False
    while True:
        header_bytes = file_obj.read(METADATA_HEADER_STRUCT.size)
        if len(header_bytes) != METADATA_HEADER_STRUCT.size:
            raise MrimgxError("Unexpected end of file while reading disk metadata.")
        block_name, block_length, _md5, flags_byte, _padding = METADATA_HEADER_STRUCT.unpack(header_bytes)
        file_obj.seek(block_length, 1)
        if block_name == TRACK0_HEADER:
            found_track0 = True
        if flags_byte & 0b001:
            break
    if not found_track0:
        raise MrimgxError("TRACK0 metadata block not found.")


def _skip_partition_metadata(file_obj) -> None:
    while True:
        header_offset = file_obj.tell()
        header_bytes = file_obj.read(METADATA_HEADER_STRUCT.size)
        if len(header_bytes) != METADATA_HEADER_STRUCT.size:
            raise MrimgxError("Unexpected end of file while reading partition metadata.")
        block_name, block_length, md5_hash, flags_byte, _padding = METADATA_HEADER_STRUCT.unpack(header_bytes)
        if block_name == BITMAP_HEADER:
            _read_metadata_block(
                file_obj,
                flags_byte=flags_byte,
                block_length=block_length,
                expected_md5=md5_hash,
            )
        elif block_name == INDEX_HEADER:
            _read_metadata_block(
                file_obj,
                flags_byte=flags_byte,
                block_length=block_length,
                expected_md5=md5_hash,
            )
            file_obj.seek(header_offset + METADATA_HEADER_STRUCT.size)
            return
        else:
            file_obj.seek(block_length, 1)
        if flags_byte & 0b001:
            break
    raise MrimgxError("INDEX metadata block not found.")


def _read_json_metadata(file_obj) -> str:
    found_json = False
    json_text = ""
    while True:
        header_bytes = file_obj.read(METADATA_HEADER_STRUCT.size)
        if len(header_bytes) != METADATA_HEADER_STRUCT.size:
            raise MrimgxError("Unexpected end of file while reading root metadata.")
        block_name, block_length, md5_hash, flags_byte, _padding = METADATA_HEADER_STRUCT.unpack(header_bytes)
        if block_name == JSON_HEADER:
            payload = _read_metadata_block(
                file_obj,
                flags_byte=flags_byte,
                block_length=block_length,
                expected_md5=md5_hash,
            )
            json_text = payload.decode("utf-8")
            found_json = True
        else:
            file_obj.seek(block_length, 1)
        if flags_byte & 0b001:
            break
    if not found_json:
        raise MrimgxError("JSON metadata block not found.")
    return json_text


def _parse_json_layout(file_path: Path, json_data: dict) -> BackupFileLayout:
    header = json_data["_header"]
    compression = json_data.get("_compression", {})
    encryption = json_data.get("_encryption", {})

    partitions: dict[PartitionKey, PartitionLayout] = {}
    for disk in json_data.get("disks", []):
        disk_header = disk["_header"]
        disk_geometry = disk["_geometry"]
        for partition in disk.get("partitions", []):
            part_header = partition["_header"]
            part_geometry = partition["_geometry"]
            file_system = partition.get("_file_system", {})
            layout = PartitionLayout(
                disk_number=int(disk_header["disk_number"]),
                partition_number=int(part_header["partition_number"]),
                partition_index=int(file_system.get("partition_index", part_header["partition_number"])),
                bytes_per_sector=int(disk_geometry["bytes_per_sector"]),
                block_size=int(part_header["block_size"]),
                block_count=int(part_header["block_count"]),
                start=int(part_geometry["start"]),
                end=int(part_geometry["end"]),
                length=int(part_geometry["length"]),
                boot_sector_offset=int(part_geometry["boot_sector_offset"]),
                fs_type=str(file_system.get("type", "unknown")),
                fs_start=int(file_system.get("start", part_geometry["start"])),
                fs_end=int(file_system.get("end", part_geometry["end"])),
                lcn0_offset=int(file_system.get("lcn0_offset", part_geometry["start"])),
                sectors_per_cluster=int(file_system.get("sectors_per_cluster", 0)),
                total_clusters=int(file_system.get("total_clusters", 0)),
                mft_offset=int(file_system.get("mft_offset", 0)),
                mft_record_size=int(file_system.get("mft_record_size", 0)),
                reserved_sectors_byte_length=int(file_system.get("reserved_sectors_byte_length", 0)),
                volume_guid=str(file_system.get("volume_guid", "")),
                volume_label=str(file_system.get("volume_label", "")),
            )
            partitions[layout.key] = layout

    return BackupFileLayout(
        file_path=file_path,
        image_id=str(header["imageid"]),
        file_number=int(header["file_number"]),
        increment_number=int(header["increment_number"]),
        backup_type=str(header["backup_type"]),
        delta_index=bool(header["delta_index"]),
        split_file=bool(header["split_file"]),
        merged_files=[int(value) for value in header.get("merged_files", [])],
        compression_level=str(compression.get("compression_level", "none")),
        compression_method=str(compression.get("compression_method", "")),
        encryption_enabled=bool(encryption.get("enable", False)),
        json_data=json_data,
        partitions=partitions,
    )


def read_backup_file(file_path: Path, *, load_index: bool) -> BackupFileLayout:
    with file_path.open("rb") as file_obj:
        file_obj.seek(-FOOTER_STRUCT.size, 2)
        header_offset, magic = FOOTER_STRUCT.unpack(file_obj.read(FOOTER_STRUCT.size))
        if magic != MAGIC_BYTES:
            raise MrimgxError(f"{file_path} is not a Macrium Reflect X backup file.")

        file_obj.seek(header_offset)
        json_text = _read_json_metadata(file_obj)
        json_data = json.loads(json_text)
        backup = _parse_json_layout(file_path, json_data)
        if backup.encryption_enabled:
            raise NotImplementedError("Encrypted backups are not implemented yet.")

        if not load_index or backup.split_file:
            return backup

        file_obj.seek(int(backup.json_data["_header"]["index_file_position"]))
        disk_keys = sorted({key[0] for key in backup.partitions})
        for disk_number in disk_keys:
            _skip_disk_metadata(file_obj)
            partition_keys = sorted(key for key in backup.partitions if key[0] == disk_number)
            for partition_key in partition_keys:
                partition = backup.partitions[partition_key]
                _skip_partition_metadata(file_obj)

                reserved_count = struct.unpack("<i", file_obj.read(4))[0]
                if reserved_count > 0:
                    reserved_payload = file_obj.read(reserved_count * DATA_BLOCK_STRUCT.size)
                    partition.reserved_sector_blocks = [
                        _parse_data_block(reserved_payload, offset)
                        for offset in range(0, len(reserved_payload), DATA_BLOCK_STRUCT.size)
                    ]

                block_count = struct.unpack("<i", file_obj.read(4))[0]
                if block_count <= 0:
                    continue

                if backup.delta_index:
                    payload = file_obj.read(block_count * DELTA_BLOCK_STRUCT.size)
                    delta_blocks: list[DeltaBlockRef] = []
                    for offset in range(0, len(payload), DELTA_BLOCK_STRUCT.size):
                        file_position, md5_hash, block_length, file_number, block_index = DELTA_BLOCK_STRUCT.unpack_from(payload, offset)
                        delta_blocks.append(
                            DeltaBlockRef(
                                block_index=block_index,
                                block=DataBlockRef(
                                    file_position=file_position,
                                    md5_hash=md5_hash,
                                    compressed_length=block_length,
                                    file_number=file_number,
                                ),
                            )
                        )
                    partition.delta_data_blocks = delta_blocks
                else:
                    payload = file_obj.read(block_count * DATA_BLOCK_STRUCT.size)
                    partition.data_blocks = [
                        _parse_data_block(payload, offset)
                        for offset in range(0, len(payload), DATA_BLOCK_STRUCT.size)
                    ]

        return backup


class BackupSet:
    def __init__(self, files: dict[int, BackupFileLayout], target_file_number: int):
        self.files = files
        self.target_file_number = target_file_number
        self.ordered_file_numbers = sorted(files)
        self._file_handles: dict[int, object] = {}
        self._file_number_to_path: dict[int, Path] = {}
        for file_layout in files.values():
            self._file_number_to_path[file_layout.file_number] = file_layout.file_path
            for merged_file_number in file_layout.merged_files:
                self._file_number_to_path.setdefault(merged_file_number, file_layout.file_path)

    @classmethod
    def from_target_file(cls, target_path: Path) -> "BackupSet":
        target_metadata = read_backup_file(target_path, load_index=False)
        files: dict[int, BackupFileLayout] = {}
        for candidate in target_path.parent.iterdir():
            if not candidate.is_file() or candidate.suffix.lower() != target_path.suffix.lower():
                continue
            try:
                candidate_metadata = read_backup_file(candidate, load_index=False)
            except Exception:
                continue
            if candidate_metadata.image_id != target_metadata.image_id:
                continue
            if candidate_metadata.increment_number > target_metadata.increment_number:
                continue
            files[candidate_metadata.file_number] = read_backup_file(candidate, load_index=True)
        if target_metadata.file_number not in files:
            raise MrimgxError(f"Target backup file {target_path} was not found in its own backup set.")
        return cls(files=files, target_file_number=target_metadata.file_number)

    def close(self) -> None:
        for handle in self._file_handles.values():
            handle.close()
        self._file_handles.clear()

    def __enter__(self) -> "BackupSet":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def target_layout(self) -> BackupFileLayout:
        return self.files[self.target_file_number]

    def open_file(self, file_number: int):
        if file_number not in self._file_handles:
            path = self._file_number_to_path[file_number]
            self._file_handles[file_number] = path.open("rb")
        return self._file_handles[file_number]

    def parent_file_number(self, file_number: int) -> int | None:
        layout = self.files[file_number]
        if layout.backup_type == "full":
            return None
        if layout.backup_type == "diff":
            full_candidates = [number for number in self.ordered_file_numbers if number < file_number and self.files[number].backup_type == "full"]
            return full_candidates[-1] if full_candidates else None
        candidates = [number for number in self.ordered_file_numbers if number < file_number]
        return candidates[-1] if candidates else None

    def build_snapshot(self, file_number: int) -> ResolvedSnapshot:
        scope = [self.files[number] for number in self.ordered_file_numbers if number <= file_number]
        target = scope[-1]
        partitions: dict[PartitionKey, SnapshotPartition] = {}
        for key, partition in target.partitions.items():
            if not target.delta_index:
                resolved = list(partition.data_blocks)
            else:
                base_index = None
                for index, layout in enumerate(scope):
                    if not layout.delta_index and not layout.split_file:
                        base_index = index
                if base_index is None:
                    raise MrimgxError(f"Could not find a full index before file number {file_number}.")
                base_partition = scope[base_index].partitions[key]
                resolved = list(base_partition.data_blocks)
                for layout in scope[base_index + 1 :]:
                    if not layout.delta_index:
                        continue
                    delta_partition = layout.partitions[key]
                    for delta_block in delta_partition.delta_data_blocks:
                        if delta_block.block_index >= len(resolved):
                            resolved.extend(
                                [
                                    DataBlockRef(
                                        file_position=0,
                                        md5_hash=b"\x00" * 16,
                                        compressed_length=0,
                                        file_number=0,
                                    )
                                ]
                                * (delta_block.block_index + 1 - len(resolved))
                            )
                        resolved[delta_block.block_index] = delta_block.block
            partitions[key] = SnapshotPartition(source=partition.snapshot_view(), resolved_data_blocks=resolved)
        return ResolvedSnapshot(file_number=file_number, backup_type=target.backup_type, partitions=partitions)

    def changed_block_indexes(self, file_number: int, partition_key: PartitionKey) -> list[int]:
        layout = self.files[file_number]
        partition = layout.partitions[partition_key]
        if layout.delta_index:
            return sorted(delta_block.block_index for delta_block in partition.delta_data_blocks if delta_block.block.compressed_length > 0)
        return [
            index
            for index, block in enumerate(partition.data_blocks)
            if block.file_number == file_number and block.compressed_length > 0
        ]


class SnapshotPartitionReader:
    def __init__(
        self,
        backup_set: BackupSet,
        snapshot: ResolvedSnapshot,
        partition_key: PartitionKey,
        *,
        max_cached_blocks: int = 8,
    ):
        self.backup_set = backup_set
        self.snapshot = snapshot
        self.partition = snapshot.partitions[partition_key]
        self.max_cached_blocks = max_cached_blocks
        self._block_cache: OrderedDict[int, bytes] = OrderedDict()

    def clear_cache(self) -> None:
        self._block_cache.clear()

    def _remember_block(self, block_index: int, block_bytes: bytes) -> bytes:
        if self.max_cached_blocks <= 0:
            return block_bytes
        self._block_cache[block_index] = block_bytes
        self._block_cache.move_to_end(block_index)
        while len(self._block_cache) > self.max_cached_blocks:
            self._block_cache.popitem(last=False)
        return block_bytes

    def read_block(self, block_index: int) -> bytes:
        cached = self._block_cache.get(block_index)
        if cached is not None:
            self._block_cache.move_to_end(block_index)
            return cached

        if block_index >= len(self.partition.resolved_data_blocks):
            block_bytes = bytes(self.partition.source.block_size)
            return self._remember_block(block_index, block_bytes)

        block = self.partition.resolved_data_blocks[block_index]
        if block.compressed_length == 0:
            block_bytes = bytes(self.partition.source.block_size)
            return self._remember_block(block_index, block_bytes)

        file_handle = self.backup_set.open_file(block.file_number)
        file_handle.seek(block.file_position)
        raw = file_handle.read(block.compressed_length)
        if len(raw) != block.compressed_length:
            raise MrimgxError("Unexpected end of file while reading a data block.")

        compression_level = self.backup_set.files[block.file_number].compression_level
        if compression_level != "none":
            block_bytes = _ZSTD.decompress(raw)
        else:
            block_bytes = raw

        actual_md5 = hashlib.md5(block_bytes, usedforsecurity=False).digest()
        if actual_md5 != block.md5_hash:
            raise MrimgxError("Data block hash mismatch.")

        return self._remember_block(block_index, block_bytes)

    def read_disk_bytes(self, absolute_disk_offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        source = self.partition.source
        if absolute_disk_offset < source.cluster0_disk_offset:
            raise MrimgxError("Requested read before the first imaged data block.")

        buffer = bytearray()
        position = absolute_disk_offset
        remaining = length
        while remaining > 0:
            relative = position - source.cluster0_disk_offset
            block_index = relative // source.block_size
            offset_in_block = relative % source.block_size
            block_bytes = self.read_block(block_index)
            take = min(remaining, len(block_bytes) - offset_in_block)
            if take <= 0:
                break
            buffer.extend(block_bytes[offset_in_block : offset_in_block + take])
            position += take
            remaining -= take
        return bytes(buffer)
