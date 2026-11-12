"""Search Google Flights from the command line: `flights BER LHR 2026-11-15`."""

import argparse
import json
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import TypeVar

from .browser import Browser
from .fetcher import get_booking_options, get_flights
from .model import Flights, SimpleDatetime
from .parser import ResultList
from .querying import FlightQuery, Passengers, Query, create_query

T = TypeVar("T")

SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def _dt(t: SimpleDatetime) -> datetime:
    return datetime(*t.date, *t.time)


def _by_price(results: list[Flights]) -> list[Flights]:
    return sorted(results, key=lambda f: (f.price is None, f.price or 0))


def _minutes(f: Flights) -> int:
    """The outbound's door-to-door time: flights plus layovers."""
    legs = f.flights
    # Each layover starts and ends in one airport's timezone, so local times subtract exactly.
    layovers = sum(
        (_dt(b.departure) - _dt(a.arrival)).total_seconds() // 60
        for a, b in zip(legs, legs[1:])
    )
    return int(sum(leg.duration for leg in legs) + layovers)


def _dates(value: str) -> list[str]:
    """One date, a list (`2027-03-17,2027-03-24`) or a range (`2026-12-11..2026-12-16`)."""
    if ".." in value:
        first, last = (date.fromisoformat(d) for d in value.split(".."))
        return [str(first + timedelta(days=i)) for i in range((last - first).days + 1)]
    return [str(date.fromisoformat(d)) for d in value.split(",")]


