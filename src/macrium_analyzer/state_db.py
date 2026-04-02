from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import AnalyzedImage, AttributionBucket


@dataclass(frozen=True)
class BucketDescriptor:
    entry_kind: str
    normalized_path: str | None
    parent_path: str | None
    name: str


def is_synthetic_bucket(key: str) -> bool:
    return (
        not key.startswith(".\\")
        or key.startswith(".\\$")
        or key.startswith("NTFS metadata:")
        or key.startswith("Partition bucket:")
        or key in {"Deleted or previous-owner bytes", "Unresolved NTFS bytes"}
    )


def describe_bucket(key: str) -> BucketDescriptor:
    if is_synthetic_bucket(key):
        return BucketDescriptor(
            entry_kind="synthetic",
            normalized_path=None,
            parent_path=None,
            name=key,
        )

    normalized = key
    is_directory_bucket = False
    if normalized.endswith(":$I30"):
        normalized = normalized[: -len(":$I30")]
        is_directory_bucket = True
    elif ":" in normalized:
        normalized = normalized.split(":", 1)[0]

    if not normalized.startswith(".\\"):
        return BucketDescriptor(
            entry_kind="synthetic",
            normalized_path=None,
            parent_path=None,
            name=key,
        )

    parent_path, _, name = normalized.rpartition("\\")
    if not parent_path:
        parent_path = ".\\"
    return BucketDescriptor(
        entry_kind=("directory_bucket" if is_directory_bucket else "file"),
        normalized_path=normalized,
        parent_path=parent_path,
        name=(name or normalized),
    )


def ancestor_directory_paths(normalized_path: str, entry_kind: str) -> list[str]:
    if not normalized_path.startswith(".\\"):
        return []

    relative = normalized_path[2:]
    components = [part for part in relative.split("\\") if part]
    if entry_kind == "directory_bucket":
        directory_count = len(components)
    else:
        directory_count = max(0, len(components) - 1)

    paths = [".\\"]
    current = ".\\"
    for component in components[:directory_count]:
        current = f".\\{component}" if current == ".\\" else f"{current}\\{component}"
        paths.append(current)
    return paths


def directory_name_and_parent(path: str) -> tuple[str, str | None]:
    if path == ".\\":
        return ".", None
    parent_path, _, name = path.rpartition("\\")
    if parent_path == ".":
        parent_path = ".\\"
    if not parent_path:
        parent_path = ".\\"
    return name or path, parent_path


def _parse_image_numbers(value: str | None) -> list[int]:
    if not value:
        return []
    return [int(part) for part in value.split(",") if part]


