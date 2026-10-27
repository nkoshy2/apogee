"""The canonical shapes every adapter produces, plus shared helpers. Standard library only."""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable

USER_AGENT = "Apogee/0.1 (personal internship tracker; one request per posting, twice a day)"
MAX_RESPONSE_BYTES = 60_000_000
FEED_SCHEMA, FEED_VERSION = "aims-feed", 1

# Student roles only: the feed tracks internships and co-ops, not every job on a board.
STUDENT_ROLE = re.compile(r"\b(intern|interns|internship|internships|co-?op|co-?ops|student)\b", re.I)
CO_OP = re.compile(r"\bco-?ops?\b", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Posting:
    """One opportunity, independent of the platform it came from."""

    source_posting_id: str
    title: str
    url: str
    location: str = ""
    opportunity_type: str = "internship"
    description: str = ""
    deadline: str | None = None          # YYYY-MM-DD if the source states one
    updated_at: str | None = None        # source's own timestamp, if any
    source_api_url: str = ""             # where this record was read from (audit reference)
    raw_sha256: str = ""                 # hash of the source's raw record (audit reference)


@dataclass
class SourceCheck:
    """The result of checking one board once. `complete` means every posting was seen."""

    source: str
    source_key: str
    employer: str
    status: str = "ok"                   # ok | failed
    complete: bool = False
    error: str | None = None
    fetched_at: str = field(default_factory=utc_now)
    total_on_board: int | None = None    # all jobs, before the student-role filter
    postings: list[Posting] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class FetchError(Exception):
    pass


HttpGet = Callable[[str], object]


def _read_bounded(resp, deadline: float, url: str) -> bytes:
    """Read a response in chunks, enforcing a total size cap and a wall-clock deadline.

    urllib's own timeout only bounds each socket read, so a server that trickles bytes
    could otherwise stall a run indefinitely.
    """
    chunks, size = [], 0
    while chunk := resp.read(65536):
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise FetchError(f"Response from {url} is larger than {MAX_RESPONSE_BYTES} bytes")
        if time.monotonic() > deadline:
            raise TimeoutError("response took too long to download")
        chunks.append(chunk)
    return b"".join(chunks)


def http_get_json(url: str, retries: int = 2, timeout: float = 30, total_timeout: float = 120) -> object:
    """GET a JSON document. 4xx fails at once; timeouts and 5xx are retried with backoff."""
    last = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = _read_bounded(resp, time.monotonic() + total_timeout, url)
            return json.loads(body)
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise FetchError(f"HTTP {exc.code} from {url}") from None
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = str(getattr(exc, "reason", exc))
        except json.JSONDecodeError:
            raise FetchError(f"Invalid JSON from {url}") from None
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise FetchError(f"{last} from {url} after {retries + 1} attempts")


def raw_hash(record: object) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# A season-and-year term in the title marks a fixed-term internship even when the title doesn't say
# "intern": e.g. Astranis' "Thermal Associate Engineer (Summer 2027)" is a 12-week internship for
# recent graduates (found by validation against the public boards, docs/validation/).
TERM_LABEL = re.compile(r"\b(spring|summer|fall|autumn|winter)(\s*[/&,]\s*(spring|summer|fall|autumn|winter))*\s+20\d\d\b", re.I)


def is_student_role(title: str, labels: list[str] = ()) -> bool:
    return (bool(STUDENT_ROLE.search(title)) or bool(TERM_LABEL.search(title))
            or any(STUDENT_ROLE.search(l or "") for l in labels))


def classify(title: str, labels: list[str] = ()) -> str:
    return "co-op" if CO_OP.search(title) or any(CO_OP.search(l or "") for l in labels) else "internship"


LOCATION_LIMIT = 1000  # multi-site postings list many cities; 300 characters cut some off (found in validation)


def clean_text(value: object, limit: int) -> str:
    """Collapse whitespace and cap length. A cut is marked with an ellipsis, never silent."""
    text = re.sub(r"[ \t\r\f\v]+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(markup: str, limit: int = 100_000) -> str:
    """Untrusted HTML to plain text (no markup survives). Handles Greenhouse's double-escaped HTML."""
    if "&lt;" in markup and "<" not in markup:
        markup = html.unescape(markup)
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:limit]


def iso_date(value: str | None) -> str | None:
    """YYYY-MM-DD from an ISO timestamp or date, else None."""
    if not value:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value))
    return m.group(1) if m else None
