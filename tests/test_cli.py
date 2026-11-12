"""Synthetic CLI tests; no live requests or browser."""

import io
import platform
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from fast_flights.browser import _as_chrome
from fast_flights.cli import main
from fast_flights.parser import ResultList


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