class AggregateState:
    def __init__(self, path: Path | None, connection: sqlite3.Connection, *, db_label: str) -> None:
        self.path = path
        self.db_label = db_label
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    @classmethod
    def create(
        cls,
        path: Path | None,
        *,
        target_file: Path,
        target_file_number: int,
        target_backup_type: str,
        parent_file_number: int | None,
        requested_image_count: int,
        in_memory: bool = False,
    ) -> "AggregateState":
        if in_memory:
            connection = sqlite3.connect(":memory:")
            state = cls(None, connection, db_label=":memory:")
        else:
            if path is None:
                raise ValueError("A state database path is required when not using in-memory mode.")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)
            for suffix in (".shm", ".wal", ".journal"):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists():
                    sidecar.unlink()
            connection = sqlite3.connect(path)
            state = cls(path, connection, db_label=str(path))
        state._configure_connection()
        state._create_schema()
        state.connection.execute(
            """
            INSERT INTO run_metadata (
                id,
                format_version,
                target_file,
                target_file_number,
                target_backup_type,
                parent_file_number,
                requested_image_count
            )
            VALUES (1, 2, ?, ?, ?, ?, ?)
            """,
            (
                str(target_file),
                target_file_number,
                target_backup_type,
                parent_file_number,
                requested_image_count,
            ),
        )
        state.connection.commit()
        return state

    @classmethod
    def open(cls, path: Path) -> "AggregateState":
        connection = sqlite3.connect(path)
        state = cls(path, connection, db_label=str(path))
        state._configure_connection()
        return state

    def close(self) -> None:
        self.connection.close()

    def _configure_connection(self) -> None:
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = DELETE")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.connection.execute("PRAGMA temp_store = MEMORY")

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE run_metadata (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                format_version INTEGER NOT NULL,
                target_file TEXT NOT NULL,
                target_file_number INTEGER NOT NULL,
                target_backup_type TEXT NOT NULL,
                parent_file_number INTEGER,
                requested_image_count INTEGER NOT NULL,
                analyzed_image_count INTEGER NOT NULL DEFAULT 0,
                total_stored_bytes INTEGER NOT NULL DEFAULT 0,
                total_changed_bytes INTEGER NOT NULL DEFAULT 0,
                bucket_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE analyzed_images (
                image_index INTEGER PRIMARY KEY,
                file_path TEXT NOT NULL,
                file_number INTEGER NOT NULL,
                backup_type TEXT NOT NULL,
                parent_file_number INTEGER,
                total_stored_bytes INTEGER NOT NULL,
                total_changed_bytes INTEGER NOT NULL,
                changed_block_count INTEGER NOT NULL,
                bucket_count INTEGER NOT NULL
            );

            CREATE TABLE notes (
                note TEXT PRIMARY KEY
            );

            CREATE TABLE entries (
                entry_key TEXT PRIMARY KEY,
                entry_kind TEXT NOT NULL,
                normalized_path TEXT,
                parent_path TEXT,
                name TEXT NOT NULL,
                stored_bytes REAL NOT NULL DEFAULT 0,
                changed_bytes INTEGER NOT NULL DEFAULT 0,
                changed_ranges INTEGER NOT NULL DEFAULT 0,
                blocks INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE entry_images (
                entry_key TEXT NOT NULL,
                file_number INTEGER NOT NULL,
                PRIMARY KEY (entry_key, file_number),
                FOREIGN KEY (entry_key) REFERENCES entries(entry_key) ON DELETE CASCADE
            );

            CREATE TABLE directory_nodes (
                path TEXT PRIMARY KEY,
                parent_path TEXT,
                name TEXT NOT NULL,
                stored_bytes REAL NOT NULL DEFAULT 0,
                changed_bytes INTEGER NOT NULL DEFAULT 0,
                changed_ranges INTEGER NOT NULL DEFAULT 0,
                blocks INTEGER NOT NULL DEFAULT 0,
                child_count INTEGER NOT NULL DEFAULT 0,
                image_occurrences INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE directory_images (
                path TEXT NOT NULL,
                file_number INTEGER NOT NULL,
                PRIMARY KEY (path, file_number),
                FOREIGN KEY (path) REFERENCES directory_nodes(path) ON DELETE CASCADE
            );

            CREATE INDEX idx_entries_kind_parent ON entries(entry_kind, parent_path);
            CREATE INDEX idx_entries_stored_bytes ON entries(stored_bytes DESC);
            CREATE INDEX idx_directory_nodes_parent ON directory_nodes(parent_path);
            CREATE INDEX idx_directory_nodes_stored_bytes ON directory_nodes(stored_bytes DESC);
            """
        )

    def record_note(self, note: str) -> None:
        self.connection.execute("INSERT OR IGNORE INTO notes (note) VALUES (?)", (note,))

    def add_bucket_batch(self, file_number: int, buckets: dict[str, AttributionBucket]) -> None:
        if not buckets:
            return

        entry_rows: list[tuple[Any, ...]] = []
        image_rows: list[tuple[str, int]] = []
        for key, bucket in buckets.items():
            descriptor = describe_bucket(key)
            entry_rows.append(
                (
                    key,
                    descriptor.entry_kind,
                    descriptor.normalized_path,
                    descriptor.parent_path,
                    descriptor.name,
                    float(bucket.stored_bytes),
                    int(bucket.changed_bytes),
                    int(bucket.changed_ranges),
                    int(bucket.blocks),
                )
            )
            image_rows.append((key, file_number))

        self.connection.executemany(
            """
            INSERT INTO entries (
                entry_key,
                entry_kind,
                normalized_path,
                parent_path,
                name,
                stored_bytes,
                changed_bytes,
                changed_ranges,
                blocks
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_key) DO UPDATE SET
                stored_bytes = stored_bytes + excluded.stored_bytes,
                changed_bytes = changed_bytes + excluded.changed_bytes,
                changed_ranges = changed_ranges + excluded.changed_ranges,
                blocks = blocks + excluded.blocks
            """,
            entry_rows,
        )
        self.connection.executemany(
            "INSERT OR IGNORE INTO entry_images (entry_key, file_number) VALUES (?, ?)",
            image_rows,
        )
        self.connection.commit()

    def add_analyzed_image(self, image_index: int, image: AnalyzedImage) -> None:
        self.connection.execute(
            """
            INSERT INTO analyzed_images (
                image_index,
                file_path,
                file_number,
                backup_type,
                parent_file_number,
                total_stored_bytes,
                total_changed_bytes,
                changed_block_count,
                bucket_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                image_index,
                str(image.file_path),
                image.file_number,
                image.backup_type,
                image.parent_file_number,
                image.total_stored_bytes,
                image.total_changed_bytes,
                image.changed_block_count,
                image.bucket_count,
            ),
        )
        self.connection.execute(
            """
            UPDATE run_metadata
            SET analyzed_image_count = analyzed_image_count + 1,
                total_stored_bytes = total_stored_bytes + ?,
                total_changed_bytes = total_changed_bytes + ?
            WHERE id = 1
            """,
            (image.total_stored_bytes, image.total_changed_bytes),
        )
        self.connection.commit()

    def finalize_run(self) -> None:
        self.connection.execute(
            """
            UPDATE run_metadata
            SET bucket_count = (SELECT COUNT(*) FROM entries)
            WHERE id = 1
            """
        )
        self.connection.commit()

    def finalize_directory_rollups(self) -> None:
        self.connection.execute("DELETE FROM directory_images")
        self.connection.execute("DELETE FROM directory_nodes")
        self.connection.execute(
            """
            INSERT INTO directory_nodes (path, parent_path, name)
            VALUES ('.\\', NULL, '.')
            """
        )

        total_entries = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM entries WHERE entry_kind IN ('file', 'directory_bucket')"
            ).fetchone()[0]
        )
        pending_nodes: dict[str, dict[str, Any]] = {}
        batch_size = 2048
        cursor = self.connection.execute(
            """
            SELECT entry_kind, normalized_path, stored_bytes, changed_bytes, changed_ranges, blocks
            FROM entries
            WHERE entry_kind IN ('file', 'directory_bucket')
            """
        )
        processed = 0
        for row in cursor:
            processed += 1
            normalized_path = row["normalized_path"]
            entry_kind = row["entry_kind"]
            if not normalized_path:
                continue
            for directory_path in ancestor_directory_paths(normalized_path, entry_kind):
                name, parent_path = directory_name_and_parent(directory_path)
                metrics = pending_nodes.setdefault(
                    directory_path,
                    {
                        "path": directory_path,
                        "parent_path": parent_path,
                        "name": name,
                        "stored_bytes": 0.0,
                        "changed_bytes": 0,
                        "changed_ranges": 0,
                        "blocks": 0,
                    },
                )
                metrics["stored_bytes"] += float(row["stored_bytes"])
                metrics["changed_bytes"] += int(row["changed_bytes"])
                metrics["changed_ranges"] += int(row["changed_ranges"])
                metrics["blocks"] += int(row["blocks"])

            if len(pending_nodes) >= batch_size:
                self._flush_directory_nodes(pending_nodes)
                pending_nodes.clear()
        if pending_nodes:
            self._flush_directory_nodes(pending_nodes)

        total_image_rows = int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM entry_images ei
                JOIN entries e ON e.entry_key = ei.entry_key
                WHERE e.entry_kind IN ('file', 'directory_bucket')
                """
            ).fetchone()[0]
        )
        pending_directory_images: set[tuple[str, int]] = set()
        cursor = self.connection.execute(
            """
            SELECT e.entry_kind, e.normalized_path, ei.file_number
            FROM entry_images ei
            JOIN entries e ON e.entry_key = ei.entry_key
            WHERE e.entry_kind IN ('file', 'directory_bucket')
            """
        )
        processed_images = 0
        for row in cursor:
            processed_images += 1
            normalized_path = row["normalized_path"]
            entry_kind = row["entry_kind"]
            file_number = int(row["file_number"])
            if not normalized_path:
                continue
            for directory_path in ancestor_directory_paths(normalized_path, entry_kind):
                pending_directory_images.add((directory_path, file_number))
            if len(pending_directory_images) >= batch_size:
                self._flush_directory_images(pending_directory_images)
                pending_directory_images.clear()
        if pending_directory_images:
            self._flush_directory_images(pending_directory_images)

        self.connection.execute(
            """
            UPDATE directory_nodes
            SET child_count = (
                SELECT COUNT(*)
                FROM directory_nodes child
                WHERE child.parent_path = directory_nodes.path
            )
            """
        )
        self.connection.execute(
            """
            UPDATE directory_nodes
            SET image_occurrences = (
                SELECT COUNT(*)
                FROM directory_images di
                WHERE di.path = directory_nodes.path
            )
            """
        )
        self.connection.commit()

    def _flush_directory_nodes(self, pending_nodes: dict[str, dict[str, Any]]) -> None:
        rows = [
            (
                metrics["path"],
                metrics["parent_path"],
                metrics["name"],
                metrics["stored_bytes"],
                metrics["changed_bytes"],
                metrics["changed_ranges"],
                metrics["blocks"],
            )
            for metrics in pending_nodes.values()
        ]
        self.connection.executemany(
            """
            INSERT INTO directory_nodes (
                path,
                parent_path,
                name,
                stored_bytes,
                changed_bytes,
                changed_ranges,
                blocks
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                stored_bytes = stored_bytes + excluded.stored_bytes,
                changed_bytes = changed_bytes + excluded.changed_bytes,
                changed_ranges = changed_ranges + excluded.changed_ranges,
                blocks = blocks + excluded.blocks
            """,
            rows,
        )
        self.connection.commit()

    def _flush_directory_images(self, pending_directory_images: Iterable[tuple[str, int]]) -> None:
        self.connection.executemany(
            "INSERT OR IGNORE INTO directory_images (path, file_number) VALUES (?, ?)",
            list(pending_directory_images),
        )
        self.connection.commit()

    def load_run_summary(self) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM run_metadata WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("Run metadata is missing from the state database.")
        return dict(row)

    def load_notes(self) -> list[str]:
        return [str(row["note"]) for row in self.connection.execute("SELECT note FROM notes ORDER BY note")]

    def load_analyzed_images(self) -> list[AnalyzedImage]:
        rows = self.connection.execute(
            """
            SELECT
                file_path,
                file_number,
                backup_type,
                parent_file_number,
                total_stored_bytes,
                total_changed_bytes,
                changed_block_count,
                bucket_count
            FROM analyzed_images
            ORDER BY image_index
            """
        ).fetchall()
        return [
            AnalyzedImage(
                file_path=Path(str(row["file_path"])),
                file_number=int(row["file_number"]),
                backup_type=str(row["backup_type"]),
                parent_file_number=(None if row["parent_file_number"] is None else int(row["parent_file_number"])),
                total_stored_bytes=int(row["total_stored_bytes"]),
                total_changed_bytes=int(row["total_changed_bytes"]),
                changed_block_count=int(row["changed_block_count"]),
                bucket_count=int(row["bucket_count"]),
            )
            for row in rows
        ]

    def load_directory_node(self, path: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            self._directory_select(where_clause="WHERE d.path = ?", order_clause=""),
            (path,),
        ).fetchone()
        return None if row is None else self._directory_row_to_dict(row)

    def load_directory_children(self, parent_path: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        sql = self._directory_select(
            where_clause="WHERE d.parent_path = ?",
            order_clause="ORDER BY d.stored_bytes DESC, d.image_occurrences DESC, lower(d.path)",
            limit=limit,
        )
        rows = self.connection.execute(sql, ((parent_path,) if limit is None else (parent_path, limit))).fetchall()
        return [self._directory_row_to_dict(row) for row in rows]

    def load_top_directories(self, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            self._directory_select(
                where_clause="WHERE d.path <> ?",
                order_clause="ORDER BY d.stored_bytes DESC, d.image_occurrences DESC, lower(d.path)",
                limit=limit,
            ),
            (".\\", limit),
        ).fetchall()
        return [self._directory_row_to_dict(row) for row in rows]

    def load_top_leaf_directories(self, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            self._directory_select(
                where_clause="WHERE d.path <> ? AND d.child_count = 0",
                order_clause="ORDER BY d.stored_bytes DESC, d.image_occurrences DESC, lower(d.path)",
                limit=limit,
            ),
            (".\\", limit),
        ).fetchall()
        return [self._directory_row_to_dict(row) for row in rows]

    def load_flat_buckets(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        sql = self._entry_select(
            where_clause="",
            order_clause="ORDER BY e.stored_bytes DESC, image_occurrences DESC, lower(e.entry_key)",
            limit=limit,
        )
        params: tuple[Any, ...] = () if limit is None else (limit,)
        rows = self.connection.execute(sql, params).fetchall()
        return [self._entry_row_to_dict(row) for row in rows]

    def load_file_entries(self, parent_path: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            self._entry_select(
                where_clause="WHERE e.entry_kind = 'file' AND e.parent_path = ?",
                order_clause="ORDER BY e.stored_bytes DESC, image_occurrences DESC, lower(e.normalized_path)",
            ),
            (parent_path,),
        ).fetchall()
        return [self._entry_row_to_dict(row) for row in rows]

    def load_all_file_entries(self) -> list[dict[str, Any]]:
        return list(self.iter_all_file_entries())

    def load_special_entries(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            self._entry_select(
                where_clause="WHERE e.entry_kind = 'synthetic'",
                order_clause="ORDER BY e.stored_bytes DESC, image_occurrences DESC, lower(e.entry_key)",
            )
        ).fetchall()
        return [self._entry_row_to_dict(row) for row in rows]

    def load_all_directories(self) -> list[dict[str, Any]]:
        return list(self.iter_all_directories())

    def iter_all_file_entries(self) -> Iterable[dict[str, Any]]:
        cursor = self.connection.execute(
            self._entry_select(
                where_clause="WHERE e.entry_kind = 'file'",
                order_clause="ORDER BY lower(e.normalized_path)",
            )
        )
        for row in cursor:
            yield self._entry_row_to_dict(row)

    def iter_all_directories(self) -> Iterable[dict[str, Any]]:
        cursor = self.connection.execute(
            self._directory_select(
                where_clause="",
                order_clause="ORDER BY CASE WHEN d.path = '.\\' THEN 0 ELSE 1 END, lower(d.path)",
            )
        )
        for row in cursor:
            yield self._directory_row_to_dict(row)

    def _directory_select(self, *, where_clause: str, order_clause: str, limit: int | None = None) -> str:
        limit_clause = "" if limit is None else " LIMIT ?"
        return f"""
            SELECT
                d.path,
                d.parent_path,
                d.name,
                d.stored_bytes,
                d.changed_bytes,
                d.changed_ranges,
                d.blocks,
                d.child_count,
                d.image_occurrences,
                (
                    SELECT group_concat(file_number, ',')
                    FROM (
                        SELECT file_number
                        FROM directory_images di
                        WHERE di.path = d.path
                        ORDER BY file_number
                    )
                ) AS image_file_numbers
            FROM directory_nodes d
            {where_clause}
            {order_clause}
            {limit_clause}
        """

    def _entry_select(self, *, where_clause: str, order_clause: str, limit: int | None = None) -> str:
        limit_clause = "" if limit is None else " LIMIT ?"
        return f"""
            SELECT
                e.entry_key,
                e.entry_kind,
                e.normalized_path,
                e.parent_path,
                e.name,
                e.stored_bytes,
                e.changed_bytes,
                e.changed_ranges,
                e.blocks,
                (
                    SELECT COUNT(*)
                    FROM entry_images ei
                    WHERE ei.entry_key = e.entry_key
                ) AS image_occurrences,
                (
                    SELECT group_concat(file_number, ',')
                    FROM (
                        SELECT file_number
                        FROM entry_images ei
                        WHERE ei.entry_key = e.entry_key
                        ORDER BY file_number
                    )
                ) AS image_file_numbers
            FROM entries e
            {where_clause}
            {order_clause}
            {limit_clause}
        """

    def _directory_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "name": str(row["name"]),
            "path": str(row["path"]),
            "parent_path": row["parent_path"],
            "kind": "directory",
            "stored_bytes": float(row["stored_bytes"]),
            "changed_bytes": int(row["changed_bytes"]),
            "changed_ranges": int(row["changed_ranges"]),
            "blocks": int(row["blocks"]),
            "child_count": int(row["child_count"]),
            "image_occurrences": int(row["image_occurrences"]),
            "image_file_numbers": _parse_image_numbers(row["image_file_numbers"]),
        }

    def _entry_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        entry_kind = str(row["entry_kind"])
        return {
            "key": str(row["entry_key"]),
            "bucket_key": str(row["entry_key"]),
            "kind": ("special" if entry_kind == "synthetic" else entry_kind),
            "entry_kind": entry_kind,
            "name": str(row["name"]),
            "path": row["normalized_path"],
            "parent_path": row["parent_path"],
            "stored_bytes": float(row["stored_bytes"]),
            "changed_bytes": int(row["changed_bytes"]),
            "changed_ranges": int(row["changed_ranges"]),
            "blocks": int(row["blocks"]),
            "image_occurrences": int(row["image_occurrences"]),
            "image_file_numbers": _parse_image_numbers(row["image_file_numbers"]),
        }