def _row(f: Flights, symbol: str) -> str:
    legs = f.flights
    first, last = legs[0], legs[-1]
    total = _minutes(f)
    days = (_dt(last.arrival).date() - _dt(first.departure).date()).days
    arrival = f"{_dt(last.arrival):%H:%M}" + (f"+{days}" if days else "")
    stops = (
        "nonstop"
        if len(legs) == 1
        else f"{len(legs) - 1} stop{'s' if len(legs) > 2 else ''} via "
        + ", ".join(leg.to_airport.code for leg in legs[:-1])
    )
    price = "n/a" if f.price is None else f"{symbol}{f.price}"
    return (
        f"{price:>7}  {', '.join(f.airlines)[:28]:28}  "
        f"{_dt(first.departure):%H:%M} {first.from_airport.code} → {arrival:7} {last.to_airport.code}  "
        f"{total // 60}h{total % 60:02d}m  {stops}"
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="flights", description="Search Google Flights. Results are sorted by price."
    )
    p.add_argument("origin", help="airport or city code, e.g. BER")
    p.add_argument("destination", help="airport or city code, e.g. LHR or LON")
    p.add_argument(
        "date",
        help="departure date, YYYY-MM-DD; several dates (A,B or A..B) print a grid of the "
        "cheapest price for each",
    )
    p.add_argument(
        "-r", "--return", dest="return_date", metavar="DATE",
        help="return date, or dates for the grid; prices become round-trip totals",
    )
    p.add_argument(
        "-o", "--outbound", type=int, metavar="ROW",
        help="with --return: list the return flights for outbound row ROW",
    )
    p.add_argument(
        "-b", "--book", type=int, metavar="ROW",
        help="list the live booking options for result row ROW: each seller, fare and "
        "whether it includes a checked bag (search prices are Google's cached ones)",
    )
    p.add_argument(
        "--leg", nargs=3, action="append", metavar=("FROM", "TO", "DATE"),
        help="add a multi-city leg (repeatable); prices become trip totals",
    )
    p.add_argument(
        "-s", "--seat", default="economy",
        choices=["economy", "premium-economy", "business", "first"],
    )
    p.add_argument("-a", "--adults", type=int, default=1)
    p.add_argument("--children", type=int, default=0, help="ages 2-11")
    p.add_argument("--infants-lap", type=int, default=0, help="under 2, on a lap")
    p.add_argument("--infants-seat", type=int, default=0, help="under 2, in their own seat")
    p.add_argument("--max-stops", type=int)
    p.add_argument(
        "--carry-on", action="store_true",
        help="include the fee for one carry-on bag per passenger",
    )
    p.add_argument("--airline", help="only itineraries with this airline, e.g. SWISS")
    p.add_argument(
        "--max-hours", type=float, metavar="H",
        help="only itineraries whose outbound takes at most H hours, layovers included",
    )
    p.add_argument(
        "-j", "--jobs", type=int, default=6, help="searches at once for a grid (default: 6)"
    )
    p.add_argument("-c", "--currency", default="EUR")
    p.add_argument(
        "--plain", action="store_true",
        help="search with plain requests instead of a headless Chromium: a few seconds "
        "faster, but Google takes them for a bot and answers with fewer, pricier "
        "itineraries and no fare names, or refuses them",
    )
    p.add_argument(
        "-n", "--limit", type=int, default=20, help="rows to print, 0 for all (default: 20)"
    )
    p.add_argument("--json", action="store_true", help="print every result as JSON")
    args = p.parse_args(argv)
    try:
        dates = _dates(args.date)
        returns = _dates(args.return_date) if args.return_date else [None]
    except ValueError as e:
        p.error(f"bad date: {e}")
    grid = len(dates) > 1 or len(returns) > 1
    if grid and (args.outbound or args.book or args.leg):
        p.error("a grid of dates works with neither --outbound, --book nor --leg")
    if args.return_date and args.leg:
        p.error("use either --return or --leg, not both")
    if args.outbound and not args.return_date:
        p.error("--outbound needs --return")
    if args.book and (args.leg or args.return_date and not args.outbound):
        p.error("--book works on one-way results, or on return flights listed with --outbound")

    origin, destination = args.origin.upper(), args.destination.upper()
    currency = args.currency.upper()
    symbol = SYMBOLS.get(currency, currency + " ")

    def trip(day: str, back: str | None) -> list[FlightQuery]:
        legs = [FlightQuery(date=day, from_airport=origin, to_airport=destination)]
        if back:
            legs.append(FlightQuery(date=back, from_airport=destination, to_airport=origin))
        return legs + [
            FlightQuery(date=d, from_airport=a.upper(), to_airport=b.upper())
            for a, b, d in args.leg or []
        ]

    def wanted(f: Flights) -> bool:
        airline = args.airline is None or args.airline.lower() in (a.lower() for a in f.airlines)
        return airline and (args.max_hours is None or _minutes(f) <= args.max_hours * 60)

    legs = trip(dates[0], returns[0])

    def query(legs: list[FlightQuery] = legs) -> Query:
        return create_query(
            flights=legs,
            seat=args.seat,
            trip="multi-city" if args.leg else "round-trip" if args.return_date else "one-way",
            passengers=Passengers(
                adults=args.adults,
                children=args.children,
                infants_in_seat=args.infants_seat,
                infants_on_lap=args.infants_lap,
            ),
            language="en-US",
            currency=currency,
            max_stops=args.max_stops,
            carry_on_bags=int(args.carry_on),
        )

    def pick(rows: list[Flights], row: int, what: str) -> Flights:
        if not 0 < row <= len(rows):
            sys.exit(f"flights: no {what} row {row}; there are {len(rows)}")
        return rows[row - 1]

    with ExitStack() as stack:
        browser: Browser | None = None

        def browse() -> Browser:
            nonlocal browser
            browser = browser or stack.enter_context(Browser())
            return browser

        def fetch(plain: Callable[[], T], in_browser: Callable[[Browser], T]) -> T:
            return plain() if args.plain else in_browser(browse())

        def search() -> ResultList:
            return fetch(lambda: get_flights(query()), lambda b: b.get_flights(query()))

        if grid:
            trips = [(day, back) for day in dates for back in returns]
            queries = [query(trip(day, back)) for day, back in trips]
            found = fetch(
                lambda: [_attempt(get_flights, q) for q in queries],
                lambda b: b.search_many(queries, args.jobs),
            )
            cells = {}
            for (day, back), results in zip(trips, found):
                if isinstance(results, Exception):
                    print(f"flights: {day} {back or ''}: {results}", file=sys.stderr)
                    cells[day, back] = None
                    continue
                priced = [f for f in results if f.price is not None and wanted(f)]
                cells[day, back] = min(priced, key=lambda f: f.price) if priced else None
            _print_grid(dates, returns, cells, symbol, args.json)
            return

        try:
            results = search()
            # Google lists a round trip's return flights only once an outbound is picked.
            if args.outbound:
                legs[0].selected_flights = pick(_by_price(results), args.outbound, "outbound").flights
                results = search()
            if args.book:
                booking = pick(_by_price(results), args.book, "result")
                legs[-1].selected_flights = booking.flights
                options = fetch(
                    lambda: get_booking_options(query(), booking.booking_token),
                    lambda b: b.get_booking_options(query()),
                )
        except Exception as e:
            sys.exit(f"flights: {e}")
    if args.book:
        if args.json:
            print(json.dumps([asdict(o) for o in options], indent=2))
            return
        bags = {True: "checked bag included", False: "no checked bag", None: ""}
        for o in options:
            price = "n/a" if o.price is None else f"{symbol}{o.price}"
            print(f"{price:>7}  {o.seller[:22]:22}  {(o.fare or '')[:24]:24}  {bags[o.checked_bag]}".rstrip())
        return
    if not results.complete:
        print(
            f"flights: Google refused the full results, so these are only the first "
            f"{len(results)} it found; retry later for the rest",
            file=sys.stderr,
        )

    results = _by_price([f for f in results if wanted(f)])
    if args.json:
        print(json.dumps([asdict(f) for f in results], indent=2))
        return
    if not results:
        sys.exit("flights: no flights found")

    for i, f in enumerate(results[: args.limit or None], 1):
        print(f"{i:>3}  " + _row(f, symbol))
    if args.limit and len(results) > args.limit:
        print(f"… {len(results) - args.limit} more (-n 0 shows all)")


def _attempt(search: Callable[[Query], T], q: Query) -> T | Exception:
    try:
        return search(q)
    except Exception as e:
        return e


def _print_grid(
    dates: list[str], returns: list[str | None], cells: dict, symbol: str, as_json: bool
) -> None:
    """Departure dates down, return dates across; each cell the cheapest itinerary."""
    if as_json:
        print(json.dumps([
            {"date": day, "return": back, "price": f and f.price, "airlines": f and f.airlines,
             "departure": f and f"{_dt(f.flights[0].departure):%H:%M}", "minutes": f and _minutes(f)}
            for (day, back), f in cells.items()
        ], indent=2))
        return

    def label(day: str | None) -> str:
        return f"{date.fromisoformat(day):%a %d %b}" if day else "one way"

    def cell(f: Flights | None) -> str:
        return f"{symbol}{f.price} {f.airlines[0][:10]}" if f else "–"

    width = max(len(cell(f)) for f in cells.values()) + 2
    print(" " * 10 + "".join(f"{label(back):>{width}}" for back in returns))
    for day in dates:
        print(f"{label(day):10}" + "".join(f"{cell(cells[day, back]):>{width}}" for back in returns))
