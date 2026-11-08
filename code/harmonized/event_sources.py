"""Pinned original encoding streams for ten empty published iSTART event tables."""
import hashlib
import json
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

from .common import lock, read_table, stamp


def git_blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def resolve(c, subject, task, canonical):
    root = Path(c["istart_root"])
    policy = root / "config/istart_event_sources.json"
    if not policy.is_file():
        return canonical, {"events_source": "published_BIDS"}
    sources = json.loads(policy.read_text())["sources"]
    entry = next((r for r in sources if r["subject"] == subject and r["task"] == task), None)
    if entry is None:
        return canonical, {"events_source": "published_BIDS"}
    _, rows = read_table(canonical)
    if rows:
        return canonical, {"events_source": "published_BIDS"}
    if git_blob(Path(canonical).read_bytes()) != entry["canonical_git_blob_sha"]:
        raise ValueError("Empty published event table differs from the audited pinned source")
    url = f"https://raw.githubusercontent.com/{entry['repository']}/{entry['commit']}/{quote(entry['path'], safe='/')}"
    target = root / f"work/behavioral-event-sources/sub-{subject}_task-{task}_events.tsv"
    with lock(root / "work/behavioral-event-sources/acquisition.lock"):
        if target.is_file():
            data = target.read_bytes()
        else:
            with urlopen(url, timeout=60) as response:
                data = response.read()
            if git_blob(data) != entry["git_blob_sha"]:
                raise ValueError("Downloaded event source does not match its pinned Git blob")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
        if git_blob(data) != entry["git_blob_sha"]:
            raise ValueError("Cached original events changed; existing file preserved")
    _, records = read_table(target)
    decisions = [r for r in records if r.get("trial_type") in ("decision", "decision-missed")]
    if len(decisions) != entry["expected_decisions"]:
        raise ValueError("Original encoding stream has an unexpected decision count")
    return target, {"events_source": "pinned_original_encoding_stream_published_BIDS_empty", "events_source_url": url,
                    "events_source_git_blob": entry["git_blob_sha"], "published_events": str(canonical),
                    "published_events_stamp": stamp(canonical), "event_source_policy_stamp": stamp(policy)}
