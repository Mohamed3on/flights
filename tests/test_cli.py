"""Synthetic CLI tests; no live requests or browser."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from fast_flights.cli import main
from fast_flights.exceptions import FlightsNotFound
from fast_flights.parser import ResultList


class FakeBrowser:
    instances = []

    def __init__(self):
        self.closed = False
        FakeBrowser.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def get_flights(self, q):
        return ResultList()


def results(complete=True):
    flights = ResultList()
    flights.complete = complete
    return flights


class BrowserFallbackTests(unittest.TestCase):
    def setUp(self):
        FakeBrowser.instances.clear()

    def run_cli(self, *args, plain):
        with patch("fast_flights.cli.Browser", FakeBrowser), \
                patch("fast_flights.cli.get_flights", side_effect=plain) as get_flights, \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main(["BER", "BKK", "2026-12-14", *args])  # no flights: exits after searching
        return get_flights

    def test_complete_plain_results_need_no_browser(self):
        self.run_cli(plain=[results()])
        self.assertEqual(FakeBrowser.instances, [])

    def test_refused_or_partial_plain_results_fall_back_to_one_closed_browser(self):
        for plain in ([FlightsNotFound("error 13")], [results(complete=False)]):
            with self.subTest(plain=plain):
                FakeBrowser.instances.clear()
                self.run_cli(plain=plain)
                self.assertEqual(len(FakeBrowser.instances), 1)
                self.assertTrue(FakeBrowser.instances[0].closed)

    def test_browser_flag_skips_plain_requests(self):
        get_flights = self.run_cli("--browser", plain=[results()])
        get_flights.assert_not_called()
        self.assertTrue(FakeBrowser.instances[0].closed)
