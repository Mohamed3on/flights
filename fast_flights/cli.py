"""Search Google Flights from the command line: `flights BER LHR 2026-11-15`."""

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime

from .fetcher import get_flights
from .model import Flights, SimpleDatetime
from .querying import FlightQuery, Passengers, create_query

SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def _dt(t: SimpleDatetime) -> datetime:
    return datetime(*t.date, *t.time)


def _row(f: Flights, symbol: str) -> str:
    legs = f.flights
    first, last = legs[0], legs[-1]
    # Each layover starts and ends in one airport's timezone, so local times subtract exactly.
    layovers = sum(
        (_dt(b.departure) - _dt(a.arrival)).total_seconds() // 60
        for a, b in zip(legs, legs[1:])
    )
    total = int(sum(leg.duration for leg in legs) + layovers)
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
    p.add_argument("date", help="departure date, YYYY-MM-DD")
    p.add_argument(
        "-r", "--return", dest="return_date", metavar="DATE",
        help="return date; prices become round-trip totals",
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
    p.add_argument("-c", "--currency", default="EUR")
    p.add_argument(
        "-n", "--limit", type=int, default=20, help="rows to print, 0 for all (default: 20)"
    )
    p.add_argument("--json", action="store_true", help="print every result as JSON")
    args = p.parse_args(argv)
    if args.return_date and args.leg:
        p.error("use either --return or --leg, not both")

    origin, destination = args.origin.upper(), args.destination.upper()
    currency = args.currency.upper()
    legs = [FlightQuery(date=args.date, from_airport=origin, to_airport=destination)]
    if args.return_date:
        legs.append(
            FlightQuery(date=args.return_date, from_airport=destination, to_airport=origin)
        )
    legs += [
        FlightQuery(date=d, from_airport=a.upper(), to_airport=b.upper())
        for a, b, d in args.leg or []
    ]

    try:
        results = get_flights(
            create_query(
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
        )
    except Exception as e:
        sys.exit(f"flights: {e}")

    results = sorted(results, key=lambda f: (f.price is None, f.price or 0))
    if args.json:
        print(json.dumps([asdict(f) for f in results], indent=2))
        return
    if not results:
        sys.exit("flights: no flights found")

    for f in results[: args.limit or None]:
        print(_row(f, SYMBOLS.get(currency, currency + " ")))
    if args.limit and len(results) > args.limit:
        print(f"… {len(results) - args.limit} more (-n 0 shows all)")
