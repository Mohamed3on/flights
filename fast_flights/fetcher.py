from typing import TypeVar, overload

from primp import Client

from .integrations.base import DataSourceIntegration, FetchIntegration
from .parser import ResultList, parse
from .querying import Query

URL = "https://www.google.com/travel/flights"
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

    html = fetch_flights_html(q, proxy=proxy, fetch_integration=integration)
    return parse(html)


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
        client = Client(
            impersonate="chrome_145",
            impersonate_os="macos",
            referer=True,
            proxy=proxy,
            cookie_store=True,
        )
        client.set_cookies("https://www.google.com", {"SOCS": CONSENT_COOKIE})

        if isinstance(q, Query):
            params = q.params()

        else:
            params = {"q": q}

        res = client.get(URL, params=params)
        return res.text

    else:
        return fetch_integration.fetch_html(q)
