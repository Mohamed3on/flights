import json
from typing import TypeVar, overload

from primp import Client
from selectolax.lexbor import LexborHTMLParser

from .exceptions import FlightsNotFound
from .integrations.base import DataSourceIntegration, FetchIntegration
from .parser import ResultList, parse, parse_payload
from .querying import Query

URL = "https://www.google.com/travel/flights"
SHOPPING_RESULTS_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetShoppingResults"
)
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
    flights = parse(html)
    if not flights:
        # Slow searches (multi-city, several passengers, some long-haul routes)
        # render without results, and the web client fetches them separately.
        flights = _fetch_deferred_results(client, html, q)
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
    GetShoppingResults takes both and returns the top results, usually about ten.
    """
    token, query = _page_data(html, 1)[0][4], _page_data(html, 0)[1][1]
    res = client.post(
        SHOPPING_RESULTS_URL,
        params={"hl": q.language, "rt": "c"},
        data={"f.req": _compact([None, _compact([[None, None, None, token], query, 0, 1, 0, 1])])},
        headers={
            "X-Same-Domain": "1",
            "x-goog-ext-259736195-jspb": _compact(
                [q.language or None, None, q.currency or None, 1, None, None, None, None, 1, []]
            ),
        },
    )
    # The body is ")]}'" then length-prefixed JSON chunks; results are in "wrb.fr".
    for line in res.text.splitlines():
        if line.startswith('[["wrb.fr"'):
            chunk = json.loads(line)[0]
            if chunk[2] is None:
                raise FlightsNotFound(
                    f"Google refused the follow-up request for this search's results "
                    f"(error {chunk[5][0]}), as it does after many searches from one IP; retry later"
                )
            return parse_payload(json.loads(chunk[2]))
    raise FlightsNotFound(f"Google's follow-up results request failed (HTTP {res.status_code})")


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
