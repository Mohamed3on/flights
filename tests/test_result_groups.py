# [AI CONTENT]
"""Synthetic regression coverage; no live requests or booking tokens."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fast_flights.exceptions import FlightsNotFound
from fast_flights.fetcher import _fetch_deferred_results, get_booking_options, get_flights
from fast_flights.parser import parse, parse_js
from fast_flights.querying import FlightQuery, create_query


def itinerary(price):
    segment = [None] * 23
    segment[3:7] = ["AAA", "Origin", "Destination", "BBB"]
    segment[8], segment[10] = [9], [11]
    segment[11], segment[17] = 120, "Test aircraft"
    segment[20] = segment[21] = [2026, 10, 1]
    segment[22] = ["TA", "1"]
    flight = [None] * 23
    flight[0:3] = ["nonstop", ["Test Airline"], [segment]]
    flight[22] = [None] * 7 + [100000, 120000]
    return [flight, [price]]


def parse_groups(best, other):
    payload = [None] * 8
    payload[2], payload[3] = best, other
    payload[7] = [None, [[["TEST", "Test Alliance"]], [["TA", "Test Airline"]]]]
    return parse_js(f"data:{json.dumps(payload)},ignored")


class ResultGroupTests(unittest.TestCase):
    def test_both_groups_are_returned_in_order(self):
        flights = parse_groups([[itinerary([None, 100])]], [[itinerary([None, 200])]])
        self.assertEqual([f.price for f in flights], [100, 200])

    def test_each_group_can_be_absent_or_empty(self):
        for empty in (None, [], [None], [[]]):
            for best, other in ((empty, [[itinerary([None, 100])]]),
                                ([[itinerary([None, 100])]], empty)):
                with self.subTest(best=best, other=other):
                    self.assertEqual([f.price for f in parse_groups(best, other)], [100])

    def test_missing_price_preserves_itinerary_and_following_results(self):
        for fare in ([], [None], [None, None]):
            with self.subTest(fare=fare):
                flights = parse_groups(None, [[itinerary(fare), itinerary([None, 200])]])
                self.assertEqual([f.price for f in flights], [None, 200])
                self.assertEqual(flights[0].flights[0].from_airport.code, "AAA")

    def test_zero_price_is_not_missing(self):
        self.assertEqual(parse_groups(None, [[itinerary([None, 0])]])[0].price, 0)

    def test_empty_results_retain_metadata(self):
        flights = parse_groups([None], [None])
        self.assertEqual(flights, [])
        self.assertEqual(flights.metadata.airlines[0].code, "TA")


def deferred_page(token, query, *prices):
    """A results page that Google rendered before the search finished."""
    status = [None] * 8
    status[0] = [None, None, 0, "id", token]
    status[2] = [[itinerary([None, price]) for price in prices]] if prices else None
    status[7] = [None, [[["TEST", "Test Alliance"]]]]  # no airline list yet
    return "".join(
        f"<script class=\"ds:{key}\">AF_initDataCallback({{key: 'ds:{key}', hash: '1', "
        f"data:{json.dumps(data)}, sideChannel: {{}}}});</script>"
        for key, data in ((0, [None, [2, query]]), (1, status))
    )


def results_chunk(*prices, done=True):
    """One streamed "wrb.fr" chunk; only a finished search's snapshot sets index 31."""
    payload = [None] * (33 if done else 8)
    payload[2] = [[itinerary([None, price]) for price in prices]]
    payload[7] = [None, [[["TEST", "Test Alliance"]], [["TA", "Test Airline"]]]]
    if done:
        payload[31] = payload[32] = True
    return json.dumps([["wrb.fr", None, json.dumps(payload)]])


REFUSED = json.dumps([["wrb.fr", None, None, None, None, [13]]])


class FakeClient:
    """Answers each post with the next response, a list of chunks, then repeats the last."""

    def __init__(self, *responses, page=""):
        self.page = page
        self.bodies = [
            ")]}'\n\n" + "".join(f"{len(c)}\n{c}\n" for c in chunks) for chunks in responses
        ]
        self.posts = []

    def get(self, url, **kwargs):
        return SimpleNamespace(text=self.page, status_code=200)

    def post(self, url, **kwargs):
        self.posts.append(kwargs)
        body = self.bodies[min(len(self.posts), len(self.bodies)) - 1]
        return SimpleNamespace(text=body, status_code=200)


