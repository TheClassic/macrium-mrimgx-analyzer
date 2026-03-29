from __future__ import annotations

import unittest

from macrium_analyzer.models import NtfsRecord
from macrium_analyzer.ntfs import (
    FILE_NAME_NAMESPACE_DOS,
    FILE_NAME_NAMESPACE_POSIX,
    FILE_NAME_NAMESPACE_WIN32,
    FILE_NAME_NAMESPACE_WIN32_AND_DOS,
    NtfsMapper,
    ParsedFileName,
)


class NtfsMapperFileNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapper = NtfsMapper.__new__(NtfsMapper)

    def test_prefers_win32_name_over_dos_alias(self) -> None:
        selected = self.mapper._select_preferred_file_name(
            [
                ParsedFileName("PROGRA~1", 5, FILE_NAME_NAMESPACE_DOS),
                ParsedFileName("Program Files", 5, FILE_NAME_NAMESPACE_WIN32),
            ]
        )

        self.assertEqual(("Program Files", 5), selected)

    def test_prefers_win32_and_dos_name(self) -> None:
        selected = self.mapper._select_preferred_file_name(
            [
                ParsedFileName("Program Files", 5, FILE_NAME_NAMESPACE_WIN32_AND_DOS),
                ParsedFileName("PROGRA~1", 5, FILE_NAME_NAMESPACE_DOS),
            ]
        )

        self.assertEqual(("Program Files", 5), selected)

    def test_falls_back_to_posix_when_no_win32_name_exists(self) -> None:
        selected = self.mapper._select_preferred_file_name(
            [
                ParsedFileName("long-posix-name", 5, FILE_NAME_NAMESPACE_POSIX),
                ParsedFileName("LONGPO~1", 5, FILE_NAME_NAMESPACE_DOS),
            ]
        )

        self.assertEqual(("long-posix-name", 5), selected)

    def test_keeps_dos_name_when_it_is_only_choice(self) -> None:
        selected = self.mapper._select_preferred_file_name(
            [
                ParsedFileName("PROGRA~1", 5, FILE_NAME_NAMESPACE_DOS),
            ]
        )

        self.assertEqual(("PROGRA~1", 5), selected)

    def test_resolves_long_path_from_record_chain(self) -> None:
        self.mapper.records = {
            5: NtfsRecord(record_number=5, display_name="$ROOT", parent_record_number=5),
            10: NtfsRecord(record_number=10, display_name="Program Files", parent_record_number=5),
            11: NtfsRecord(record_number=11, display_name="Macrium", parent_record_number=10),
        }

        self.assertEqual(".\\Program Files\\Macrium", self.mapper._resolve_path(11))


if __name__ == "__main__":
    unittest.main()
