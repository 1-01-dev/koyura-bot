"""Unit tests for the engine's download queue and its estimate model.

Everything here is deterministic: the queue takes an injectable clock and the
runner is a stub, so FIFO order, admission refusals, expiry and the EWMA are
checked without threads, yt-dlp or a network.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import download_queue  # noqa: E402
from download_queue import (  # noqa: E402
    Estimator,
    JobQueue,
    Limits,
    clamp_estimate,
)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_queue(limits: Limits, clock: Clock | None = None, started=None, expired=None):
    """A queue whose runner only records that a job started."""
    clock = clock or Clock()
    started = started if started is not None else []
    queue = JobQueue(
        limits,
        lambda job_id: started.append(job_id),
        clock=clock,
        on_expire=(lambda job_id, message: expired.append((job_id, message))) if expired is not None else None,
    )
    return queue, started


def fast_estimator(queue: JobQueue) -> None:
    """Fixed, quick estimates so the admission maths is easy to read."""
    queue.estimator.record("video", None, 60.0)  # 60 s per job, no size


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

class TestLimits:
    def test_defaults_match_the_documented_policy(self, monkeypatch):
        for name in (
            "MAX_CONCURRENT_DOWNLOADS",
            "PER_USER_MAX_ACTIVE",
            "PER_USER_MAX_QUEUED",
            "MAX_QUEUE_LENGTH",
            "QUEUE_MAX_WAIT_SECONDS",
            "QUEUE_ESTIMATE_MBPS",
            "QUEUE_ESTIMATE_OVERHEAD_SECONDS",
            "QUEUE_EXPIRY_SECONDS",
        ):
            monkeypatch.delenv(name, raising=False)
        limits = Limits.from_env()
        assert limits.max_concurrent == 2
        assert limits.per_user_active == 1
        assert limits.per_user_queued == 1
        assert limits.max_queue_length == 6
        assert limits.max_wait_seconds == 120
        assert limits.estimate_mbps == 2.0
        assert limits.estimate_overhead_seconds == 6.0
        assert limits.expiry_seconds == 120

    def test_env_overrides_are_read(self, monkeypatch):
        monkeypatch.setenv("MAX_CONCURRENT_DOWNLOADS", "4")
        monkeypatch.setenv("PER_USER_MAX_ACTIVE", "2")
        monkeypatch.setenv("MAX_QUEUE_LENGTH", "12")
        monkeypatch.setenv("QUEUE_MAX_WAIT_SECONDS", "180")
        monkeypatch.setenv("QUEUE_ESTIMATE_MBPS", "6.5")
        limits = Limits.from_env()
        assert limits.max_concurrent == 4
        assert limits.per_user_active == 2
        assert limits.max_queue_length == 12
        assert limits.max_wait_seconds == 180
        assert limits.estimate_mbps == 6.5

    @pytest.mark.parametrize(
        "payload,expected_error",
        [
            ({"max_concurrent": 0}, "between 1 and 8"),
            ({"max_concurrent": 9}, "between 1 and 8"),
            ({"max_queue_length": 51}, "between 0 and 50"),
            ({"max_wait_seconds": 10}, "between 15 and 900"),
            ({"estimate_mbps": 0.1}, "between 0.2 and 50"),
            ({"per_user_queued": 4}, "between 0 and 3"),
        ],
    )
    def test_parse_rejects_out_of_range_values(self, payload, expected_error):
        _, errors = Limits.parse(payload, base=Limits())
        assert errors and expected_error in errors[0]

    def test_per_user_active_is_bounded_by_the_slot_count(self):
        _, errors = Limits.parse({"per_user_active": 3}, base=Limits(max_concurrent=2))
        assert errors and "between 1 and 2" in errors[0]
        limits, errors = Limits.parse({"per_user_active": 2}, base=Limits(max_concurrent=2))
        assert not errors
        assert limits.per_user_active == 2

    def test_parse_accepts_env_style_aliases(self):
        limits, errors = Limits.parse({"MAX_CONCURRENT_DOWNLOADS": 4, "QUEUE_MAX_WAIT_SECONDS": 200})
        assert not errors
        assert limits.max_concurrent == 4
        assert limits.max_wait_seconds == 200

    def test_unknown_and_non_numeric_values_are_reported(self):
        _, errors = Limits.parse({"nope": 1, "max_concurrent": "many"})
        assert any("Unknown setting" in error for error in errors)
        assert any("must be a number" in error for error in errors)
        # A rejected payload leaves the previous limits untouched.
        unchanged, errors = Limits.parse({"max_concurrent": 0}, base=Limits(max_concurrent=2))
        assert errors
        assert unchanged.max_concurrent == 2


# ---------------------------------------------------------------------------
# Estimates
# ---------------------------------------------------------------------------

class TestEstimator:
    def test_fallback_is_flagged_approximate(self):
        estimator = Estimator(default_mbps=2.0, overhead_seconds=6.0)
        seconds, approximate = estimator.estimate("video", 40 * 1024 * 1024)
        assert approximate is True
        assert seconds == pytest.approx(6.0 + 20.0)  # 40 MB at 2 MB/s

    def test_measurements_make_the_estimate_concrete(self):
        estimator = Estimator(default_mbps=2.0, overhead_seconds=6.0)
        estimator.record("video", 60 * 1024 * 1024, 30.0)  # 2 MB/s
        seconds, approximate = estimator.estimate("video", 60 * 1024 * 1024)
        assert approximate is False
        assert seconds == pytest.approx(36.0, abs=0.5)

    def test_ewma_moves_towards_the_new_sample(self):
        estimator = Estimator(default_mbps=2.0, overhead_seconds=0.0, alpha=0.5)
        estimator.record("video", None, 100.0)
        estimator.record("video", None, 20.0)
        duration, _ = estimator.estimate("video")
        # 100 * 0.5 + 20 * 0.5 == 60
        assert duration == pytest.approx(60.0, abs=1.0)

    def test_unknown_size_uses_the_measured_duration(self):
        estimator = Estimator(default_mbps=2.0, overhead_seconds=6.0)
        estimator.record("audio", 10 * 1024 * 1024, 25.0)
        seconds, approximate = estimator.estimate("audio")
        assert approximate is True  # no size given -> still a guess
        assert seconds == pytest.approx(25.0, abs=1.0)

    def test_estimates_are_clamped_to_the_documented_range(self):
        assert clamp_estimate(0.1) == download_queue.ESTIMATE_MIN_SECONDS
        assert clamp_estimate(5000) == download_queue.ESTIMATE_MAX_SECONDS

        estimator = Estimator(default_mbps=0.001, overhead_seconds=0.0)
        seconds, _ = estimator.estimate("video", 10_000 * 1024 * 1024)
        assert seconds == download_queue.ESTIMATE_MAX_SECONDS

    def test_kinds_are_tracked_separately(self):
        estimator = Estimator(default_mbps=2.0, overhead_seconds=0.0, alpha=1.0)
        estimator.record("video", None, 120.0)
        estimator.record("audio", None, 12.0)
        assert estimator.estimate("video")[0] == pytest.approx(120.0)
        assert estimator.estimate("audio")[0] == pytest.approx(12.0)

    def test_stats_survive_a_restart(self, tmp_path):
        path = str(tmp_path / "stats.json")
        first = Estimator(path, default_mbps=2.0, overhead_seconds=6.0, alpha=1.0)
        first.record("video", 50 * 1024 * 1024, 25.0)
        first.record("audio", 5 * 1024 * 1024, 10.0)
        expected_video = first.estimate("video", 50 * 1024 * 1024)
        expected_audio = first.estimate("audio", 5 * 1024 * 1024)

        # A brand new engine process reads the same file.
        second = Estimator(path, default_mbps=2.0, overhead_seconds=6.0)
        assert second.estimate("video", 50 * 1024 * 1024) == pytest.approx(expected_video)
        assert second.estimate("audio", 5 * 1024 * 1024) == pytest.approx(expected_audio)
        assert second.estimate("video", 50 * 1024 * 1024)[1] is False

    def test_corrupt_stats_file_is_ignored_not_fatal(self, tmp_path):
        path = tmp_path / "stats.json"
        path.write_text("{not json")
        estimator = Estimator(str(path), default_mbps=2.0, overhead_seconds=6.0)
        assert estimator.snapshot() == {}
        assert estimator.estimate("video", 10 * 1024 * 1024)[0] > 0

    def test_stats_path_never_lives_in_the_downloads_directory(self, tmp_path, monkeypatch):
        monkeypatch.delenv("QUEUE_STATS_PATH", raising=False)
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        monkeypatch.setenv("KOYURA_STATE_DIR", str(downloads))
        path = download_queue.default_stats_path(str(downloads))
        # Even when pointed at the purgeable directory, the file moves aside.
        assert os.path.dirname(path) != str(downloads)
        assert os.path.exists(os.path.dirname(path))

    def test_stats_path_honours_the_env_override(self, tmp_path, monkeypatch):
        target = tmp_path / "queue.json"
        monkeypatch.setenv("QUEUE_STATS_PATH", str(target))
        assert download_queue.default_stats_path(str(tmp_path)) == str(target)

    def test_stats_file_holds_no_user_data(self, tmp_path):
        path = tmp_path / "stats.json"
        estimator = Estimator(str(path), default_mbps=2.0, overhead_seconds=6.0)
        estimator.record("video", 1024 * 1024, 10.0)
        stored = json.loads(path.read_text())
        assert set(stored) <= {"video", "all"}
        for entry in stored.values():
            assert set(entry) <= {"mbps", "seconds", "samples"}


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------

class TestAdmission:
    def test_free_slot_starts_immediately(self):
        queue, started = make_queue(Limits(max_concurrent=2))
        admission = queue.submit("a", user_id=1)
        assert admission.accepted is True
        assert admission.status == "downloading"
        assert admission.position is None
        assert started == ["a"]

    def test_second_job_of_the_same_user_is_refused(self):
        queue, started = make_queue(Limits(max_concurrent=4))
        queue.submit("a", user_id=1)
        admission = queue.submit("b", user_id=1)
        assert admission.accepted is False
        assert admission.code == download_queue.CODE_USER_BUSY
        assert started == ["a"]

    def test_a_different_user_may_use_the_free_slot(self):
        queue, started = make_queue(Limits(max_concurrent=2))
        queue.submit("a", user_id=1)
        admission = queue.submit("b", user_id=2)
        assert admission.accepted is True
        assert admission.status == "downloading"
        assert started == ["a", "b"]

    def test_jobs_wait_fifo_and_report_their_position(self):
        queue, started = make_queue(Limits(max_concurrent=1, max_wait_seconds=900))
        fast_estimator(queue)
        assert queue.submit("a", user_id=1).status == "downloading"

        second = queue.submit("b", user_id=2)
        third = queue.submit("c", user_id=3)
        assert second.status == "queued" and second.position == 1
        assert third.status == "queued" and third.position == 2
        assert started == ["a"]

        queue.finish("a", seconds=60.0)
        assert started == ["a", "b"], "the first queued job must start next"
        queue.finish("b", seconds=60.0)
        assert started == ["a", "b", "c"]

    def test_refuses_when_the_predicted_wait_exceeds_the_budget(self):
        queue, started = make_queue(
            Limits(max_concurrent=1, max_wait_seconds=60, max_queue_length=10)
        )
        queue.estimator.record("video", None, 100.0)  # every job takes ~100 s
        queue.submit("a", user_id=1)
        admission = queue.submit("b", user_id=2)
        assert admission.accepted is False
        assert admission.code == download_queue.CODE_TOO_LONG
        assert admission.wait_seconds and admission.wait_seconds >= 60
        assert started == ["a"]

    def test_refuses_when_the_queue_is_full(self):
        queue, _ = make_queue(
            Limits(max_concurrent=1, max_wait_seconds=900, max_queue_length=1)
        )
        fast_estimator(queue)
        queue.submit("a", user_id=1)
        assert queue.submit("b", user_id=2).status == "queued"
        admission = queue.submit("c", user_id=3)
        assert admission.accepted is False
        assert admission.code == download_queue.CODE_QUEUE_FULL

    def test_zero_queue_length_refuses_as_busy(self):
        queue, _ = make_queue(Limits(max_concurrent=1, max_queue_length=0))
        queue.submit("a", user_id=1)
        admission = queue.submit("b", user_id=2)
        assert admission.accepted is False
        assert admission.code == download_queue.CODE_BUSY

    def test_wait_counts_the_remaining_work_of_running_jobs(self):
        clock = Clock()
        queue, _ = make_queue(
            Limits(max_concurrent=1, max_wait_seconds=900), clock=clock
        )
        queue.estimator.record("video", None, 100.0)
        queue.submit("a", user_id=1)
        clock.advance(80)  # 20 s of work left on the running job
        admission = queue.submit("b", user_id=2)
        assert admission.status == "queued"
        assert admission.wait_seconds == pytest.approx(20, abs=2)

    def test_raise_at_runtime_starts_queued_jobs_immediately(self):
        queue, started = make_queue(Limits(max_concurrent=1, max_wait_seconds=900))
        fast_estimator(queue)
        queue.submit("a", user_id=1)
        queue.submit("b", user_id=2)
        assert started == ["a"]

        queue.apply_limits(queue.limits.copy(max_concurrent=2))
        assert started == ["a", "b"]

    def test_per_user_queued_limit(self):
        """A user may hold the configured number of waiting jobs, no more.

        With the shipped defaults (1 active + 1 queued) a second request is
        refused outright, which is the intended "one person cannot fill the
        queue" policy; raising ``per_user_active`` is what makes the queued
        allowance reachable.
        """
        queue, _ = make_queue(
            Limits(max_concurrent=1, per_user_active=2, per_user_queued=1, max_wait_seconds=900)
        )
        fast_estimator(queue)
        queue.submit("a", user_id=1)
        assert queue.submit("b", user_id=1).status == "queued"
        refused = queue.submit("c", user_id=1)
        assert refused.accepted is False
        assert refused.code == download_queue.CODE_USER_BUSY

    def test_defaults_refuse_a_second_request_from_the_same_user(self):
        queue, _ = make_queue(Limits(max_concurrent=2), clock=Clock())
        assert queue.submit("a", user_id=1).status == "downloading"
        # per_user_active=1 / per_user_queued=1: refused, never queued behind
        # the user's own job.
        assert queue.submit("b", user_id=1).code == download_queue.CODE_USER_BUSY
        assert queue.queued_count() == 0


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

class TestExpiry:
    def test_stale_queue_entries_expire_with_a_reason(self):
        clock = Clock()
        expired: list[tuple[str, str]] = []
        queue, started = make_queue(
            Limits(max_concurrent=1, max_wait_seconds=900, expiry_seconds=120),
            clock=clock,
            expired=expired,
        )
        fast_estimator(queue)
        queue.submit("a", user_id=1)
        queue.submit("b", user_id=2)

        clock.advance(121)
        queue.tick()

        assert [job for job, _ in expired] == ["b"]
        assert "120" in expired[0][1]
        assert queue.position_of("b") is None
        assert queue.queued_count() == 0
        assert started == ["a"]

    def test_a_started_job_never_expires(self):
        clock = Clock()
        expired: list[tuple[str, str]] = []
        queue, _ = make_queue(
            Limits(max_concurrent=1, expiry_seconds=60), clock=clock, expired=expired
        )
        queue.submit("a", user_id=1)
        clock.advance(600)
        queue.tick()
        assert expired == []
        assert queue.active_count() == 1


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_shape_is_the_api_contract(self):
        queue, _ = make_queue(Limits(max_concurrent=1, max_wait_seconds=900))
        fast_estimator(queue)
        queue.submit("a", user_id=7)
        queue.submit("b", user_id=8)
        snapshot = queue.snapshot()

        assert set(snapshot) == {"active", "queued", "limits", "counts", "next_wait_seconds", "estimator"}
        assert snapshot["counts"]["active"] == 1
        assert snapshot["counts"]["queued"] == 1
        assert snapshot["active"][0]["job_id"] == "a"
        assert snapshot["queued"][0]["job_id"] == "b"
        assert snapshot["queued"][0]["position"] == 1
        assert snapshot["queued"][0]["user_id"] == 8
        assert "estimate_seconds" in snapshot["queued"][0]
        for key in (
            "max_concurrent",
            "per_user_active",
            "per_user_queued",
            "max_queue_length",
            "max_wait_seconds",
            "estimate_mbps",
        ):
            assert key in snapshot["limits"]

    def test_finish_releases_the_slot_and_trains_the_estimator(self):
        queue, started = make_queue(Limits(max_concurrent=1, max_wait_seconds=900))
        queue.submit("a", user_id=1)
        queue.finish("a", size_bytes=10 * 1024 * 1024, seconds=10.0)
        assert queue.active_count() == 0
        assert queue.estimator.snapshot()["video"]["samples"] == 1
        assert started == ["a"]

    def test_a_failed_job_does_not_train_the_estimator(self):
        queue, _ = make_queue(Limits(max_concurrent=1))
        queue.submit("a", user_id=1)
        queue.finish("a", seconds=10.0, succeeded=False)
        assert queue.estimator.snapshot() == {}
        assert queue.active_count() == 0

    def test_the_state_directory_comes_from_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOYURA_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.delenv("QUEUE_STATS_PATH", raising=False)
        path = download_queue.default_stats_path(None)

        assert path == str(tmp_path / "state" / "koyura_queue_stats.json")
        assert (tmp_path / "state").is_dir()


# ---------------------------------------------------------------------------
# acquire: a conversion takes a slot like any other work
# ---------------------------------------------------------------------------

class TestAcquireForConversions:
    def test_a_conversion_takes_a_free_slot(self):
        queue, _ = make_queue(Limits(max_concurrent=2))
        assert queue.acquire("conv1", kind="transcode") is True
        assert queue.active_count() == 1
        entry = queue.entry("conv1")
        assert entry is not None and entry.kind == "transcode"

    def test_a_conversion_waits_when_every_slot_is_taken(self):
        """One x264 pass at a time is a CPU budget, not a politeness rule."""
        queue, _ = make_queue(Limits(max_concurrent=1), started=[])
        queue.submit("download1", user_id=1)
        assert queue.acquire("conv1", kind="transcode") is False
        assert queue.active_count() == 1

    def test_acquire_is_idempotent_for_a_job_that_already_holds_a_slot(self):
        queue, _ = make_queue(Limits(max_concurrent=1))
        queue.submit("job1", user_id=1)
        assert queue.acquire("job1", kind="transcode") is True
        assert queue.active_count() == 1

    def test_finishing_a_conversion_frees_the_slot(self):
        queue, started = make_queue(Limits(max_concurrent=1))
        assert queue.acquire("conv1") is True
        queue.finish("conv1")
        assert queue.active_count() == 0
        assert queue.submit("download2", user_id=2).accepted
        assert started == ["download2"]
