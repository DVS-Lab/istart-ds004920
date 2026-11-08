import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from harmonized.common import ROOT, write_json

spec = importlib.util.spec_from_file_location("publisher", ROOT / "code/publish_processing_status.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def test_status_allowlist_and_redaction_preserve_valid_json(tmp_path):
    write_json(tmp_path / "config/linux2.json", {})
    batch = tmp_path / "work/preprocessing-runs/pilot"
    write_json(batch / "state.json", {"name": "pilot", "status": "complete", "failed": 0, "ignored_secret": "not included"})
    write_json(batch / "spec.json", {"jobs": 1, "nprocs": 4, "mem_mb": 0, "ignored_secret": "not included"})
    (batch / "results.tsv").write_text("subject\tstatus\treason\nsub-1002\tfailed\tAuthorization: Bearer ghp_testcredential\n")
    logs = tmp_path / "logs/fmriprep"
    logs.mkdir(parents=True)
    (logs / "sub-1002.log").write_text("fMRIPrep finished successfully!\nhttps://example.org?token=private\nAuthorization: Bearer private\n/home/person/private\n")
    (tmp_path / "license.txt").write_text("not included")
    (tmp_path / "derivatives").mkdir()
    (tmp_path / "derivatives/private-image.nii.gz").write_text("not included")
    (logs / "sub-secret.log").symlink_to(tmp_path / "license.txt")
    payload = publisher.collect(tmp_path)
    assert set(payload) == {"README.txt", "status.json", "log-tails.txt"}
    report = json.loads(payload["status.json"])
    assert report["preprocessing_batches"][0]["status"] == "complete"
    text = "\n".join(payload.values())
    assert "not included" not in text and "testcredential" not in text and "token=private" not in text
    assert "Bearer private" not in text and "/home/person" not in text
    assert "fMRIPrep finished successfully!" in text


def test_real_git_status_push_preserves_main_and_is_fast_forward(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    blob = publisher.git(remote, ["hash-object", "-w", "--stdin"], "existing source must be preserved")
    tree = publisher.git(remote, ["mktree"], f"100644 blob {blob}\tREADME.md\n")
    initial = publisher.git(remote, ["commit-tree", tree], "Initial source\n")
    publisher.git(remote, ["update-ref", "refs/heads/main", initial])
    root = tmp_path / "production"
    root.mkdir()
    sentinel = root / "production-index-sentinel"
    sentinel.write_text("unchanged")
    payload = {"README.txt": "test status", "status.json": '{"status":"running"}', "log-tails.txt": "test log"}
    first = publisher.publish(root, payload, remote=str(remote))
    payload["status.json"] = '{"status":"complete"}'
    second = publisher.publish(root, payload, remote=str(remote))
    def git(*args):
        return subprocess.run(["git", "--git-dir", str(remote), *args], check=True, capture_output=True, text=True).stdout.strip()
    assert git("rev-parse", "refs/heads/main") == second
    assert git("rev-parse", second + "^") == first
    assert json.loads(git("show", second + ":status/linux2/status.json"))["status"] == "complete"
    assert git("show", second + ":README.md") == "existing source must be preserved"
    assert set(git("ls-tree", "--name-only", second + ":status/linux2").splitlines()) == set(payload)
    assert git("for-each-ref", "--format=%(refname)", "refs/heads") == "refs/heads/main"
    assert sentinel.read_text() == "unchanged" and not (root / ".git").exists()
    with pytest.raises(ValueError, match="allowlist"):
        publisher.publish(root, {"license.txt": "never upload"}, remote=str(remote))


def test_l1_watcher_publishes_final_snapshot_and_exits(tmp_path, monkeypatch):
    state = tmp_path / "work/l1-runs/pilot/state.json"
    write_json(state, {"status": "complete", "worker_script": "run_harmonized_l1.py"})
    write_json(tmp_path / "work/github-status/state.json", {"batch": "l1-pilot", "target_branch": "main"})
    monkeypatch.setattr(publisher, "ROOT", tmp_path)
    monkeypatch.setattr(publisher.sys, "argv", ["publisher", "watch", "--root", str(tmp_path), "--batch", "l1-pilot", "--run-state", str(state)])
    calls = []
    monkeypatch.setattr(publisher, "collect", lambda root: {"status.json": "{}"})
    def publish(root, payload):
        calls.append((root, payload))
        return "final-status-commit"
    monkeypatch.setattr(publisher, "publish", publish)
    assert publisher.main() == 0 and len(calls) == 1
    saved = json.loads((tmp_path / "work/github-status/state.json").read_text())
    assert saved["status"] == "finished" and saved["last_commit"] == "final-status-commit"


def test_rolling_progress_deduplicates_pilots_and_uses_latest_resume_status(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    plan = {"jobs": [{"unit": unit, "dataset": dataset} for unit, dataset in
                    (("a_seed-left", "istart"), ("b_seed-left", "rf1"), ("c_seed-left", "rf1"), ("d_seed-left", "rf1"))]}
    (logs / "harmonized-l1-pilot.log").write_text("a_seed-left verified\na_seed-left verified\n")
    (logs / "harmonized-l1-cohort.log").write_text("a_seed-left verified\nb_seed-left review_or_failure old failure\nb_seed-left verified\nc_seed-left review_or_failure smoothing outside band\nunknown_seed-x verified\n")
    progress = publisher.rolling_model_progress(tmp_path, plan)
    assert (progress["planned"], progress["verified"], progress["failed_or_held"], progress["remaining_or_running"]) == (4, 2, 1, 1)
    assert progress["percent_verified"] == 50
    assert progress["by_dataset"]["istart"]["verified"] == 1
    assert progress["by_dataset"]["rf1"]["remaining_or_running"] == 1
    assert progress["issues"] == [{"unit": "c_seed-left", "dataset": "rf1", "reason": "smoothing outside band"}]
