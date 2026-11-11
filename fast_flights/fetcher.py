import json
from typing import TypeVar, overload

from primp import Client
from selectolax.lexbor import LexborHTMLParser

from .exceptions import FlightsNotFound
from .integrations.base import DataSourceIntegration, FetchIntegration
from .model import BookingOption
from .parser import ResultList, parse, parse_booking_options, parse_payload
from .querying import Query

URL = "https://www.google.com/travel/flights"
SHOPPING_RESULTS_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetShoppingResults"
)
BOOKING_URL = "https://www.google.com/travel/flights/booking"
BOOKING_RESULTS_URL = SHOPPING_RESULTS_URL.replace("GetShoppingResults", "GetBookingResults")
FOLLOW_UP_TRIES = 3
# EU/EEA IPs get Google's consent page instead of results unless this SOCS cookie is set.
CONSENT_COOKIE = "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODI5LjA3X3AxGgJlbiACGgYIgJnPpwY"


T = TypeVar("T")


@overload
def get_flights(
    q: Query | str, /, *, proxy: str | None = None, integration: None = None
) -> ResultList: ...


@overload
def get_flights(
    q: Query | str, /, *, proxy: str | None = None, integration: FetchIntegration
) -> ResultList: ...


@overload
def get_flights(
    q: Query | str,
    /,
    *,
    proxy: str | None = None,
    integration: DataSourceIntegration[T],
) -> T: ...


def get_flights(
    q: Query | str,
    /,
    *,
    proxy: str | None = None,
    integration: FetchIntegration | DataSourceIntegration[T] | None = None,
) -> T | ResultList:
    """Get flights.

    Args:
        q: The query.
        proxy (optional): Proxy, if you're using `fast-flight`'s default fetcher.
        integration (optional): Plug-in integration.
    """
    if integration is not None and isinstance(integration, DataSourceIntegration):
        return integration.fetch(q)

    if integration is not None or not isinstance(q, Query):
        return parse(fetch_flights_html(q, proxy=proxy, fetch_integration=integration))

    client = _client(proxy)
    html = client.get(URL, params=q.params()).text
    # The page carries at most the first results Google found (16 of 32 for
    # BKK→CNX, none for slow searches); the web client streams the rest.
    try:
        return _fetch_deferred_results(client, html, q)
    except FlightsNotFound:
        # When Google refuses the rest, the page's first results beat nothing.
        flights = parse(html)
        if not flights:
            raise
        flights.complete = False
        return flights


def _client(proxy: str | None) -> Client:
    client = Client(
        impersonate="chrome_145",
        impersonate_os="macos",
        referer=True,
        proxy=proxy,
        cookie_store=True,
    )
    client.set_cookies("https://www.google.com", {"SOCS": CONSENT_COOKIE})
    return client


def _compact(o: object) -> str:
    return json.dumps(o, separators=(",", ":"))


def _page_data(html: str, key: int) -> list:
    script = LexborHTMLParser(html).css_first(rf"script.ds\:{key}")
    return json.loads(script.text().split("data:", 1)[1].rsplit(",", 1)[0])


def _fetch_deferred_results(client: Client, html: str, q: Query) -> ResultList:
    """Fetch the results Google left out of the page, as the web client does.

    The page still carries a search token (ds:1) and the decoded query (ds:0);
    GetShoppingResults takes both and streams the results as they arrive.
    """
    token, query = _page_data(html, 1)[0][4], _page_data(html, 0)[1][1]
    # Google sometimes refuses the request or ends the stream before the search
    # finishes; asking again with the same token picks up where it stopped.
    for _ in range(FOLLOW_UP_TRIES):
        payload, error, status = _rpc(
            client, SHOPPING_RESULTS_URL, [[None, None, None, token], query, 0, 1, 0, 1], q
        )
        # Only the snapshot of a finished search sets index 31.
        if payload is not None and len(payload) > 31 and payload[31]:
            break
    if payload is not None:
        return parse_payload(payload)
    raise _rpc_failure(error, status)


def get_booking_options(
    q: Query, booking_token: str, /, *, proxy: str | None = None
) -> list[BookingOption]:
    """Get the live booking options for an itinerary, the airline's fares first.

    Every leg of `q` needs its flights picked (`FlightQuery.selected_flights`),
    and `booking_token` is that itinerary's `Flights.booking_token` from the
    search results. Search results carry Google's cached prices; these are
    checked with the airline and travel sites, and name each fare and whether
    it includes a checked bag.
    """
    client = _client(proxy)
    html = client.get(BOOKING_URL, params=q.params()).text
    # The page carries the itinerary as Google decoded it from `tfs`.
    query = _page_data(html, 0)[1][1]
    payload, error, status = _rpc(client, BOOKING_RESULTS_URL, [[None, booking_token], query, None, 0], q)
    if payload is None:
        raise _rpc_failure(error, status)
    return parse_booking_options(payload)


def _rpc(client: Client, url: str, request: list, q: Query) -> tuple[list | None, int | None, int]:
    """Call a FlightsFrontendService method; return its last payload, refusal code and HTTP status."""
    res = client.post(
        url,
        params={"hl": q.language, "rt": "c"},
        data={"f.req": _compact([None, _compact(request)])},
        headers={
            "X-Same-Domain": "1",
            "x-goog-ext-259736195-jspb": _compact(
                [q.language or None, None, q.currency or None, 1, None, None, None, None, 1, []]
            ),
        },
    )
    payload = error = None
    # The body is ")]}'" then length-prefixed JSON chunks. Results come in "wrb.fr"
    # chunks, each a fuller snapshot than the one before.
    for line in res.text.splitlines():
        if line.startswith('[["wrb.fr"'):
            chunk = json.loads(line)[0]
            if chunk[2] is None:
                error = chunk[5][0]
            else:
                payload = json.loads(chunk[2])
    return payload, error, res.status_code


def _rpc_failure(error: int | None, status: int) -> FlightsNotFound:
    if error is not None:
        return FlightsNotFound(
            f"Google refused the follow-up request for this search's results "
            f"(error {error}), as it does after many searches from one IP; retry later"
        )
    return FlightsNotFound(f"Google's follow-up results request failed (HTTP {status})")


def fetch_flights_html(
    q: Query | str,
    /,
    *,
    proxy: str | None = None,
    fetch_integration: FetchIntegration | None = None,
) -> str:
    """Fetch flights and get the **HTML**.

    Args:
        q: The query.
        proxy (str, optional): Proxy.
    """
    if fetch_integration is None:
        client = _client(proxy)

        if isinstance(q, Query):
            params = q.params()

        else:
            params = {"q": q}

        res = client.get(URL, params=params)
        return res.text

    else:
        return fetch_integration.fetch_html(q)
