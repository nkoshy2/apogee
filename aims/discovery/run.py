"""Check every enabled source once and produce one versioned feed run. Standard library only.

    python -m aims.discovery.run --out feed-out/run.json [--sources path] [--origin cloud]

The same code produces the cloud feed (GitHub Actions) and local "Run discovery here" runs,
so there is one import path. Its only inputs are the public sources list and the public job
boards; it cannot reach the local database or settings.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from aims.discovery import greenhouse, lever
from aims.discovery.model import FEED_SCHEMA, FEED_VERSION, HttpGet, SourceCheck, http_get_json, utc_now

SOURCES_PATH = Path(__file__).parent / "sources.json"
ADAPTERS = {"greenhouse": greenhouse.fetch, "lever": lever.fetch}
SOURCE_WORKERS = 4


def load_sources(path: Path = SOURCES_PATH) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = [s for s in data["sources"] if s.get("enabled", True)]
    for s in sources:
        if s["source"] not in ADAPTERS:
            raise ValueError(f"Unsupported source type: {s['source']}")
    return sources


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def check_source(spec: dict, http_get: HttpGet = http_get_json) -> SourceCheck:
    try:
        return ADAPTERS[spec["source"]](spec["key"], spec["employer"], http_get)
    except Exception as exc:  # an adapter bug must not hide the other sources' results
        return SourceCheck(spec["source"], spec["key"], spec["employer"], status="failed",
                           error=f"Unexpected error: {type(exc).__name__}: {exc}")


def run(sources: list[dict], origin: str, http_get: HttpGet = http_get_json) -> dict:
    with ThreadPoolExecutor(SOURCE_WORKERS) as pool:
        checks = list(pool.map(lambda s: check_source(s, http_get), sources))
    return {
        "schema": FEED_SCHEMA,
        "version": FEED_VERSION,
        "run_id": new_run_id(),
        "origin": origin,
        "generated_at": utc_now(),
        "checks": [c.as_dict() for c in checks],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sources", type=Path, default=SOURCES_PATH)
    ap.add_argument("--origin", default="cloud", choices=["cloud", "local"])
    args = ap.parse_args(argv)
    feed = run(load_sources(args.sources), args.origin)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(feed, ensure_ascii=False), encoding="utf-8")
    for c in feed["checks"]:
        state = "ok" if c["status"] == "ok" and c["complete"] else ("partial" if c["status"] == "ok" else "FAILED")
        print(f"{state:7} {c['source']}/{c['source_key']}: {len(c['postings'])} student roles"
              f" of {c['total_on_board']}{'  ' + c['error'] if c['error'] else ''}")
    # Failures are recorded in the feed itself; the run only exits non-zero if nothing worked.
    return 0 if any(c["status"] == "ok" for c in feed["checks"]) else 1


if __name__ == "__main__":
    sys.exit(main())
