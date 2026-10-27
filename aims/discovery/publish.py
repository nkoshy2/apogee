"""Add a run to a feed folder and prune old runs. Standard library only.

    python -m aims.discovery.publish --run feed-out/run.json --feed feed/v1

Layout: <feed>/latest.json indexes <feed>/runs/<run_id>.json, oldest first, each with its
SHA-256 so the importer can verify what it downloads. Runs older than --keep-days are
removed, but the newest --keep-min runs are always kept.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aims.discovery.model import FEED_SCHEMA, FEED_VERSION, utc_now

INDEX_SCHEMA = "aims-feed-index"
RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def publish(run_path: Path, feed_dir: Path, keep_days: int = 14, keep_min: int = 4, now: datetime | None = None) -> dict:
    data = run_path.read_bytes()
    run = json.loads(data)
    if run.get("schema") != FEED_SCHEMA or run.get("version") != FEED_VERSION or not RUN_ID.match(str(run.get("run_id"))):
        raise ValueError("Not a valid feed run")
    runs_dir = feed_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    index_path = feed_dir / "latest.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"runs": []}

    name = f"{run['run_id']}.json"
    (runs_dir / name).write_bytes(data)
    entries = [e for e in index["runs"] if e["run_id"] != run["run_id"]]
    entries.append({"run_id": run["run_id"], "file": f"runs/{name}", "generated_at": run["generated_at"],
                    "sha256": hashlib.sha256(data).hexdigest()})
    entries.sort(key=lambda e: e["generated_at"])

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=keep_days)
    keep = [e for i, e in enumerate(entries) if _parse(e["generated_at"]) >= cutoff or i >= len(entries) - keep_min]
    kept_files = {Path(e["file"]).name for e in keep}
    for f in runs_dir.glob("*.json"):
        if f.name not in kept_files:
            f.unlink()

    index = {"schema": INDEX_SCHEMA, "version": FEED_VERSION, "updated_at": utc_now(), "runs": keep}
    index_path.write_text(json.dumps(index, indent=1), encoding="utf-8")
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("--feed", required=True, type=Path)
    ap.add_argument("--keep-days", type=int, default=14)
    args = ap.parse_args(argv)
    index = publish(args.run, args.feed, args.keep_days)
    print(f"Feed now holds {len(index['runs'])} runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
