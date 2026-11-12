"""Synthetic CLI tests; no live requests or browser."""

import base64
import io
import json
import platform
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from fast_flights.browser import URL, _as_chrome, _Load
from fast_flights.cli import _dates, main
from fast_flights.parser import ResultList, parse_payload
from test_result_groups import itinerary, parse_groups, results_chunk


class FakeBrowser:
    instances = []

    def __init__(self):
        self.closed = False
        self.searches = 0
        FakeBrowser.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def get_flights(self, q):
        self.searches += 1
        return ResultList()

    def search_many(self, queries, jobs):
        self.searches += len(queries)
        # Each search's cheapest costs 100 more than the one before; a pricier one too.
        return [parse_groups(None, [[itinerary([None, 100 * i]), itinerary([None, 999])]])
                for i in range(1, len(queries) + 1)]


class BrowserTests(unittest.TestCase):
    def setUp(self):
        FakeBrowser.instances.clear()

    def run_cli(self, *args):
        with patch("fast_flights.cli.Browser", FakeBrowser), \
                patch("fast_flights.cli.get_flights", return_value=ResultList()) as get_flights, \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main(["BER", "BKK", "2026-12-14", *args])  # no flights: exits after searching
        return get_flights

    def test_searches_in_one_browser_that_is_closed(self):
        get_flights = self.run_cli()
        get_flights.assert_not_called()
        self.assertEqual([(b.searches, b.closed) for b in FakeBrowser.instances], [(1, True)])

    def test_plain_flag_needs_no_browser(self):
        get_flights = self.run_cli("--plain")
        get_flights.assert_called_once()
        self.assertEqual(FakeBrowser.instances, [])


class GridTests(unittest.TestCase):
    def setUp(self):
        FakeBrowser.instances.clear()

    def grid(self, *args):
        out = io.StringIO()
        with patch("fast_flights.cli.Browser", FakeBrowser), redirect_stdout(out):
            main(["BER", "BKK", "2026-12-14..2026-12-15", "-r", "2027-03-17,2027-03-24", *args])
        return out.getvalue().splitlines()

    def test_dates_take_lists_and_ranges(self):
        self.assertEqual(_dates("2026-12-30..2027-01-01"), ["2026-12-30", "2026-12-31", "2027-01-01"])
        self.assertEqual(_dates("2027-03-17,2027-03-24"), ["2027-03-17", "2027-03-24"])
        self.assertEqual(_dates("2027-03-17"), ["2027-03-17"])

    def test_grid_shows_each_trips_cheapest_from_one_browser(self):
        self.assertEqual(
            [line.split() for line in self.grid()],
            [["Wed", "17", "Mar", "Wed", "24", "Mar"],
             ["Mon", "14", "Dec", "€100", "Test", "Airli", "€200", "Test", "Airli"],
             ["Tue", "15", "Dec", "€300", "Test", "Airli", "€400", "Test", "Airli"]],
        )
        self.assertEqual([(b.searches, b.closed) for b in FakeBrowser.instances], [(4, True)])

    def test_grid_filters_by_airline_and_hours(self):
        self.assertTrue(all(line.count("–") == 2 for line in self.grid("--airline", "SWISS")[1:]))
        self.assertTrue(all(line.count("–") == 2 for line in self.grid("--max-hours", "1.5")[1:]))
        self.assertIn("€100", self.grid("--airline", "test airline", "--max-hours", "2")[1])


class FakePage:
    def __init__(self, url):
        self.url, self.calls = url, []

    def goto(self, url, **_):
        self.calls.append("goto")

    def evaluate(self, script, url):
        self.calls.append("pushState")


class FakeSession:
    """A page's CDP session; `buffered` is what the response streamed before being asked."""

    def __init__(self, buffered=b""):
        self.buffered = buffered

    def on(self, *_):
        pass

    def send(self, method, params=None):
        return {"bufferedData": base64.b64encode(self.buffered).decode()}


def streamed(load, *lines, close=False):
    """Feed `lines` to `load`'s GetShoppingResults request, starting it on the first call."""
    if "1" not in load.bodies:
        load._sent({"requestId": "1", "request": {"url": "https://www.google.com/x/GetShoppingResults?f"}})
        load._received({"requestId": "1", "response": {"status": 200}})
    for line in lines:
        load._data({"requestId": "1", "data": base64.b64encode(f"{len(line)}\n{line}\n".encode()).decode()})
    if close:
        load._closed({"requestId": "1"})


class LoadTests(unittest.TestCase):
    def load(self):
        return _Load(FakePage("about:blank"), FakeSession(b")]}'\n\n"), URL, "GetShoppingResults", 0)

    def test_a_loaded_web_app_searches_again_without_reloading(self):
        for start, method, calls in ((URL + "/search?tfs=X", "GetShoppingResults", ["pushState"]),
                                     ("about:blank", "GetShoppingResults", ["goto"]),
                                     (URL + "/search?tfs=X", "GetBookingResults", ["goto"])):
            with self.subTest(start=start, method=method):
                page = FakePage(start)
                _Load(page, FakeSession(), URL + "/search?tfs=Y", method, 0)
                self.assertEqual(page.calls, calls)

    def test_done_at_the_finished_snapshot_while_the_response_stays_open(self):
        # Google keeps a search's response open long after its finished snapshot, and
        # streams small updates without results in between.
        load = self.load()
        streamed(load, results_chunk(100, done=False))
        self.assertIsNone(load.poll())
        small = json.dumps([["wrb.fr", None, json.dumps([None] * 31 + [True])]])
        streamed(load, results_chunk(100, 90), small)
        self.assertEqual([f.price for f in parse_payload(load.poll())], [100, 90])

    def test_an_unfinished_search_waits(self):
        load = self.load()
        streamed(load, results_chunk(100, done=False), close=True)
        self.assertIsNone(load.poll())


class FakeCDP:
    def __init__(self):
        self.sent = {}

    def send(self, method, params=None):
        self.sent[method] = params
        ua = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/158.0.1.2 Safari/537.36"
        return {"userAgent": ua}


class AsChromeTests(unittest.TestCase):
    def test_hides_headless_from_user_agent_and_client_hints(self):
        # Google answers a browser whose hints lack "Google Chrome" with its bot results.
        cdp = FakeCDP()
        page = type("Page", (), {"context": type("Context", (), {"new_cdp_session": lambda self, p: cdp})()})()
        _as_chrome(page, "158.0.1.2")
        override = cdp.sent["Network.setUserAgentOverride"]
        self.assertNotIn("Headless", override["userAgent"])
        brands = override["userAgentMetadata"]["brands"]
        self.assertIn({"brand": "Google Chrome", "version": "158"}, brands)
        self.assertFalse(any("Headless" in b["brand"] for b in brands))
        self.assertEqual(override["userAgentMetadata"]["platform"] == "macOS", platform.system() == "Darwin")
