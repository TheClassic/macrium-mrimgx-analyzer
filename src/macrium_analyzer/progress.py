from __future__ import annotations

import json
import os
import time
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_PSAPI = ctypes.WinDLL("psapi", use_last_error=True)
_GET_PROCESS_MEMORY_INFO = _PSAPI.GetProcessMemoryInfo
_GET_PROCESS_MEMORY_INFO.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_ProcessMemoryCounters),
    wintypes.DWORD,
]
_GET_PROCESS_MEMORY_INFO.restype = wintypes.BOOL


def _memory_snapshot() -> dict[str, int]:
    try:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        if not _GET_PROCESS_MEMORY_INFO(
            _KERNEL32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        ):
            return {}
        return {
            "process_working_set_bytes": int(counters.WorkingSetSize),
            "process_peak_working_set_bytes": int(counters.PeakWorkingSetSize),
            "process_pagefile_bytes": int(counters.PagefileUsage),
            "process_peak_pagefile_bytes": int(counters.PeakPagefileUsage),
        }
    except Exception:
        return {}


@dataclass
class ProgressTracker:
    path: Path | None = None
    log_path: Path | None = None
    interval_seconds: float = 1.0
    started_at: float = field(default_factory=time.time)
    _last_emit_at: float = 0.0
    _state: dict[str, Any] = field(default_factory=dict)

    def emit(self, *, force: bool = False, **updates: Any) -> None:
        if self.path is None and self.log_path is None:
            return

        now = time.time()
        self._state.update(updates)
        if not force and (now - self._last_emit_at) < self.interval_seconds:
            return

        elapsed_seconds = max(0.0, now - self.started_at)
        payload = {
            "started_at": self.started_at,
            "updated_at": now,
            "elapsed_seconds": elapsed_seconds,
            **self._state,
        }
        payload.update(_memory_snapshot())
        completed = payload.get("progress_completed")
        total = payload.get("progress_total")
        if isinstance(completed, int) and isinstance(total, int) and total > 0:
            payload["progress_percent"] = (completed / total) * 100.0
            if completed > 0 and elapsed_seconds > 0:
                rate = completed / elapsed_seconds
                if rate > 0:
                    payload["estimated_remaining_seconds"] = max(0.0, (total - completed) / rate)
        if self.path is not None:
            temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temp_path, self.path)
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(self._format_log_line(payload) + "\n")
        self._last_emit_at = now

    def begin(self, phase: str, message: str, **updates: Any) -> None:
        self.emit(force=True, status="running", phase=phase, message=message, **updates)

    def update(self, phase: str, message: str, **updates: Any) -> None:
        self.emit(phase=phase, message=message, **updates)

    def finish(self, **updates: Any) -> None:
        self.emit(force=True, status="completed", **updates)

    def fail(self, message: str, **updates: Any) -> None:
        self.emit(force=True, status="failed", message=message, **updates)

    def _format_log_line(self, payload: dict[str, Any]) -> str:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(payload["updated_at"]))
        parts = [
            timestamp,
            f"status={payload.get('status', 'unknown')}",
            f"phase={payload.get('phase', 'unknown')}",
        ]

        progress_completed = payload.get("progress_completed")
        progress_total = payload.get("progress_total")
        progress_percent = payload.get("progress_percent")
        if isinstance(progress_completed, int) and isinstance(progress_total, int):
            if isinstance(progress_percent, (int, float)):
                parts.append(f"progress={progress_completed}/{progress_total} ({progress_percent:.1f}%)")
            else:
                parts.append(f"progress={progress_completed}/{progress_total}")

        elapsed_seconds = payload.get("elapsed_seconds")
        if isinstance(elapsed_seconds, (int, float)):
            parts.append(f"elapsed={self._format_duration(float(elapsed_seconds))}")

        peak_working_set = payload.get("process_peak_working_set_bytes")
        if isinstance(peak_working_set, int) and peak_working_set > 0:
            parts.append(f"peak_ws={self._format_bytes(peak_working_set)}")

        eta_seconds = payload.get("estimated_remaining_seconds")
        if isinstance(eta_seconds, (int, float)):
            parts.append(f"eta={self._format_duration(float(eta_seconds))}")

        partition_index = payload.get("partition_index")
        partition_total = payload.get("partition_total")
        if isinstance(partition_index, int) and isinstance(partition_total, int):
            parts.append(f"partition={partition_index}/{partition_total}")

        image_index = payload.get("image_index")
        image_total = payload.get("image_total")
        image_file_number = payload.get("image_file_number")
        if isinstance(image_index, int) and isinstance(image_total, int):
            if isinstance(image_file_number, int):
                parts.append(f"image={image_index}/{image_total}#file{image_file_number}")
            else:
                parts.append(f"image={image_index}/{image_total}")

        changed_blocks_completed = payload.get("changed_blocks_completed")
        changed_block_total = payload.get("changed_block_total")
        if isinstance(changed_blocks_completed, int) and isinstance(changed_block_total, int):
            parts.append(f"partition_blocks={changed_blocks_completed}/{changed_block_total}")

        changed_block_index = payload.get("changed_block_index")
        if isinstance(changed_block_index, int):
            parts.append(f"block_index={changed_block_index}")

        message = payload.get("message")
        if isinstance(message, str) and message:
            parts.append(f"message={message}")

        error = payload.get("error")
        if isinstance(error, str) and error:
            parts.append(f"error={error}")

        return " | ".join(parts)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = int(round(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}h{minutes:02d}m{secs:02d}s"
        if minutes:
            return f"{minutes}m{secs:02d}s"
        return f"{secs}s"

    @staticmethod
    def _format_bytes(value: int) -> str:
        units = ["B", "KiB", "MiB", "GiB", "TiB"]
        size = float(value)
        for unit in units:
            if abs(size) < 1024.0 or unit == units[-1]:
                if unit == "B":
                    return f"{int(round(size))}{unit}"
                return f"{size:.1f}{unit}"
            size /= 1024.0
        return f"{size:.1f}PiB"
