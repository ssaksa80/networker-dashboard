"""Server Protection is found among every job, and exports say "80 of N" (review C17).

The dashboard keeps the first TABLE_LIMIT (80) rows of each table. REST mode
looked for the Server Protection run in those rows only, so on a busy day a
run older than the newest 80 jobs was "Not found"; it also looked in the
failed-jobs rows, which carry no status, so a failed run there read as
"Unknown". NWUI mode looked in the table and the full list together, so it
counted each run twice. And an export of an 80-row table did not say there
were more.
"""
import io
import unittest
import zipfile
from xml.etree import ElementTree

from collector_fixtures import HOUR, NwuiServer, RestServer, config, iso, rest_job, server_protection_job

from nwdash import reports
from nwdash.config import TABLE_LIMIT

NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


# 2026-10-15 00:00 UTC.
MIDNIGHT = 1792022400
JOBS_ON_A_BUSY_DAY = TABLE_LIMIT + 11


def _busy_day(protection_status="Succeeded", newer_jobs=TABLE_LIMIT + 10):
    """A Server Protection run at 07:00 UTC, older than `newer_jobs` backups."""
    newer = [
        rest_job(20000 + n, f"client{n:03}.example.com", "Succeeded", start=iso(MIDNIGHT + 8 * HOUR + n * 60))
        for n in range(newer_jobs)
    ]
    return [*newer, server_protection_job(19999, protection_status, start=iso(MIDNIGHT + 7 * HOUR))]


def _the_day(api_mode="rest"):
    """A custom window holding _busy_day's jobs in any host time zone."""
    return config(api_mode, "custom", "12-10-2026", "15-10-2026")


def _sheet_rows(workbook, name):
    """The text of each row of one sheet of an exported workbook."""
    with zipfile.ZipFile(io.BytesIO(workbook)) as zf:
        names = [sheet.get("name") for sheet in ElementTree.fromstring(zf.read("xl/workbook.xml")).iter(f"{{{NS['x']}}}sheet")]
        sheet = ElementTree.fromstring(zf.read(f"xl/worksheets/sheet{names.index(name) + 1}.xml"))
    return [
        [cell.findtext("x:is/x:t", default="", namespaces=NS) or cell.findtext("x:v", default="", namespaces=NS)
         for cell in row.findall("x:c", NS)]
        for row in sheet.iter(f"{{{NS['x']}}}row")
    ]


class TestRestServerProtection(unittest.TestCase):
    def test_a_run_older_than_the_table_is_found(self):
        protection = RestServer(jobs=_busy_day()).build(_the_day())["serverProtectionJob"]
        self.assertEqual((protection["status"], protection["count"]), ("succeeded", 1))

    def test_a_failed_run_older_than_the_table_is_failed_not_unknown(self):
        protection = RestServer(jobs=_busy_day("Failed")).build(_the_day())["serverProtectionJob"]
        self.assertEqual(protection["status"], "failed")
        self.assertIn("nw.example.com", protection["detail"])

    def test_failed_rows_carry_their_status(self):
        rows = RestServer(jobs=_busy_day("Failed")).build(_the_day())["tables"]["failedJobs"]
        self.assertEqual([row["status"] for row in rows], ["Failed"])


class TestNwuiServerProtection(unittest.TestCase):
    def test_each_run_is_counted_once(self):
        second_run = server_protection_job(19998, "Succeeded", start=iso(MIDNIGHT + 6 * HOUR))
        body = NwuiServer(history=[*_busy_day(newer_jobs=3), second_run]).build(_the_day("nwui"))
        self.assertEqual(body["serverProtectionJob"]["count"], 2)

    def test_failed_rows_carry_their_status(self):
        rows = NwuiServer(history=_busy_day("Failed", newer_jobs=3)).build(_the_day("nwui"))["tables"]["failedJobs"]
        self.assertEqual([row["status"] for row in rows], ["failed"])


class TestTableTotals(unittest.TestCase):
    def test_the_dashboard_says_how_many_rows_each_table_had(self):
        body = RestServer(jobs=_busy_day()).build(_the_day())
        self.assertEqual(len(body["tables"]["jobs"]), TABLE_LIMIT)
        self.assertEqual(body["tableTotals"]["jobs"], JOBS_ON_A_BUSY_DAY)
        self.assertEqual(body["tableTotals"]["failedJobs"], 0)

    def test_nwui_mode_says_it_too(self):
        body = NwuiServer(history=_busy_day()).build(_the_day("nwui"))
        self.assertEqual(body["tableTotals"]["jobs"], JOBS_ON_A_BUSY_DAY)


class TestExport(unittest.TestCase):
    def setUp(self):
        self.workbook = reports.build_excel_report(RestServer(jobs=_busy_day("Failed")).build(_the_day()))

    def test_a_cut_table_says_80_of_n(self):
        rows = _sheet_rows(self.workbook, "Backup Jobs")
        self.assertEqual(rows[-1][0], f"Showing {TABLE_LIMIT} of {JOBS_ON_A_BUSY_DAY} rows.")
        self.assertEqual(len(rows), 1 + TABLE_LIMIT + 2)

    def test_a_whole_table_has_no_note(self):
        rows = _sheet_rows(self.workbook, "Failed Jobs")
        self.assertEqual(len(rows), 2)

    def test_the_failed_jobs_sheet_has_a_status_column(self):
        header, row = _sheet_rows(self.workbook, "Failed Jobs")
        self.assertEqual(row[header.index("Status")], "Failed")

    def test_the_dashboard_sheet_lists_the_totals(self):
        rows = {row[0]: row[1:] for row in _sheet_rows(self.workbook, "Dashboard") if row and row[0]}
        self.assertEqual(rows["Backup Jobs"], [f"{TABLE_LIMIT} of {JOBS_ON_A_BUSY_DAY}"])
        self.assertEqual(rows["Failed Jobs"], ["1 of 1"])

    def test_the_chart_rows_stay_where_the_chart_reads_them(self):
        rows = _sheet_rows(self.workbook, "Dashboard")
        self.assertEqual([row[0] for row in rows[11:18]],
                         ["Status", "Succeeded", "Failed", "Running / Queued", "Restores", "Clones", "Alerts"])

    def test_an_export_without_totals_is_unchanged(self):
        dashboard = RestServer(jobs=_busy_day()).build(_the_day())
        del dashboard["tableTotals"]
        rows = _sheet_rows(reports.build_excel_report(dashboard), "Backup Jobs")
        self.assertEqual(len(rows), 1 + TABLE_LIMIT)


if __name__ == "__main__":
    unittest.main()
