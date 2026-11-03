"""NWUI mode's Clients figure counts NetWorker's clients (review C21).

It counted the names in the jobs list: NWUI's live actions are per workflow
and name a workflow, the jobs database's records name a client host, so the
figure was workflows plus hosts. It now comes from the REST API's
/global/clients, the list REST mode counts. When that list is out of reach,
the client hosts that ran a job stand in; a workflow name never counts.
"""
import unittest
from unittest import mock

from collector_fixtures import NwuiServer, client_record, config, nwui_action, rest_job

from nwdash import nwui
from nwdash.models import RestApiError

CLIENTS = [client_record(f"fs0{n}.example.com") for n in (1, 2, 3)]


def _activity():
    """Two live workflows, and jobs of two client hosts in the history."""
    return {
        "live": [nwui_action("Running", workflow="Gold-Filesystems"), nwui_action("Running", workflow="Silver-VMs")],
        "history": [rest_job(9601, "fs01.example.com"), rest_job(9602, "fs02.example.com")],
    }


class TestClientsFigure(unittest.TestCase):
    def test_the_figure_is_the_client_list(self):
        body = NwuiServer(clients=CLIENTS, **_activity()).build()
        self.assertEqual(body["summary"]["totalClients"], 3)
        self.assertEqual([row["hostname"] for row in body["tables"]["clients"]],
                         ["fs01.example.com", "fs02.example.com", "fs03.example.com"])

    def test_the_list_comes_from_the_rest_api(self):
        server = NwuiServer(clients=CLIENTS, **_activity())
        body = server.build()
        self.assertEqual(len(server.urls_for("/global/clients")), 1)
        self.assertTrue(body["sources"]["clients"]["ok"])

    def test_without_the_list_client_hosts_stand_in(self):
        server = NwuiServer(clients=RestApiError(502, "HTTP 502 from NetWorker"), **_activity())
        body = server.build()
        self.assertEqual(body["summary"]["totalClients"], 2)
        self.assertEqual([row["hostname"] for row in body["tables"]["clients"]],
                         ["fs01.example.com", "fs02.example.com"])
        self.assertFalse(body["sources"]["clients"]["ok"])
        self.assertEqual(body["summary"]["health"], "ok", "a missing client list is not a degraded estate")

    def test_a_workflow_is_never_a_client(self):
        body = NwuiServer(live=_activity()["live"], history=RestApiError(502, "HTTP 502")).build()
        self.assertEqual(body["summary"]["totalClients"], 0)

    def test_no_history_means_no_client_query(self):
        server = NwuiServer(live=_activity()["live"], history=RestApiError(502, "HTTP 502"), clients=CLIENTS)
        server.build()
        self.assertEqual(server.urls_for("/global/clients"), [])

    def test_the_list_is_cached_between_builds(self):
        server = NwuiServer(clients=CLIENTS)
        with mock.patch.object(nwui, "fetch_json", server.fetch), mock.patch.dict(nwui._CLIENTS_CACHE, clear=True):
            first = nwui.cached_nwui_clients(config("nwui"), None)
            second = nwui.cached_nwui_clients(config("nwui"), None)
        self.assertEqual((len(first[0]), first[2]), (3, False))
        self.assertEqual((len(second[0]), second[2]), (3, True))
        self.assertEqual(len(server.urls_for("/global/clients")), 1)


if __name__ == "__main__":
    unittest.main()
