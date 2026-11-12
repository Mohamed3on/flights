"""Search Google Flights in a headless Chromium, as its web app does.

Google takes plain results requests for a bot: it refuses them (error 13) or
answers with fewer, pricier itineraries and booking options without fare names.
The web app's own requests carry an `x-goog-batchexecute-bgr` header, a BotGuard
token minted in the page for that very request, so this loads the results or
booking page and reads the page's own GetShoppingResults or GetBookingResults
response. Slower than plain requests (about 3–7 s a page), but it gets what
Chrome gets.
"""

import platform
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

        # Google serves a browser it takes for a bot a shorter, pricier list instead of
        # refusing it: for BER⇄BKK, 28 itineraries with SWISS at €2,881 where Chrome
        # lists 126 with SWISS at €2,739 and Hainan at €2,191. Playwright's headless
        # shell always gets that list; full Chromium in new headless mode gets Chrome's,
        # once it hides automation and calls itself Google Chrome (see `_as_chrome`).
        launch = lambda: self._playwright.chromium.launch(  # noqa: E731
            channel="chromium", args=["--disable-blink-features=AutomationControlled"]
        )
        self._playwright = sync_playwright().start()
        try:
            try:
                self._browser = launch()
            except Error as e:
                if "playwright install" not in str(e):
                    raise
                print("flights: installing Playwright's Chromium (once)…", file=sys.stderr)
                subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
                self._browser = launch()
            context = self._browser.new_context(locale="en-US")
            context.add_cookies(
                [{"name": "SOCS", "value": CONSENT_COOKIE, "domain": ".google.com", "path": "/"}]
            )
            self._page = context.new_page()
            _as_chrome(self._page, self._browser.version)
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


def _as_chrome(page, version: str) -> None:
    """Make `page` present itself as this machine's Google Chrome, in its user agent and client hints."""
    cdp = page.context.new_cdp_session(page)
    user_agent = cdp.send("Browser.getVersion")["userAgent"].replace("HeadlessChrome", "Chrome")
    os_name, os_version = {
        "darwin": ("macOS", platform.mac_ver()[0]),
        "win32": ("Windows", platform.version()),
    }.get(sys.platform, ("Linux", ""))
    brands = [("Google Chrome", version), ("Chromium", version), ("Not.A/Brand", "24.0.0.0")]
    cdp.send(
        "Network.setUserAgentOverride",
        {
            "userAgent": user_agent,
            "userAgentMetadata": {
                "brands": [{"brand": b, "version": v.split(".")[0]} for b, v in brands],
                "fullVersionList": [{"brand": b, "version": v} for b, v in brands],
                "fullVersion": version,
                "platform": os_name,
                "platformVersion": os_version,
                "architecture": "arm" if platform.machine() in ("arm64", "aarch64") else "x86",
                "bitness": "64",
                "model": "",
                "mobile": False,
                "wow64": False,
            },
        },
    )


def _finished(payload: list) -> bool:
    """Only the snapshot of a finished search sets index 31."""
    return len(payload) > 31 and bool(payload[31])
