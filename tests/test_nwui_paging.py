"""NWUI monitoring lists are read to their end, or flagged (review C8).

NWUI pages its monitoring lists by number. The collector stopped at the first
page shorter than it asked for, so a server that caps its page size below
200 returned one page of a long list; it stopped silently at 50 pages; and
any HTTP 4xx ended the retries, so a 400 for a page size the server would
not take never tried the smaller ones. None of it was flagged.

The fake NWUI here pages like the real one: `pageNumber` and `pageLimit` in,
one page of items and the list's `totalCount` out.
"""
import json
import unittest
from unittest import mock

from collector_fixtures import config, nwui_action

from nwdash import nwui
from nwdash.models import RestApiError


class FakeNwui:
    """One NWUI monitoring list of `count` actions.

    cap: the largest page the server sends, whatever is asked.
    offset_by_request: page N starts at (N - 1) * the page size asked for,
        not the size sent; servers differ.
    with_total: whether answers carry totalCount.
    refuse_above: answer HTTP 400 for a pageLimit above this.
    """

    def __init__(self, count, cap=None, offset_by_request=False, with_total=True, refuse_above=None, status=None):
        self.items = [nwui_action("Running", workflow=f"wf-{n:05}") for n in range(count)]
        self.cap = cap
        self.offset_by_request = offset_by_request
        self.with_total = with_total
        self.refuse_above = refuse_above
        self.status = status
        self.requests = []

    def post(self, _config, _opener, _headers, path, payload):
        self.requests.append(dict(payload))
        asked = payload["pageLimit"]
        if self.status:
            raise RestApiError(self.status, f"HTTP {self.status} from NWUI")
        if self.refuse_above and asked > self.refuse_above:
            raise RestApiError(400, f"HTTP 400 Bad Request: pageLimit must not exceed {self.refuse_above}")
        size = min(asked, self.cap or asked)
        start = (payload["pageNumber"] - 1) * (asked if self.offset_by_request else size)
        answer = {"actions": self.items[start:start + size]}
        if self.with_total:
            answer["totalCount"] = len(self.items)
        return json.loads(json.dumps(answer))

    def read(self):
        paging = {}
        with mock.patch.object(nwui, "nwui_post_json", self.post):
            items = nwui.nwui_monitoring_all_pages(config("nwui"), None, {}, "monitoringactions", paging=paging)
        return items, paging


def _names(items):
    return [item["workflowName"] for item in items]


class TestPagingToTheEnd(unittest.TestCase):
    def test_a_server_page_cap_does_not_end_the_list(self):
        server = FakeNwui(250, cap=100)
        items, paging = server.read()
        self.assertEqual(_names(items), _names(server.items))
        self.assertFalse(paging["truncated"])

    def test_the_pages_after_a_capped_one_start_where_it_ended(self):
        # A server that counts pages by the size asked for, not the size sent.
        server = FakeNwui(250, cap=100, offset_by_request=True)
        items, paging = server.read()
        self.assertEqual(_names(items), _names(server.items))
        self.assertFalse(paging["truncated"])

    def test_without_a_total_a_short_page_is_the_last(self):
        server = FakeNwui(230, with_total=False)
        items, paging = server.read()
        self.assertEqual(len(items), 230)
        self.assertEqual([request["pageNumber"] for request in server.requests], [1, 2])
        self.assertFalse(paging["truncated"])

    def test_a_list_that_fits_one_page_is_one_request(self):
        server = FakeNwui(12)
        items, paging = server.read()
        self.assertEqual((len(items), len(server.requests)), (12, 1))
        self.assertEqual((paging["totalCount"], paging["truncated"]), (12, False))


class TestTruncationIsFlagged(unittest.TestCase):
    def test_a_list_longer_than_the_page_stop_is_flagged(self):
        server = FakeNwui(nwui.NWUI_MAX_PAGES * 200 + 20)
        items, paging = server.read()
        self.assertEqual(len(items), nwui.NWUI_MAX_PAGES * 200)
        self.assertTrue(paging["truncated"])
        self.assertEqual(paging["totalCount"], nwui.NWUI_MAX_PAGES * 200 + 20)

    def test_full_pages_to_the_stop_with_no_total_are_flagged(self):
        server = FakeNwui(nwui.NWUI_MAX_PAGES * 200 + 20, with_total=False)
        _items, paging = server.read()
        self.assertTrue(paging["truncated"])

    def test_a_list_that_runs_dry_before_its_total_is_flagged(self):
        server = FakeNwui(150)
        real_post = server.post

        def shrinking(*args):
            answer = real_post(*args)
            answer["totalCount"] = 400
            return answer

        server.post = shrinking
        items, paging = server.read()
        self.assertEqual(len(items), 150)
        self.assertTrue(paging["truncated"])


class TestRetries(unittest.TestCase):
    def test_a_refused_page_size_tries_a_smaller_one(self):
        server = FakeNwui(150, refuse_above=100)
        items, paging = server.read()
        self.assertEqual(len(items), 150)
        self.assertFalse(paging["truncated"])

    def test_a_rejected_login_is_not_retried(self):
        server = FakeNwui(10, status=401)
        with self.assertRaises(RestApiError):
            server.read()
        self.assertEqual(len(server.requests), 1)


class TestTheDashboardShowsTheFlag(unittest.TestCase):
    def build(self, server):
        def post(cfg, opener, headers, path, payload):
            if path == "monitoringactions":
                return server.post(cfg, opener, headers, path, payload)
            return {"totalCount": 0, "items": []}

        with mock.patch.object(nwui, "nwui_post_json", post), \
                mock.patch.object(nwui, "fetch_json", return_value={"jobs": []}), \
                mock.patch.object(nwui, "load_server_health_nwui", return_value={"status": "ok"}), \
                mock.patch.dict(nwui._JOBS_HISTORY_CACHE, clear=True):
            return nwui.build_dashboard_nwui(
                config("nwui"), auth_headers={"Authorization": "Bearer fixture"}, create_session=False,
            )[1]

    def test_a_truncated_list_is_flagged_on_its_source(self):
        source = self.build(FakeNwui(nwui.NWUI_MAX_PAGES * 200 + 20))["sources"]["monitoringActions"]
        self.assertTrue(source["truncated"])
        self.assertEqual(source["totalCount"], nwui.NWUI_MAX_PAGES * 200 + 20)
        self.assertIn(f"{nwui.NWUI_MAX_PAGES * 200} of {nwui.NWUI_MAX_PAGES * 200 + 20}", source["detail"])

    def test_a_complete_list_is_not_flagged(self):
        source = self.build(FakeNwui(30))["sources"]["monitoringActions"]
        self.assertNotIn("truncated", source)


if __name__ == "__main__":
    unittest.main()
