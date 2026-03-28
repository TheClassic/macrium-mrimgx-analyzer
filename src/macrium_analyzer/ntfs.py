from __future__ import annotations

import bisect
import struct
import sys
from dataclasses import dataclass

from .models import Extent, NtfsRecord
from .mrimgx import SnapshotPartitionReader


ATTRIBUTE_END = 0xFFFFFFFF
ATTRIBUTE_FILE_NAME = 0x30
ATTRIBUTE_DATA = 0x80
ATTRIBUTE_INDEX_ALLOCATION = 0xA0


@dataclass
class ParsedAttribute:
    attribute_type: int
    name: str
    resident: bool
    content: bytes | None
    runlist: list[tuple[int | None, int]]
    real_size: int


class NtfsMapper:
    def __init__(self, reader: SnapshotPartitionReader):
        self.reader = reader
        self.partition = reader.partition.source
        self.cluster_size = self.partition.cluster_size
        self.record_size = self.partition.mft_record_size
        self.bytes_per_sector = self.partition.bytes_per_sector
        if not self.partition.is_ntfs:
            raise ValueError("NtfsMapper requires an NTFS partition.")
        self.records: dict[int, NtfsRecord] = {}
        self.extents: list[Extent] = []
        self._extent_starts: list[int] = []

    def build(self) -> "NtfsMapper":
        record_zero = self._read_record_at_disk_offset(self.partition.mft_offset)
        mft_runs, mft_size = self._extract_mft_runs(record_zero)
        record_count = max(1, mft_size // self.record_size)
        for record_number in range(record_count):
            raw = self._read_from_runs(mft_runs, record_number * self.record_size, self.record_size)
            if len(raw) != self.record_size:
                continue
            parsed = self._parse_record(raw)
            if parsed is None:
                continue
            owner_record = parsed["base_record"] or record_number
            record = self.records.setdefault(owner_record, NtfsRecord(record_number=owner_record))
            if parsed["base_record"] == 0:
                record.in_use = parsed["in_use"]
                record.is_directory = parsed["is_directory"]
                if parsed["file_name"] is not None:
                    record.display_name = parsed["file_name"][0]
                    record.parent_record_number = parsed["file_name"][1]
            for attribute in parsed["attributes"]:
                if attribute.attribute_type in (ATTRIBUTE_DATA, ATTRIBUTE_INDEX_ALLOCATION):
                    record.data_attributes.append((attribute.name, attribute.runlist, attribute.real_size, attribute.attribute_type))

        self._build_extent_index()
        self.records.clear()
        return self

    def find_overlaps(self, start: int, end: int) -> list[Extent]:
        if end <= start:
            return []
        start_index = max(0, bisect.bisect_right(self._extent_starts, start) - 1)
        matches: list[Extent] = []
        for extent in self.extents[start_index:]:
            if extent.start >= end:
                break
            if extent.end > start:
                matches.append(extent)
        return matches

    def _build_extent_index(self) -> None:
        extents: list[Extent] = []
        for record in self.records.values():
            path = self._resolve_path(record.record_number)
            for stream_name, runlist, _real_size, attribute_type in record.data_attributes:
                stream_suffix = ""
                if attribute_type == ATTRIBUTE_INDEX_ALLOCATION:
                    stream_suffix = ":$I30"
                elif stream_name:
                    stream_suffix = f":{stream_name}"
                owner = sys.intern(f"{path}{stream_suffix}")
                for lcn, cluster_count in runlist:
                    if lcn is None or cluster_count <= 0:
                        continue
                    start = self.partition.cluster0_disk_offset + (lcn * self.cluster_size)
                    end = start + (cluster_count * self.cluster_size)
                    extents.append(Extent(start=start, end=end, owner=owner))
        extents.sort(key=lambda extent: extent.start)
        merged: list[Extent] = []
        for extent in extents:
            if merged and merged[-1].owner == extent.owner and merged[-1].end >= extent.start:
                merged[-1].end = max(merged[-1].end, extent.end)
                continue
            merged.append(extent)
        self.extents = merged
        self._extent_starts = [extent.start for extent in merged]

    def _resolve_path(self, record_number: int) -> str:
        seen: set[int] = set()
        parts: list[str] = []
        current = record_number
        while True:
            if current in seen:
                parts.insert(0, "<cycle>")
                break
            seen.add(current)
            record = self.records.get(current)
            if record is None or record.display_name is None:
                parts.insert(0, f"<record-{current}>")
                break
            if current == 5:
                break
            parts.insert(0, record.display_name)
            if record.parent_record_number is None or record.parent_record_number == current:
                break
            current = record.parent_record_number
        if not parts:
            return ".\\"
        return ".\\" + "\\".join(parts)

    def _extract_mft_runs(self, record_bytes: bytes) -> tuple[list[tuple[int | None, int]], int]:
        parsed = self._parse_record(record_bytes)
        if parsed is None:
            raise RuntimeError("Could not parse the $MFT record.")
        for attribute in parsed["attributes"]:
            if attribute.attribute_type == ATTRIBUTE_DATA and attribute.name == "":
                return attribute.runlist, attribute.real_size
        raise RuntimeError("Could not find the $MFT data attribute.")

    def _read_record_at_disk_offset(self, disk_offset: int) -> bytes:
        raw = self.reader.read_disk_bytes(disk_offset, self.record_size)
        if len(raw) != self.record_size:
            raise RuntimeError("Unexpected end of MFT data.")
        return raw

    def _read_from_runs(self, runlist: list[tuple[int | None, int]], offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        remaining = length
        virtual_offset = offset
        output = bytearray()
        file_cursor = 0
        for lcn, cluster_count in runlist:
            run_length_bytes = cluster_count * self.cluster_size
            run_start = file_cursor
            run_end = file_cursor + run_length_bytes
            if virtual_offset >= run_end:
                file_cursor = run_end
                continue
            start_in_run = max(0, virtual_offset - run_start)
            available = run_end - (run_start + start_in_run)
            take = min(remaining, available)
            if take <= 0:
                file_cursor = run_end
                continue
            if lcn is None:
                output.extend(b"\x00" * take)
            else:
                disk_offset = self.partition.cluster0_disk_offset + (lcn * self.cluster_size) + start_in_run
                output.extend(self.reader.read_disk_bytes(disk_offset, take))
            virtual_offset += take
            remaining -= take
            file_cursor = run_end
            if remaining == 0:
                break
        return bytes(output)

    def _parse_record(self, record_bytes: bytes):
        fixed = self._apply_fixup(record_bytes)
        if fixed[:4] != b"FILE":
            return None
        flags = struct.unpack_from("<H", fixed, 0x16)[0]
        if not (flags & 0x01):
            return None
        first_attr_offset = struct.unpack_from("<H", fixed, 0x14)[0]
        base_record_ref = struct.unpack_from("<Q", fixed, 0x20)[0]
        base_record = base_record_ref & 0xFFFFFFFFFFFF
        is_directory = bool(flags & 0x02)
        offset = first_attr_offset
        file_name: tuple[str, int] | None = None
        attributes: list[ParsedAttribute] = []
        while offset + 8 <= len(fixed):
            attribute_type, length = struct.unpack_from("<II", fixed, offset)
            if attribute_type == ATTRIBUTE_END or length <= 0:
                break
            non_resident = fixed[offset + 8] != 0
            name_length = fixed[offset + 9]
            name_offset = struct.unpack_from("<H", fixed, offset + 10)[0]
            attribute_name = ""
            if name_length:
                raw_name = fixed[offset + name_offset : offset + name_offset + (name_length * 2)]
                attribute_name = raw_name.decode("utf-16le", errors="replace")
            if non_resident:
                run_offset = struct.unpack_from("<H", fixed, offset + 32)[0]
                real_size = struct.unpack_from("<Q", fixed, offset + 48)[0]
                runlist_bytes = fixed[offset + run_offset : offset + length]
                attributes.append(
                    ParsedAttribute(
                        attribute_type=attribute_type,
                        name=attribute_name,
                        resident=False,
                        content=None,
                        runlist=self._parse_runlist(runlist_bytes),
                        real_size=real_size,
                    )
                )
            else:
                value_length = struct.unpack_from("<I", fixed, offset + 16)[0]
                value_offset = struct.unpack_from("<H", fixed, offset + 20)[0]
                content = fixed[offset + value_offset : offset + value_offset + value_length]
                if attribute_type == ATTRIBUTE_FILE_NAME and file_name is None:
                    candidate = self._parse_file_name(content)
                    if candidate is not None:
                        file_name = candidate
                attributes.append(
                    ParsedAttribute(
                        attribute_type=attribute_type,
                        name=attribute_name,
                        resident=True,
                        content=content,
                        runlist=[],
                        real_size=value_length,
                    )
                )
            offset += length
        return {
            "base_record": base_record,
            "in_use": True,
            "is_directory": is_directory,
            "file_name": file_name,
            "attributes": attributes,
        }

    def _parse_file_name(self, content: bytes) -> tuple[str, int] | None:
        if len(content) < 66:
            return None
        parent_ref = struct.unpack_from("<Q", content, 0)[0]
        parent_record_number = parent_ref & 0xFFFFFFFFFFFF
        name_length = content[64]
        raw_name = content[66 : 66 + (name_length * 2)]
        name = raw_name.decode("utf-16le", errors="replace")
        return name, int(parent_record_number)

    def _parse_runlist(self, payload: bytes) -> list[tuple[int | None, int]]:
        runs: list[tuple[int | None, int]] = []
        offset = 0
        current_lcn = 0
        while offset < len(payload):
            header = payload[offset]
            offset += 1
            if header == 0:
                break
            length_size = header & 0x0F
            offset_size = header >> 4
            cluster_count = int.from_bytes(payload[offset : offset + length_size], "little", signed=False)
            offset += length_size
            if offset_size == 0:
                runs.append((None, cluster_count))
                continue
            lcn_delta = int.from_bytes(payload[offset : offset + offset_size], "little", signed=True)
            offset += offset_size
            current_lcn += lcn_delta
            runs.append((current_lcn, cluster_count))
        return runs

    def _apply_fixup(self, record_bytes: bytes) -> bytes:
        data = bytearray(record_bytes)
        usa_offset, usa_count = struct.unpack_from("<HH", data, 4)
        if usa_offset + (usa_count * 2) > len(data):
            raise RuntimeError("Invalid NTFS update sequence array.")
        usa = data[usa_offset : usa_offset + (usa_count * 2)]
        update_sequence = usa[:2]
        for sector_index in range(1, usa_count):
            replacement = usa[sector_index * 2 : (sector_index + 1) * 2]
            end_offset = (sector_index * self.bytes_per_sector) - 2
            if data[end_offset : end_offset + 2] != update_sequence:
                raise RuntimeError("NTFS fixup validation failed.")
            data[end_offset : end_offset + 2] = replacement
        return bytes(data)
