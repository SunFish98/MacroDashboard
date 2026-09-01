#!/usr/bin/env python3
"""
Refresh the cache/ snapshots from a residential IP and (optionally) push.

Truth Social and CME FedWatch block datacenter IPs, so cloud deployments
fall back to the JSON snapshots committed in cache/. Run this script on a
home machine (e.g. via the launchd job in scripts/) to keep those
snapshots fresh; each push triggers the cloud redeploy automatically.

Usage:
    python3 scripts/refresh_snapshots.py           # fetch + write only
    python3 scripts/refresh_snapshots.py --push    # + publish to snapshots branch

Only writes a snapshot when a LIVE fetch succeeds — a failed fetch never
overwrites the last good snapshot.

IMPORTANT — publishing never touches the code branch. Snapshots are
force-pushed as a single orphan commit to config.SNAPSHOT_BRANCH: a push
to a deployed branch (e.g. main) triggers a container rebuild, and hourly
rebuilds pile up Artifact Registry storage + egress cost. The deployed app
reads the snapshots at runtime from config.SNAPSHOT_REMOTE_BASE instead,
so no redeploy is needed for data updates.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import config  # noqa: E402
from data_fetchers import FedWatchFetcher, TruthSocialFetcher  # noqa: E402
from memory_monitor import MemoryMonitorFetcher  # noqa: E402

SNAPSHOT_FILES = [
    "cache/truthsocial.json",
    "cache/fedwatch.json",
    "cache/memory_history.json",
]

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


def refresh_memory_prices() -> bool:
    """Scrape DRAMeXchange and append today's spot prices to the history
    file that publish_snapshots() ships to the snapshots branch."""
    fetcher = MemoryMonitorFetcher()
    try:
        # Merge the published history first: if this clone is fresh, its
        # local file is only the committed seed, and publishing that would
        # force-push a regressed history over everything accumulated so far.
        fetcher.merge_remote_history()
    except Exception as exc:
        logger.warning("Remote memory history merge failed: %s", exc)
    try:
        prices = fetcher.fetch_spot_prices()
    except Exception as exc:
        logger.warning("DRAMeXchange fetch failed — keeping existing history: %s", exc)
        return False
    if not prices:
        logger.warning("DRAMeXchange scrape returned no rows — keeping existing history.")
        return False
    from datetime import datetime as _dt
    today = _dt.now(timezone.utc).strftime("%Y-%m-%d")
    added = sum(
        fetcher.store.append_observations(sid, {today: value})
        for sid, value in prices.items()
    )
    fetcher.store.save()
    logger.info("Memory spot history: %d series scraped, %d new observations.",
                len(prices), added)
    return added > 0


def _git(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, env=env
    )


def publish_snapshots() -> None:
    """Force-push the snapshot files as a single orphan commit to the
    dedicated snapshots branch. The working branch and index are untouched
    (a scratch GIT_INDEX_FILE is used), the branch always holds exactly one
    commit so the repo never grows from data updates, and — critically —
    no deployed branch is pushed, so no cloud rebuild is triggered."""
    files = [f for f in SNAPSHOT_FILES if (REPO / f).exists()]
    if not files:
        logger.info("No snapshot files exist yet — nothing to publish.")
        return

    env = os.environ.copy()
    scratch_index = REPO / ".git" / "snapshots-index"
    env["GIT_INDEX_FILE"] = str(scratch_index)
    try:
        for step in (("read-tree", "--empty"), ("add", "-f", "--", *files)):
            res = _git(*step, env=env)
            if res.returncode != 0:
                logger.error("git %s failed:\n%s%s", step[0], res.stdout, res.stderr)
                return
        tree = _git("write-tree", env=env)
        if tree.returncode != 0:
            logger.error("git write-tree failed:\n%s%s", tree.stdout, tree.stderr)
            return
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        commit = _git(
            "commit-tree", tree.stdout.strip(), "-m", f"Data snapshots ({stamp})",
            env=env,
        )
        if commit.returncode != 0:
            logger.error("git commit-tree failed:\n%s%s", commit.stdout, commit.stderr)
            return
        push = _git(
            "push", "--force", "origin",
            f"{commit.stdout.strip()}:refs/heads/{config.SNAPSHOT_BRANCH}",
        )
        if push.returncode != 0:
            logger.error("git push failed (will retry next run):\n%s%s",
                         push.stdout, push.stderr)
            return
        logger.info(
            "Published snapshots to '%s' branch — served to the app via %s "
            "(no rebuild triggered).",
            config.SNAPSHOT_BRANCH, config.SNAPSHOT_REMOTE_BASE,
        )
    finally:
        scratch_index.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--push", action="store_true",
                        help="publish the snapshots to the snapshots branch")
    args = parser.parse_args()

    updated_any = refresh_truthsocial() | refresh_fedwatch() | refresh_memory_prices()
    if args.push:
        if updated_any:
            publish_snapshots()
        else:
            logger.info("No live fetch succeeded — nothing new to publish.")
    elif updated_any:
        logger.info("Snapshots written. Re-run with --push to publish them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
