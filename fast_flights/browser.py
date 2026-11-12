"""Search Google Flights in a headless Chromium, as its web app does.

Google takes plain results requests for a bot: it refuses them (error 13) or
answers with fewer, pricier itineraries and booking options without fare names.
The web app's own requests carry an `x-goog-batchexecute-bgr` header, a BotGuard
token minted in the page for that very request, so this loads the results or
booking page and reads the page's own GetShoppingResults or GetBookingResults
response. About 3–4 s a search, but it gets what Chrome gets; several pages search at
once, and a loaded page searches again without reloading, so a grid of 40
searches takes about 30 s.
"""

import platform
import re
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
JOBS = 6
# Pictures, fonts and media only slow the page down: the results come as data.
MEDIA = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|woff2?|ttf|otf|mp4|webm)(\?|$)")


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
            self._context = self._browser.new_context(locale="en-US")
            self._context.add_cookies(
                [{"name": "SOCS", "value": CONSENT_COOKIE, "domain": ".google.com", "path": "/"}]
            )
            self._context.route(MEDIA, lambda route: route.abort())
            self._pages = []
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
        return _raised(self.search_many([q])[0])

    def search_many(self, queries: list[Query], jobs: int = JOBS) -> list[ResultList | Exception]:
        """Run the searches `jobs` pages at a time; each gives its results or its error."""
        urls = [f"{URL}/search?{urlencode(q.params())}" for q in queries]
        return [
            p if isinstance(p, Exception) else _results(p)
            for p in self._payloads(urls, "GetShoppingResults", jobs)
        ]

    def get_booking_options(self, q: Query) -> list[BookingOption]:
        """List the booking options like `fetcher.get_booking_options`; the page needs no token."""
        url = f"{BOOKING_URL}?{urlencode(q.params())}"
        return parse_booking_options(_raised(self._payloads([url], "GetBookingResults", 1)[0]))

    def _payloads(self, urls: list[str], method: str, jobs: int) -> list[list | Exception]:
        """Load each URL in one of `jobs` pages and return the payload of its `method` response."""
        while len(self._pages) < min(jobs, len(urls)):
            page = self._context.new_page()
            _as_chrome(page, self._browser.version)
            self._pages.append(page)
        payloads: list[list | Exception | None] = [None] * len(urls)
        todo = list(enumerate(urls))[::-1]
        idle = self._pages[:jobs]
        loading: list[_Load] = []
        try:
            while todo or loading:
                while todo and idle:
                    i, url = todo.pop()
                    loading.append(_Load(idle.pop(), url, method, i))
                # Waiting on one page lets Playwright deliver every page's responses.
                self._pages[0].wait_for_timeout(100)
                for load in loading[:]:
                    try:
                        payload = load.poll()
                    except Exception as e:
                        payload = e
                    if payload is not None:
                        payloads[load.index] = payload
                        load.stop()
                        loading.remove(load)
                        idle.append(load.page)
        finally:
            for load in loading:
                load.stop()
        return payloads


class _Load:
    """One page loading `url` until the page's `method` response is done, reloading if refused."""

    def __init__(self, page, url: str, method: str, index: int) -> None:
        self.page, self.url, self.method, self.index = page, url, method, index
        self.responses: list = []
        self.tries = 0
        # Only finished requests: reading a streaming response would block every other page.
        self._listener = lambda r: method in r.url and self.responses.append(r)  # noqa: E731
        page.on("requestfinished", self._listener)
        self._start()

    def _start(self) -> None:
        # Even the web app is refused now and then; loading the page again usually works.
        self.tries += 1
        self.responses.clear()
        self.payload = self.error = None
        self.status = 0
        self.deadline = time.monotonic() + TIMEOUT
        reusable = self.method == "GetShoppingResults" and self.page.url.startswith(f"{URL}/search")
        if self.tries == 1 and reusable:
            # The loaded web app searches again when its URL changes, which spares the
            # page load: most of a search's time, and of the CPU that limits parallel pages.
            self.page.evaluate(
                "url => { history.pushState({}, '', url); dispatchEvent(new PopStateEvent('popstate')) }",
                self.url,
            )
        else:
            self.page.goto(self.url, wait_until="commit")

    def poll(self) -> list | None:
        """The payload once it is done, None while it isn't; raises when the page gave up."""
        while self.responses and self.error is None:
            response = self.responses.pop(0).response()
            last, self.error = _last_payload(response.text())
            self.status = response.status
            self.payload = last or self.payload
            # A booking page asks once; a search streams until it is finished.
            if last is not None and (self.method != "GetShoppingResults" or _finished(last)):
                return last
        if self.error is None and time.monotonic() < self.deadline:
            return None
        if self.payload is not None:
            return self.payload
        if self.tries < FOLLOW_UP_TRIES:
            self._start()
            return None
        if self.error is not None or self.status:
            raise _rpc_failure(self.error, self.status)
        raise FlightsNotFound(f"Google Flights' page sent no {self.method} request in {TIMEOUT} s")

    def stop(self) -> None:
        self.page.remove_listener("requestfinished", self._listener)


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


def _raised(result):
    if isinstance(result, Exception):
        raise result
    return result


def _results(payload: list) -> ResultList:
    flights = parse_payload(payload)
    flights.complete = _finished(payload)
    return flights


def _finished(payload: list) -> bool:
    """Only the snapshot of a finished search sets index 31."""
    return len(payload) > 31 and bool(payload[31])
