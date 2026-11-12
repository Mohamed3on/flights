"""Search Google Flights in a headless Chromium, as its web app does.

Google answers scripted results requests with error 13 once it distrusts an IP,
but not the web app's own: their `x-goog-batchexecute-bgr` header carries a
BotGuard token minted in the page for that very request. So this loads the
results or booking page and reads the page's own GetShoppingResults or
GetBookingResults response. Slower than plain requests (about 2–10 s a page),
but it gets what a browser gets.
"""

import subprocess
import sys
import time
from urllib.parse import urlencode

from .exceptions import FlightsNotFound
from .fetcher import (
    BOOKING_URL,
    CONSENT_COOKIE,
    FOLLOW_UP_TRIES,
    URL,
    _last_payload,
    _rpc_failure,
)
from .model import BookingOption
from .parser import ResultList, parse_booking_options, parse_payload
from .querying import Query

TIMEOUT = 60


class Browser:
    """One headless Chromium for a run of searches; close it, or use it in a `with` block."""

    def __init__(self) -> None:
        from playwright.sync_api import Error, sync_playwright

        self._playwright = sync_playwright().start()
        try:
            try:
                self._browser = self._playwright.chromium.launch()
            except Error as e:
                if "playwright install" not in str(e):
                    raise
                print("flights: installing Playwright's Chromium (once)…", file=sys.stderr)
                subprocess.run(
                    [sys.executable, "-m", "playwright", "install", "chromium-headless-shell"],
                    check=True,
                )
                self._browser = self._playwright.chromium.launch()
            # Google tells airlines the user agent; some answer "HeadlessChrome" differently.
            probe = self._browser.new_page()
            user_agent = probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            probe.close()
            context = self._browser.new_context(locale="en-US", user_agent=user_agent)
            context.add_cookies(
                [{"name": "SOCS", "value": CONSENT_COOKIE, "domain": ".google.com", "path": "/"}]
            )
            self._page = context.new_page()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "Browser":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if hasattr(self, "_browser"):
            self._browser.close()
        self._playwright.stop()

    def get_flights(self, q: Query) -> ResultList:
        """Search like `fetcher.get_flights`, with the browser's full results."""
        payload = self._page_payload(f"{URL}/search?{urlencode(q.params())}", "GetShoppingResults")
        flights = parse_payload(payload)
        flights.complete = _finished(payload)
        return flights

    def get_booking_options(self, q: Query) -> list[BookingOption]:
        """List the booking options like `fetcher.get_booking_options`; the page needs no token."""
        payload = self._page_payload(f"{BOOKING_URL}?{urlencode(q.params())}", "GetBookingResults")
        return parse_booking_options(payload)

    def _page_payload(self, url: str, method: str) -> list:
        """Load `url` and return the payload of the page's last `method` response."""

        responses = []
        listener = lambda r: method in r.url and responses.append(r)  # noqa: E731
        self._page.on("response", listener)
        try:
            # Even the web app is refused now and then; loading the page again usually works.
            for _ in range(FOLLOW_UP_TRIES):
                responses.clear()
                self._page.goto(url, wait_until="commit")
                deadline = time.monotonic() + TIMEOUT
                payload = error = None
                status = 0
                while time.monotonic() < deadline and error is None:
                    self._page.wait_for_timeout(200)
                    while responses:
                        response = responses.pop(0)
                        last, error = _last_payload(response.text())
                        status = response.status
                        payload = last or payload
                        # A booking page asks once; a search streams until it is finished.
                        if last is not None and (method != "GetShoppingResults" or _finished(last)):
                            return last
                if payload is not None:
                    return payload
            if error is not None or status:
                raise _rpc_failure(error, status)
            raise FlightsNotFound(f"Google Flights' page sent no {method} request in {TIMEOUT} s")
        finally:
            self._page.remove_listener("response", listener)


def _finished(payload: list) -> bool:
    """Only the snapshot of a finished search sets index 31."""
    return len(payload) > 31 and bool(payload[31])