class DeferredResultsTests(unittest.TestCase):
    query = create_query(
        flights=[FlightQuery(date="2027-03-20", from_airport="AAA", to_airport="BBB")],
        language="en-US",
        currency="EUR",
    )

    def test_page_without_results_or_airlines_parses_empty(self):
        self.assertEqual(parse(deferred_page("TOKEN", ["QUERY"])), [])

    def test_follow_up_echoes_page_token_and_query(self):
        client = FakeClient([results_chunk(100), json.dumps([["di", 1]])])
        flights = _fetch_deferred_results(client, deferred_page("TOKEN", ["QUERY"]), self.query)
        self.assertEqual([f.price for f in flights], [100])
        request = json.loads(json.loads(client.posts[0]["data"]["f.req"])[1])
        self.assertEqual(request[:2], [[None, None, None, "TOKEN"], ["QUERY"]])

    def test_follow_up_keeps_the_last_streamed_snapshot(self):
        client = FakeClient([results_chunk(100, done=False), results_chunk(100, 90)])
        flights = _fetch_deferred_results(client, deferred_page("TOKEN", ["QUERY"]), self.query)
        self.assertEqual([f.price for f in flights], [100, 90])
        self.assertEqual(len(client.posts), 1)

    def test_follow_up_asks_again_until_the_search_finishes(self):
        client = FakeClient([REFUSED], [results_chunk(100, done=False)], [results_chunk(100, 90)])
        flights = _fetch_deferred_results(client, deferred_page("TOKEN", ["QUERY"]), self.query)
        self.assertEqual([f.price for f in flights], [100, 90])
        self.assertEqual(len(client.posts), 3)

    def test_refused_follow_up_is_an_error_not_an_empty_result(self):
        client = FakeClient([REFUSED])
        with self.assertRaisesRegex(FlightsNotFound, "refused"):
            _fetch_deferred_results(client, deferred_page("TOKEN", ["QUERY"]), self.query)

    def test_refused_follow_up_falls_back_to_the_page_results(self):
        client = FakeClient([REFUSED], page=deferred_page("TOKEN", ["QUERY"], 100))
        with patch("fast_flights.fetcher._client", return_value=client):
            flights = get_flights(self.query)
        self.assertEqual([f.price for f in flights], [100])
        self.assertFalse(flights.complete)

    def test_results_keep_each_itinerary_booking_token(self):
        flight = itinerary([None, 100])[0]
        flights = parse_groups(None, [[[flight, [[None, 100], "BOOKTOKEN"]], itinerary([None, 90])]])
        self.assertEqual([f.booking_token for f in flights], ["BOOKTOKEN", None])


def booking_option(seller, price, fare=None, bag=None):
    """One option of a GetBookingResults payload; airlines name the fare and its bag."""
    option = [None] * 23
    option[1] = [[seller[:2].upper(), seller, None, fare is not None]]
    option[7] = [[None, price], "ignored"]
    if fare:
        option[21] = [["XX", fare.upper()], [[8, 1], [9, bag]], None, fare]
    return option


class BookingOptionsTests(unittest.TestCase):
    query = DeferredResultsTests.query

    def test_lists_each_seller_fare_and_checked_bag(self):
        page = (
            "<script class=\"ds:0\">AF_initDataCallback({key: 'ds:0', hash: '1', data:"
            + json.dumps([None, [3, ["ITINERARY"], None, [None, [0, 1], None, []]]])
            + ", sideChannel: {}});</script>"
        )
        payload = [None, [[
            booking_option("SWISS", 2338, "Economy Light", 2),
            booking_option("SWISS", 2617, "Economy Comfort", 1),
            booking_option("Expedia", 2249),
        ]]]
        client = FakeClient([json.dumps([["wrb.fr", None, json.dumps(payload)]])], page=page)
        with patch("fast_flights.fetcher._client", return_value=client):
            options = get_booking_options(self.query, "BOOKTOKEN")
        self.assertEqual(
            [(o.seller, o.price, o.fare, o.checked_bag) for o in options],
            [("SWISS", 2338, "Economy Light", False), ("SWISS", 2617, "Economy Comfort", True),
             ("Expedia", 2249, None, None)],
        )
        request = json.loads(json.loads(client.posts[0]["data"]["f.req"])[1])
        self.assertEqual(request, [[None, "BOOKTOKEN"], ["ITINERARY"], None, 0])
# [/AI CONTENT]
