"""FIFO download queue with admission control and a measured wait estimate.

The queue lives in the engine, not in the bot, so the Telegram client and the
loopback web UI see exactly the same state (``GET /api/queue``).

Admission rule
--------------
A new job is either started right away (a slot is free) or queued. It is
**refused** — never silently queued forever — when any of these holds:

* the predicted wait is longer than ``QUEUE_MAX_WAIT_SECONDS``;
* the queue already holds ``MAX_QUEUE_LENGTH`` jobs;
* the user already has an active or a queued job (``PER_USER_MAX_*``).

The predicted wait is::

    (remaining work of the active jobs + estimates of the queued jobs ahead)
    / MAX_CONCURRENT_DOWNLOADS

which is why a 2 vCPU box with two 3-minute downloads refuses a third job
instead of parking it behind a five-minute wait.

Estimates
---------
Every finished job feeds an EWMA of measured throughput (MB/s) and wall-clock
duration, kept per media kind (video/audio) plus an "all" fallback. The number
shown to a user is always ``overhead + size_MB / MBps`` clamped to 5…900 s;
when no measurement exists yet the configured fallback MB/s is used and the
estimate is flagged *approximate* instead of being presented as fact.

The EWMA state is a small JSON file (``QUEUE_STATS_PATH``) so a restart does
not throw away what the engine has learned. Only aggregate numbers are stored,
never URLs or titles.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("koyura.engine.queue")

ESTIMATE_MIN_SECONDS = 5.0
ESTIMATE_MAX_SECONDS = 900.0
KINDS = ("video", "audio")
MB = 1024.0 * 1024.0

# Size assumed for an unknown-size job ("a typical short video") when the EWMA
# has no duration for that kind yet. It is only ever shown as approximate.
UNKNOWN_SIZE_MB = 25.0

# Refusal codes, mirrored by bot/koyura_client.py and the web UI.
CODE_BUSY = "busy"
CODE_QUEUE_FULL = "queue_full"
CODE_TOO_LONG = "too_long"
CODE_USER_BUSY = "user_busy"

QUEUED_EXPIRED_MESSAGE = (
    "Downloaded queue wait exceeded {seconds} s; the job was removed from the "
    "queue. Please try again later."
)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%s is below the minimum %s, using %s", name, value, minimum, minimum)
        return minimum
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%s is below the minimum %s, using %s", name, value, minimum, minimum)
        return minimum
    return value


def clamp_estimate(seconds: float) -> float:
    """Clamp an estimate into the range a human can reason about (5…900 s)."""
    return max(ESTIMATE_MIN_SECONDS, min(ESTIMATE_MAX_SECONDS, float(seconds)))


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

@dataclass
class Limits:
    """Runtime download limits (env defaults, overridable from the panel)."""

    max_concurrent: int = 2
    per_user_active: int = 1
    per_user_queued: int = 1
    max_queue_length: int = 6
    max_wait_seconds: int = 120
    estimate_mbps: float = 2.0
    estimate_overhead_seconds: float = 6.0
    expiry_seconds: int = 120

    # -- construction ------------------------------------------------------

    @classmethod
    def from_env(cls) -> Limits:
        """Defaults: two slots (one per vCPU) and a two-minute wait budget."""
        max_concurrent = _env_int("MAX_CONCURRENT_DOWNLOADS", 2, minimum=1)
        return cls(
            max_concurrent=max_concurrent,
            per_user_active=_env_int("PER_USER_MAX_ACTIVE", 1, minimum=0),
            per_user_queued=_env_int("PER_USER_MAX_QUEUED", 1, minimum=0),
            max_queue_length=_env_int("MAX_QUEUE_LENGTH", 6, minimum=0),
            max_wait_seconds=_env_int("QUEUE_MAX_WAIT_SECONDS", 120, minimum=0),
            estimate_mbps=_env_float("QUEUE_ESTIMATE_MBPS", 2.0, minimum=0.1),
            estimate_overhead_seconds=_env_float(
                "QUEUE_ESTIMATE_OVERHEAD_SECONDS", 6.0, minimum=0.0
            ),
            expiry_seconds=_env_int("QUEUE_EXPIRY_SECONDS", 120, minimum=1),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_concurrent": self.max_concurrent,
            "per_user_active": self.per_user_active,
            "per_user_queued": self.per_user_queued,
            "max_queue_length": self.max_queue_length,
            "max_wait_seconds": self.max_wait_seconds,
            "estimate_mbps": round(self.estimate_mbps, 2),
            "estimate_overhead_seconds": round(self.estimate_overhead_seconds, 1),
            "expiry_seconds": self.expiry_seconds,
        }

    def copy(self, **overrides: float | int) -> Limits:
        data = self.as_dict()
        data.update(overrides)
        return Limits(**data)  # type: ignore[arg-type]

    # -- validation --------------------------------------------------------

    # Lower/upper bounds for the panel form; ``None`` means "derive from the
    # value of another field" (per-user active is bounded by the slot count).
    BOUNDS = {
        "max_concurrent": (1, 8),
        "per_user_queued": (0, 3),
        "max_queue_length": (0, 50),
        "max_wait_seconds": (15, 900),
        "estimate_mbps": (0.2, 50.0),
        "estimate_overhead_seconds": (0.0, 60.0),
        "expiry_seconds": (15, 900),
    }

    # Environment-style names accepted by ``POST /api/settings`` as aliases.
    ALIASES = {
        "MAX_CONCURRENT_DOWNLOADS": "max_concurrent",
        "PER_USER_MAX_ACTIVE": "per_user_active",
        "PER_USER_MAX_QUEUED": "per_user_queued",
        "MAX_QUEUE_LENGTH": "max_queue_length",
        "QUEUE_MAX_WAIT_SECONDS": "max_wait_seconds",
        "QUEUE_ESTIMATE_MBPS": "estimate_mbps",
        "QUEUE_ESTIMATE_OVERHEAD_SECONDS": "estimate_overhead_seconds",
        "QUEUE_EXPIRY_SECONDS": "expiry_seconds",
    }

    @classmethod
    def parse(cls, payload: dict, base: Limits | None = None) -> tuple[Limits, list[str]]:
        """Validate a partial update.

        Returns the updated limits (``base`` plus whatever validated) and a list
        of human-readable errors. Invalid values are reported, not silently
        clamped, so the panel form can explain what is wrong.
        """
        current = (base or cls.from_env()).as_dict()
        errors: list[str] = []
        incoming: dict[str, object] = {}
        for raw_key, raw_value in (payload or {}).items():
            key = cls.ALIASES.get(str(raw_key), str(raw_key))
            if key not in current:
                errors.append(f"Unknown setting: {raw_key}")
                continue
            incoming[key] = raw_value

        # Concurrency first: per-user active is bounded by it.
        parsed: dict[str, float | int] = {}
        slots = int(current["max_concurrent"])
        if "max_concurrent" in incoming:
            value, error = cls._parse_number("max_concurrent", incoming["max_concurrent"])
            if error:
                errors.append(error)
            else:
                low, high = cls.BOUNDS["max_concurrent"]
                if not low <= value <= high:
                    errors.append(f"max_concurrent must be between {low} and {high}")
                else:
                    parsed["max_concurrent"] = value
                    slots = int(value)

        for key in ("per_user_active", "per_user_queued", "max_queue_length",
                    "max_wait_seconds", "estimate_mbps",
                    "estimate_overhead_seconds", "expiry_seconds"):
            if key not in incoming:
                continue
            value, error = cls._parse_number(key, incoming[key])
            if error:
                errors.append(error)
                continue
            if key == "per_user_active":
                if not 1 <= value <= slots:
                    errors.append(
                        f"per_user_active must be between 1 and {slots} "
                        "(the number of concurrent slots)"
                    )
                    continue
            else:
                low, high = cls.BOUNDS[key]
                if not low <= value <= high:
                    errors.append(f"{key} must be between {low} and {high}")
                    continue
            parsed[key] = value

        if errors:
            return cls(**current), errors  # type: ignore[arg-type]
        current.update(parsed)
        return cls(**current), []  # type: ignore[arg-type]

    @staticmethod
    def _parse_number(key: str, raw: object) -> tuple[float, str | None]:
        if isinstance(raw, bool) or raw is None or str(raw).strip() == "":
            return 0.0, f"{key} must be a number"
        try:
            number = float(str(raw).strip())
        except (TypeError, ValueError):
            return 0.0, f"{key} must be a number"
        if number != number:  # NaN
            return 0.0, f"{key} must be a number"
        if key in ("estimate_mbps", "estimate_overhead_seconds"):
            return number, None
        if number != int(number):
            return 0.0, f"{key} must be a whole number"
        return int(number), None


# ---------------------------------------------------------------------------
# Estimate model
# ---------------------------------------------------------------------------

class Estimator:
    """EWMA of measured throughput and duration, persisted as small JSON.

    The EWMA is fed by the time a job spends in the queue **plus** the time it
    downloads (both are wall clock the next job has to wait for), so the
    estimate covers merging/transcoding too and does not promise a speed the
    box cannot deliver.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        default_mbps: float = 2.0,
        overhead_seconds: float = 6.0,
        alpha: float = 0.3,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.default_mbps = float(default_mbps)
        self.overhead_seconds = float(overhead_seconds)
        self.alpha = float(alpha)
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, float | int]] = {}
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            logger.debug("Could not read queue stats %s", self.path, exc_info=True)
            return
        if not isinstance(raw, dict):
            return
        state: dict[str, dict[str, float | int]] = {}
        for kind, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            cleaned: dict[str, float | int] = {}
            for field_name in ("mbps", "seconds"):
                value = entry.get(field_name)
                if isinstance(value, (int, float)) and value > 0:
                    cleaned[field_name] = float(value)
            samples = entry.get("samples")
            if isinstance(samples, int) and samples > 0:
                cleaned["samples"] = samples
            if cleaned:
                state[str(kind)] = cleaned
        with self._lock:
            self._state = state

    def _save_locked(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            logger.debug("Could not persist queue stats to %s", self.path, exc_info=True)

    # -- configuration -----------------------------------------------------

    def configure(self, *, default_mbps: float | None = None,
                  overhead_seconds: float | None = None) -> None:
        with self._lock:
            if default_mbps is not None:
                self.default_mbps = float(default_mbps)
            if overhead_seconds is not None:
                self.overhead_seconds = float(overhead_seconds)

    # -- estimation --------------------------------------------------------

    def _measured_mbps(self, kind: str) -> float | None:
        for key in (kind, "all"):
            value = self._state.get(key, {}).get("mbps")
            if isinstance(value, float) and value > 0:
                return value
        return None

    def _measured_seconds(self, kind: str) -> float | None:
        for key in (kind, "all"):
            value = self._state.get(key, {}).get("seconds")
            if isinstance(value, float) and value > 0:
                return value
        return None

    def estimate(self, kind: str, size_bytes: float | int | None = None) -> tuple[float, bool]:
        """Return ``(seconds, approximate)`` for a job of ``size_bytes``."""
        kind = kind if kind in KINDS else "all"
        with self._lock:
            mbps = self._measured_mbps(kind)
            measured = mbps is not None
            rate = mbps or self.default_mbps
            overhead = self.overhead_seconds
            duration = self._measured_seconds(kind)

        size_mb = (float(size_bytes) / MB) if size_bytes and float(size_bytes) > 0 else None
        if size_mb is None:
            # No size: the best available answer is how long a job of this kind
            # has actually taken; failing that, a typical size at the fallback
            # rate. Both are approximations by definition.
            seconds = duration if duration is not None else overhead + UNKNOWN_SIZE_MB / rate
            return clamp_estimate(seconds), True
        return clamp_estimate(overhead + size_mb / rate), not measured

    def record(self, kind: str, size_bytes: float | int | None, seconds: float | None) -> None:
        """Fold one finished job into the EWMA."""
        kind = kind if kind in KINDS else "video"
        if not seconds or seconds <= 0.5:
            return
        size = float(size_bytes) if size_bytes and float(size_bytes) > 0 else None
        with self._lock:
            for key in (kind, "all"):
                entry = self._state.setdefault(key, {})
                entry["seconds"] = self._ewma(entry.get("seconds"), float(seconds))
                if size is not None:
                    mbps = (size / MB) / float(seconds)
                    entry["mbps"] = self._ewma(entry.get("mbps"), mbps)
                entry["samples"] = int(entry.get("samples") or 0) + 1
            self._save_locked()

    def _ewma(self, previous: object, sample: float) -> float:
        if isinstance(previous, (int, float)) and previous > 0:
            return float(previous) * (1.0 - self.alpha) + sample * self.alpha
        return sample

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        with self._lock:
            return {kind: dict(values) for kind, values in self._state.items()}


def default_stats_path(downloads_dir: str | None = None) -> str:
    """Where the EWMA lives.

    Deliberately **not** inside ``DOWNLOADS_PATH``: that directory is garbage
    collected by the bot (age/disk limits) and by the panel's "Purge all", so a
    hidden file there would be deleted together with the media it describes.
    The default sits in the engine's state directory, which nothing cleans:

    * ``QUEUE_STATS_PATH`` — explicit override, used as-is;
    * ``KOYURA_STATE_DIR`` — the engine's data directory (a volume in compose,
      ``/var/lib/koyura`` for the native/systemd layout).

    Returns ``""`` when no writable place exists; ``Estimator`` then simply
    keeps its EWMA in memory instead of losing it silently.
    """
    explicit = os.environ.get("QUEUE_STATS_PATH", "").strip()
    if explicit:
        return explicit

    state_dir = os.environ.get("KOYURA_STATE_DIR", "").strip() or "/var/lib/koyura"
    if downloads_dir and os.path.abspath(state_dir) == os.path.abspath(downloads_dir):
        # Never the purgeable directory, even if someone points it there.
        state_dir = os.path.join(os.path.dirname(os.path.abspath(downloads_dir)), "koyura-state")
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        logger.warning(
            "Queue stats directory %s is not writable; estimates stay in memory", state_dir
        )
        return ""
    return os.path.join(state_dir, "koyura_queue_stats.json")


# ---------------------------------------------------------------------------
# Admission results
# ---------------------------------------------------------------------------

@dataclass
class Admission:
    """Outcome of ``JobQueue.submit``."""

    accepted: bool
    status: str = "queued"  # "downloading" | "queued" | "refused"
    position: int | None = None
    wait_seconds: int | None = None
    estimate_seconds: float | None = None
    estimate_approximate: bool = False
    code: str | None = None
    error: str | None = None
    queue_length: int = 0

    def as_payload(self) -> dict[str, object]:
        """The additive JSON the bot and the web UI read."""
        return {
            "status": self.status,
            "queue_position": self.position,
            "wait_seconds": self.wait_seconds,
            "queue_estimate_seconds": self.estimate_seconds,
            "queue_estimate_approximate": self.estimate_approximate,
            "queue_length": self.queue_length,
            "code": self.code,
            "error": self.error,
        }


@dataclass
class QueueEntry:
    """One job the queue knows about."""

    job_id: str
    user_id: int | None
    kind: str
    estimate_seconds: float
    approximate: bool
    enqueued_at: float
    started_at: float | None = None
    size_bytes: int | None = None
    attempts: int = 0
    extra: dict = field(default_factory=dict)

    def position_info(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "estimate_seconds": round(self.estimate_seconds, 1),
            "estimate_approximate": self.approximate,
        }


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------

class JobQueue:
    """Own the slots: FIFO order, per-user limits, expiry and dispatch.

    ``runner(job_id)`` is called on a fresh thread when a job starts; it must
    call :meth:`finish` when the job is over (successfully or not), otherwise
    the slot is never released.
    """

    def __init__(
        self,
        limits: Limits,
        runner: Callable[[str], None],
        *,
        estimator: Estimator | None = None,
        clock: Callable[[], float] = time.time,
        on_expire: Callable[[str, str], None] | None = None,
        dispatch_interval: float = 1.0,
    ) -> None:
        self._limits = limits
        self._runner = runner
        self._clock = clock
        self._lock = threading.RLock()
        self._active: dict[str, QueueEntry] = {}
        self._queued: list[QueueEntry] = []
        self._on_expire = on_expire
        self._dispatch_interval = dispatch_interval
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self.estimator = estimator or Estimator(
            default_mbps=limits.estimate_mbps,
            overhead_seconds=limits.estimate_overhead_seconds,
        )

    # -- limits ------------------------------------------------------------

    @property
    def limits(self) -> Limits:
        with self._lock:
            return self._limits

    def apply_limits(self, limits: Limits) -> Limits:
        """Swap the limits at runtime, without touching jobs already running."""
        with self._lock:
            previous = self._limits
            self._limits = limits
            self.estimator.configure(
                default_mbps=limits.estimate_mbps,
                overhead_seconds=limits.estimate_overhead_seconds,
            )
        logger.info(
            "Download limits updated: %s -> %s", previous.as_dict(), limits.as_dict()
        )
        # A raised concurrency limit should let queued work start immediately.
        self.tick()
        return limits

    # -- submission --------------------------------------------------------

    def submit(
        self,
        job_id: str,
        *,
        user_id: int | None = None,
        kind: str = "video",
        size_bytes: int | None = None,
    ) -> Admission:
        with self._lock:
            limits = self._limits
            now = self._clock()
            self._expire_locked(now)
            estimate, approximate = self.estimator.estimate(kind, size_bytes)

            if user_id is not None:
                active_user = sum(1 for e in self._active.values() if e.user_id == user_id)
                queued_user = sum(1 for e in self._queued if e.user_id == user_id)
                if active_user >= limits.per_user_active or queued_user >= limits.per_user_queued:
                    return Admission(
                        accepted=False,
                        status="refused",
                        code=CODE_USER_BUSY,
                        error=(
                            "You already have a download in progress. "
                            "Wait for it to finish or try again later."
                        ),
                        wait_seconds=round(self._wait_ahead_locked(len(self._queued))) or 1,
                        estimate_seconds=round(estimate, 1),
                        estimate_approximate=approximate,
                        queue_length=len(self._queued),
                    )

            if len(self._active) < limits.max_concurrent:
                entry = QueueEntry(
                    job_id=job_id,
                    user_id=user_id,
                    kind=kind,
                    estimate_seconds=estimate,
                    approximate=approximate,
                    enqueued_at=now,
                    started_at=now,
                    size_bytes=size_bytes,
                )
                self._active[job_id] = entry
                self._launch(entry)
                return Admission(
                    accepted=True,
                    status="downloading",
                    position=None,
                    wait_seconds=0,
                    estimate_seconds=round(estimate, 1),
                    estimate_approximate=approximate,
                    queue_length=len(self._queued),
                )

            wait = self._wait_ahead_locked(len(self._queued))
            if limits.max_queue_length <= 0:
                return Admission(
                    accepted=False,
                    status="refused",
                    code=CODE_BUSY,
                    error="The server is busy and queueing is disabled. Please try again later.",
                    wait_seconds=max(1, round(wait)),
                    estimate_seconds=round(estimate, 1),
                    estimate_approximate=approximate,
                    queue_length=0,
                )
            if len(self._queued) >= limits.max_queue_length:
                return Admission(
                    accepted=False,
                    status="refused",
                    code=CODE_QUEUE_FULL,
                    error="The download queue is full. Please try again in a moment.",
                    wait_seconds=max(1, round(wait)),
                    estimate_seconds=round(estimate, 1),
                    estimate_approximate=approximate,
                    queue_length=len(self._queued),
                )
            if wait > limits.max_wait_seconds:
                return Admission(
                    accepted=False,
                    status="refused",
                    code=CODE_TOO_LONG,
                    error=(
                        "The download queue is longer than the "
                        f"{limits.max_wait_seconds} s wait budget. "
                        "Please try again later."
                    ),
                    wait_seconds=max(1, round(wait)),
                    estimate_seconds=round(estimate, 1),
                    estimate_approximate=approximate,
                    queue_length=len(self._queued),
                )

            entry = QueueEntry(
                job_id=job_id,
                user_id=user_id,
                kind=kind,
                estimate_seconds=estimate,
                approximate=approximate,
                enqueued_at=now,
                size_bytes=size_bytes,
            )
            self._queued.append(entry)
            return Admission(
                accepted=True,
                status="queued",
                position=len(self._queued),
                wait_seconds=max(0, round(wait)),
                estimate_seconds=round(estimate, 1),
                estimate_approximate=approximate,
                queue_length=len(self._queued),
            )

    # -- lifecycle ---------------------------------------------------------

    def finish(
        self,
        job_id: str,
        *,
        size_bytes: int | None = None,
        seconds: float | None = None,
        succeeded: bool = True,
    ) -> None:
        """Release a slot, feed the estimator and start the next job."""
        finished: QueueEntry | None = None
        with self._lock:
            finished = self._active.pop(job_id, None)
            self._queued = [e for e in self._queued if e.job_id != job_id]
            if finished is not None and succeeded:
                self.estimator.record(
                    finished.kind,
                    size_bytes if size_bytes is not None else finished.size_bytes,
                    seconds,
                )
        if finished is not None:
            self.tick()

    def cancel(self, job_id: str) -> bool:
        """Drop a queued job (used by expiry and by tests)."""
        with self._lock:
            before = len(self._queued)
            self._queued = [e for e in self._queued if e.job_id != job_id]
            return len(self._queued) != before

    def set_on_expire(self, callback: Callable[[str, str], None] | None) -> None:
        with self._lock:
            self._on_expire = callback

    def tick(self) -> list[str]:
        """Expire stale queue entries and start everything that fits."""
        expired: list[str] = []
        with self._lock:
            expired = self._expire_locked(self._clock())
            self._dispatch_locked()
        for job_id in expired:
            callback = self._on_expire
            if callback is not None:
                try:
                    callback(
                        job_id,
                        QUEUED_EXPIRED_MESSAGE.format(seconds=self.limits.expiry_seconds),
                    )
                except Exception:  # noqa: BLE001 - an eviction must never crash the queue
                    logger.exception("Queue expiry callback failed for %s", job_id)
        return expired

    def start(self) -> None:
        """Run the dispatcher loop in the background (idempotent)."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._loop, name="koyura-queue", daemon=True
            )
            self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.wait(self._dispatch_interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep the dispatcher alive
                logger.exception("Queue dispatcher tick failed")

    # -- introspection -----------------------------------------------------

    def position_of(self, job_id: str) -> int | None:
        """1-based position in the queue, or ``None`` when not queued."""
        with self._lock:
            for index, entry in enumerate(self._queued):
                if entry.job_id == job_id:
                    return index + 1
        return None

    def queued_count(self) -> int:
        """How many jobs are waiting for a slot right now."""
        with self._lock:
            return len(self._queued)

    def active_count(self) -> int:
        """How many jobs hold a slot right now."""
        with self._lock:
            return len(self._active)

    def wait_for(self, job_id: str) -> int | None:
        """Predicted wait for a job (0 when it is already downloading)."""
        with self._lock:
            if job_id in self._active:
                return 0
            for index, entry in enumerate(self._queued):
                if entry.job_id == job_id:
                    return max(1, round(self._wait_ahead_locked(index)))
        return None

    def entry(self, job_id: str) -> QueueEntry | None:
        with self._lock:
            if job_id in self._active:
                return self._active[job_id]
            for entry in self._queued:
                if entry.job_id == job_id:
                    return entry
        return None

    def snapshot(self) -> dict[str, Any]:
        """The body of ``GET /api/queue``."""
        with self._lock:
            now = self._clock()
            limits = self._limits
            active = [
                {
                    "job_id": entry.job_id,
                    "user_id": entry.user_id,
                    "kind": entry.kind,
                    "elapsed": round(max(0.0, now - (entry.started_at or now)), 1),
                    "estimate_seconds": round(entry.estimate_seconds, 1),
                    "estimate_approximate": entry.approximate,
                }
                for entry in self._active.values()
            ]
            queued = [
                {
                    "job_id": entry.job_id,
                    "user_id": entry.user_id,
                    "kind": entry.kind,
                    "position": index + 1,
                    "estimate_seconds": round(entry.estimate_seconds, 1),
                    "estimate_approximate": entry.approximate,
                    "wait_seconds": max(1, round(self._wait_ahead_locked(index))),
                    "queued_seconds": round(max(0.0, now - entry.enqueued_at), 1),
                }
                for index, entry in enumerate(self._queued)
            ]
            total_wait = self._wait_ahead_locked(len(self._queued))
            return {
                "active": active,
                "queued": queued,
                "limits": limits.as_dict(),
                "counts": {
                    "active": len(self._active),
                    "queued": len(self._queued),
                    "max_concurrent": limits.max_concurrent,
                    "max_queue_length": limits.max_queue_length,
                },
                "next_wait_seconds": max(0, round(total_wait)),
                "estimator": self.estimator.snapshot(),
            }

    # -- internals ---------------------------------------------------------

    def _wait_ahead_locked(self, index: int) -> float:
        """Predicted wait for a job sitting at position ``index`` in the queue."""
        limits = self._limits
        now = self._clock()
        remaining = 0.0
        for entry in self._active.values():
            started = entry.started_at or now
            left = entry.estimate_seconds - max(0.0, now - started)
            remaining += max(0.0, min(left, entry.estimate_seconds))
        ahead = sum(e.estimate_seconds for e in self._queued[:index])
        return (remaining + ahead) / max(1, limits.max_concurrent)

    def _expire_locked(self, now: float) -> list[str]:
        if not self._queued:
            return []
        limit = self._limits.expiry_seconds
        expired: list[str] = []
        kept: list[QueueEntry] = []
        for entry in self._queued:
            if now - entry.enqueued_at > limit:
                expired.append(entry.job_id)
            else:
                kept.append(entry)
        if expired:
            self._queued = kept
            logger.info("Queue evicted %d expired job(s): %s", len(expired), expired)
        return expired

    def _dispatch_locked(self) -> None:
        limits = self._limits
        started: list[QueueEntry] = []
        while self._queued and len(self._active) < limits.max_concurrent:
            entry = self._queued.pop(0)
            entry.started_at = self._clock()
            self._active[entry.job_id] = entry
            started.append(entry)
        for entry in started:
            self._launch(entry)

    def _launch(self, entry: QueueEntry) -> None:
        """Start one job on its own thread (never call with the lock held twice)."""
        entry.attempts += 1
        thread = threading.Thread(
            target=self._run_entry,
            args=(entry,),
            name=f"download-{entry.job_id}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:  # noqa: BLE001 - surface a start failure as a finished job
            logger.exception("Could not start queue worker for %s", entry.job_id)
            self._active.pop(entry.job_id, None)

    def _run_entry(self, entry: QueueEntry) -> None:
        try:
            self._runner(entry.job_id)
        except Exception:  # noqa: BLE001 - the runner reports its own errors
            logger.exception("Queue runner crashed for %s", entry.job_id)
            self.finish(entry.job_id, succeeded=False)
