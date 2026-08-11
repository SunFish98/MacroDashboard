#!/usr/bin/env python3
"""
Refresh the cache/ snapshots from a residential IP and (optionally) push.

Truth Social and CME FedWatch block datacenter IPs, so cloud deployments
fall back to the JSON snapshots committed in cache/. Run this script on a
home machine (e.g. via the launchd job in scripts/) to keep those
snapshots fresh; each push triggers the cloud redeploy automatically.

Usage:
    python3 scripts/refresh_snapshots.py           # fetch + write only
    python3 scripts/refresh_snapshots.py --push    # + git commit & push

Only writes a snapshot when a LIVE fetch succeeds — a failed fetch never
overwrites the last good snapshot. Only commits when file contents changed.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from data_fetchers import FedWatchFetcher, TruthSocialFetcher  # noqa: E402

SNAPSHOT_FILES = ["cache/truthsocial.json", "cache/fedwatch.json"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("refresh-snapshots")


def _write_json(rel_path: str, data) -> None:
    path = REPO / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def refresh_truthsocial() -> bool:
    """Fetch posts LIVE (no cache fallback); write snapshot on success."""
    fetcher = TruthSocialFetcher()
    posts = fetcher._try_mastodon_api("realDonaldTrump", 20)
    if not posts:
        posts = fetcher._try_rss("realDonaldTrump", 20)
    if not posts:
        logger.warning("Truth Social live fetch failed — keeping existing snapshot.")
        return False
    _write_json("cache/truthsocial.json", posts)
    logger.info("Truth Social snapshot updated (%d posts).", len(posts))
    return True


def refresh_fedwatch() -> bool:
    """Fetch CME probabilities LIVE; write snapshot on success."""
    fetcher = FedWatchFetcher()
    prob = fetcher._try_cme_api()
    if not prob or not prob.get("probabilities"):
        logger.warning("CME FedWatch live fetch failed — keeping existing snapshot.")
        return False
    snapshot = {
        "meetings": fetcher.fetch_fomc_meetings(),
        "current_rate": prob.get("current_rate"),
        "probabilities": prob.get("probabilities"),
        "source_note": (
            "Data from CME FedWatch API (snapshot refreshed "
            + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            + "). Verify at: " + fetcher.CME_FEDWATCH_URL
        ),
    }
    _write_json("cache/fedwatch.json", snapshot)
    logger.info("FedWatch snapshot updated (%d meetings with probabilities).",
                len(snapshot["probabilities"]))
    return True


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True
    )


def commit_and_push() -> None:
    status = _git("status", "--porcelain", "--", *SNAPSHOT_FILES)
    if not status.stdout.strip():
        logger.info("Snapshots unchanged — nothing to commit.")
        return
    _git("add", "--", *SNAPSHOT_FILES)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit = _git("commit", "-m", f"Update cache snapshots ({stamp})")
    if commit.returncode != 0:
        logger.error("git commit failed:\n%s%s", commit.stdout, commit.stderr)
        return
    push = _git("push")
    if push.returncode != 0:
        logger.error("git push failed (will retry next run):\n%s%s",
                     push.stdout, push.stderr)
        # Undo the local commit so the next run re-commits cleanly on top
        # of whatever the remote looks like by then.
        _git("reset", "--soft", "HEAD~1")
        return
    logger.info("Snapshots committed and pushed — cloud redeploy will pick them up.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--push", action="store_true",
                        help="git commit + push the snapshots if they changed")
    args = parser.parse_args()

    updated_any = refresh_truthsocial() | refresh_fedwatch()
    if args.push:
        commit_and_push()
    elif updated_any:
        logger.info("Snapshots written. Re-run with --push to publish them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
