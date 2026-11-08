import io

import pytest

from harmonized import event_sources
from harmonized.common import write_json
from harmonized.inputs import events


def policy(tmp_path, source):
    canonical = tmp_path / "events.tsv"
    canonical.write_text("onset\tduration\ttrial_type\n")
    write_json(tmp_path / "config/istart_event_sources.json", {"sources": [{
        "subject": "1011", "task": "doors", "repository": "DVS-Lab/istart", "commit": "pinned",
        "path": "source.tsv", "git_blob_sha": event_sources.git_blob(source),
        "canonical_git_blob_sha": event_sources.git_blob(canonical.read_bytes()), "expected_decisions": 1}]})
    return canonical


def test_empty_events_use_pinned_source_without_editing_bids(tmp_path, monkeypatch):
    source = b"onset\tduration\ttrial_type\n1\t3\tdecision\n5\t1\twin\n10\t1\tloss\n"
    canonical = policy(tmp_path, source)
    original = canonical.read_bytes()
    monkeypatch.setattr(event_sources, "urlopen", lambda *args, **kwargs: io.BytesIO(source))
    selected, provenance = event_sources.resolve({"istart_root": str(tmp_path)}, "1011", "doors", canonical)
    assert selected != canonical and selected.read_bytes() == source and canonical.read_bytes() == original
    assert provenance["events_source"] == "pinned_original_encoding_stream_published_BIDS_empty"
    assert events(selected, 20)[0]["decision"] == [[1, 3, 1]]


def test_nonempty_published_events_take_precedence(tmp_path, monkeypatch):
    canonical = policy(tmp_path, b"unused")
    canonical.write_text("onset\tduration\ttrial_type\n1\t3\tdecision\n")
    monkeypatch.setattr(event_sources, "urlopen", lambda *args, **kwargs: pytest.fail("No download for nonempty published events"))
    selected, provenance = event_sources.resolve({"istart_root": str(tmp_path)}, "1011", "doors", canonical)
    assert selected == canonical and provenance["events_source"] == "published_BIDS"


def test_changed_download_is_rejected_before_writing(tmp_path, monkeypatch):
    canonical = policy(tmp_path, b"expected")
    monkeypatch.setattr(event_sources, "urlopen", lambda *args, **kwargs: io.BytesIO(b"unexpected"))
    with pytest.raises(ValueError, match="pinned Git blob"):
        event_sources.resolve({"istart_root": str(tmp_path)}, "1011", "doors", canonical)
    assert not (tmp_path / "work/behavioral-event-sources/sub-1011_task-doors_events.tsv").exists()


def test_clock_reset_cannot_merge_incompatible_attempts(tmp_path):
    path = tmp_path / "events.tsv"
    path.write_text("onset\tduration\ttrial_type\n1\t3\tdecision\n5\t1\twin\n10\t1\tloss\n0\t3\tdecision\n")
    with pytest.raises(ValueError, match="clock reset"):
        events(path, 20)
