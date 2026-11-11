from dataclasses import dataclass
from typing import Literal


@dataclass
class Airline:
    code: str
    name: str


@dataclass
class Alliance:
    code: str
    name: str


@dataclass
class JsMetadata:
    airlines: list[Airline]
    alliances: list[Alliance]


@dataclass
class Airport:
    name: str
    code: str


@dataclass
class SimpleDatetime:
    date: tuple[int, int, int]
    time: tuple[int, int]


@dataclass
class SingleFlight:
    from_airport: Airport
    to_airport: Airport
    departure: SimpleDatetime
    arrival: SimpleDatetime

    duration: int
    """Unit: minutes"""

    plane_type: str

    flight_number: str
    """Airline code and number, e.g. "LX 180"."""


@dataclass
class CarbonEmission:
    typical_on_route: int
    """Unit: grams"""

    emission: int
    """Unit: grams"""


@dataclass
class Flights:
    type: str | Literal["multi"]
    price: int | None
    """Fare amount, or None when Google returns an unpriced itinerary."""
    airlines: list[str]
    flights: list[SingleFlight]
    carbon: CarbonEmission

    booking_token: str | None = None
    """Identifies this itinerary to `get_booking_options`."""


@dataclass
class BookingOption:
    """One way to book an itinerary, from its Google Flights booking page."""

    seller: str
    """The airline or travel site selling it."""

    price: int | None

    fare: str | None
    """The airline's fare name, e.g. "Economy Comfort"; None for travel sites."""

    checked_bag: bool | None
    """Whether the fare includes a checked bag per passenger; None when unknown."""
