"""Download-limit settings for the admin panel.

Three pieces live here:

* :class:`Limits` — the validated shape of the six fields the panel edits,
  with the same bounds the engine enforces (``koyura/download_queue.py``);
* :data:`PRESETS` — ready-made configurations for the common VPS sizes, with a
  per-user allowance derived as ``max(1, slots // 2)`` capped at 3;
* :func:`detect_hardware` — what this server actually is, so the right preset
  can be marked as recommended.

The dashboard stores the values in its own SQLite ``settings`` table and pushes
them to the engine (``POST /api/settings``) after every save, so a restart of
the engine picks them up again from the dashboard instead of the environment.
Values are only ever strings in the table; parsing/validation happens here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger("koyura.dashboard.settings")

# ---------------------------------------------------------------------------
# Field definitions
# ---------------------------------------------------------------------------

# (env/engine name, engine key, label, unit, min, max)
FIELDS: tuple[tuple[str, str, str, str, float, float], ...] = (
    ("MAX_CONCURRENT_DOWNLOADS", "max_concurrent", "Concurrent downloads", "slots", 1, 8),
    ("PER_USER_MAX_ACTIVE", "per_user_active", "Per-user active", "jobs", 1, 8),
    ("PER_USER_MAX_QUEUED", "per_user_queued", "Per-user queued", "jobs", 0, 3),
    ("MAX_QUEUE_LENGTH", "max_queue_length", "Queue length", "jobs", 0, 50),
    ("QUEUE_MAX_WAIT_SECONDS", "max_wait_seconds", "Max wait", "s", 15, 900),
    ("QUEUE_ESTIMATE_MBPS", "estimate_mbps", "Estimate throughput", "MB/s", 0.2, 50.0),
)

# Defaults match the engine's own environment defaults.
DEFAULTS: dict[str, float] = {
    "max_concurrent": 2,
    "per_user_active": 1,
    "per_user_queued": 1,
    "max_queue_length": 6,
    "max_wait_seconds": 120,
    "estimate_mbps": 2.0,
}

FIELD_NAMES = tuple(key for _, key, _, _, _, _ in FIELDS)
# Fields that must stay whole numbers (everything but MB/s).
INTEGER_FIELDS = {"max_concurrent", "per_user_active", "per_user_queued",
                  "max_queue_length", "max_wait_seconds"}

SETTING_PREFIX = "download_limits."


@dataclass
class Limits:
    """The six panel-controlled limits, already validated."""

    max_concurrent: int = 2
    per_user_active: int = 1
    per_user_queued: int = 1
    max_queue_length: int = 6
    max_wait_seconds: int = 120
    estimate_mbps: float = 2.0

    # -- construction ------------------------------------------------------

    @classmethod
    def from_env(cls) -> Limits:
        """Starting point when the admin never saved anything."""
        values: dict[str, object] = {}
        for env_name, key, *_ in FIELDS:
            values[key] = _env_number(env_name, DEFAULTS[key])
        limits, _ = cls.parse(values)
        return limits

    @classmethod
    def from_row(cls, values: Mapping[str, object], base: Limits | None = None) -> Limits:
        """Merge stored strings over ``base`` (or the env defaults)."""
        current = (base or cls.from_env()).as_dict()
        merged: dict[str, object] = dict(current)
        for key, raw in values.items():
            if key in FIELD_NAMES and raw is not None and str(raw).strip() != "":
                merged[key] = str(raw)
        limits, errors = cls.parse(merged)
        if errors:
            logger.warning("Stored download limits are invalid (%s); keeping previous", errors)
            return base or cls.from_env()
        return limits

    # -- validation --------------------------------------------------------

    @classmethod
    def parse(cls, values: Mapping[str, object]) -> tuple[Limits, list[str]]:
        """Validate a full set of values; returns parsed limits and errors.

        Every error names the field the way the form labels it, so the message
        can be shown next to the input without translation.
        """
        errors: list[str] = []
        parsed: dict[str, float] = {}
        for _env_name, key, label, _unit, low, high in FIELDS:
            raw = values.get(key, values.get(_env_name, DEFAULTS[key]))
            number, error = _number(key, raw)
            if error:
                errors.append(f"{label}: {error}")
                continue
            if not low <= number <= high:
                errors.append(
                    f"{label}: must be between {_pretty(low)} and {_pretty(high)}"
                )
                continue
            parsed[key] = number

        slots = int(parsed.get("max_concurrent", DEFAULTS["max_concurrent"]))
        if "per_user_active" in parsed:
            # One user cannot hold more slots than exist.
            if parsed["per_user_active"] > slots:
                errors.append(
                    "Per-user active: cannot exceed concurrent downloads "
                    f"({slots})"
                )
            elif parsed["per_user_active"] < 1:
                errors.append("Per-user active: must be at least 1")
        if errors:
            return cls(), errors
        return cls(**{key: _as_int(key, value) for key, value in parsed.items()}), []  # type: ignore[arg-type]

    # -- representation ----------------------------------------------------

    def as_dict(self) -> dict[str, float]:
        return asdict(self)

    def as_engine_payload(self) -> dict[str, float]:
        """Engine key names (``POST /api/settings`` accepts both spellings)."""
        return {
            "max_concurrent": int(self.max_concurrent),
            "per_user_active": int(self.per_user_active),
            "per_user_queued": int(self.per_user_queued),
            "max_queue_length": int(self.max_queue_length),
            "max_wait_seconds": int(self.max_wait_seconds),
            "estimate_mbps": float(self.estimate_mbps),
        }

    def as_form(self) -> dict[str, str]:
        """Strings for the HTML inputs."""
        return {
            key: _pretty(value)
            for key, value in self.as_dict().items()
        }

    def matches_preset(self, preset: Preset) -> bool:
        return (
            int(self.max_concurrent) == preset.max_concurrent
            and int(self.max_queue_length) == preset.max_queue_length
            and int(self.max_wait_seconds) == preset.max_wait_seconds
            and int(self.per_user_active) == preset.per_user_active
        )


def _as_int(key: str, value: float) -> int | float:
    return int(value) if key in INTEGER_FIELDS else value


def _number(key: str, raw: object) -> tuple[float, str | None]:
    if isinstance(raw, bool) or raw is None or str(raw).strip() == "":
        return 0.0, "enter a number"
    try:
        number = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return 0.0, "enter a number"
    if number != number:
        return 0.0, "enter a number"
    if key in INTEGER_FIELDS and number != int(number):
        return 0.0, "enter a whole number"
    return number, None


def _pretty(value: object) -> str:
    """``2`` for 2.0, ``0.5`` for 0.5 — no trailing zeros in the form."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return str(int(number))
    return f"{number:g}"


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        logger.warning("Invalid %s=%r, using %s", name, raw, default)
        return default


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Preset:
    """A ready-made configuration for one server size."""

    key: str
    label: str
    cpu: int
    memory_gb: int
    max_concurrent: int
    max_queue_length: int
    max_wait_seconds: int
    per_user_active: int
    per_user_queued: int = 1
    estimate_mbps: float = 2.0

    def as_limits(self) -> Limits:
        return Limits(
            max_concurrent=self.max_concurrent,
            per_user_active=self.per_user_active,
            per_user_queued=self.per_user_queued,
            max_queue_length=self.max_queue_length,
            max_wait_seconds=self.max_wait_seconds,
            estimate_mbps=self.estimate_mbps,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "cpu": self.cpu,
            "memory_gb": self.memory_gb,
            "fields": self.as_limits().as_form(),
        }


def per_user_active_for(slots: int) -> int:
    """``max(1, slots // 2)`` capped at 3.

    Half the slots keeps a single person from owning the box while still
    letting one user run two jobs in parallel on a bigger server; the cap is
    there because a third simultaneous job is slower than two on every box we
    care about.
    """
    return max(1, min(3, int(slots) // 2))


PRESETS: tuple[Preset, ...] = tuple(
    Preset(
        key=key,
        label=label,
        cpu=cpu,
        memory_gb=memory,
        max_concurrent=slots,
        max_queue_length=queue,
        max_wait_seconds=wait,
        per_user_active=per_user_active_for(slots),
    )
    for key, label, cpu, memory, slots, queue, wait in (
        ("tiny", "1 vCPU / 2 GB", 1, 2, 1, 4, 90),
        ("small", "2 vCPU / 4 GB", 2, 4, 2, 6, 120),
        ("medium", "4 vCPU / 8 GB", 4, 8, 4, 12, 180),
        ("large", "8 vCPU / 16 GB", 8, 16, 6, 20, 240),
    )
)

PRESETS_BY_KEY = {preset.key: preset for preset in PRESETS}


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Hardware:
    """What the panel thinks this machine is."""

    cpu: int
    memory_gb: float

    @property
    def memory_label(self) -> str:
        return f"{self.memory_gb:g} GB" if self.memory_gb > 0 else "unknown RAM"


def _cpu_count() -> int:
    try:
        count = os.cpu_count() or 1
    except (OSError, NotImplementedError):  # pragma: no cover - exotic platforms
        count = 1
    # cgroup quota (containers): os.cpu_count() reports the host's cores, but a
    # CPU limit is what the box can actually give to yt-dlp/ffmpeg.
    quota = _cgroup_cpu_limit()
    if quota:
        count = min(count, quota)
    return max(1, int(count))


_CGROUP_QUOTA = re.compile(r"^(\d+)\s+(\d+)$")


def _cgroup_cpu_limit() -> int | None:
    candidates = (
        ("/sys/fs/cgroup/cpu.max", "v2"),
        ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "v1"),
    )
    for path, version in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if version == "v2":
            match = _CGROUP_QUOTA.match(raw)
            if not match:
                continue
            quota, period = int(match.group(1)), int(match.group(2))
            if quota > 0 and period > 0:
                return max(1, quota // period)
        else:
            try:
                quota = int(raw)
            except ValueError:
                continue
            try:
                with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", encoding="utf-8") as handle:
                    period = int(handle.read().strip())
            except (OSError, ValueError):
                continue
            if quota > 0 and period > 0:
                return max(1, quota // period)
    return None


def _memory_gb() -> float:
    """Total RAM, 0.0 when it cannot be determined.

    ``/proc/meminfo`` is the authority on Linux; elsewhere (and in tests) the
    POSIX ``sysconf`` page count is a portable fallback, so the panel does not
    report "0 GB" on a machine that clearly has memory. A cgroup limit wins when
    it is lower — a container sees the host's RAM otherwise.
    """
    total_bytes = _proc_meminfo_bytes() or _sysconf_memory_bytes()
    limit = _cgroup_memory_limit()
    if limit and (total_bytes is None or limit < total_bytes):
        total_bytes = limit
    if not total_bytes:
        return 0.0
    return round(total_bytes / (1024.0 ** 3), 1)


def _proc_meminfo_bytes() -> int | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _sysconf_memory_bytes() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return int(pages) * int(page_size)


def _cgroup_memory_limit() -> int | None:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if raw == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        # Unlimited cgroups report a huge number (often just below 1 << 63).
        if 0 < value < (1 << 60):
            return value
    return None


def detect_hardware(
    *,
    cpu: int | None = None,
    memory_gb: float | None = None,
) -> Hardware:
    """Measure this server (arguments exist for the tests)."""
    return Hardware(
        cpu=_cpu_count() if cpu is None else max(1, int(cpu)),
        memory_gb=_memory_gb() if memory_gb is None else float(memory_gb),
    )


def recommended_preset(hardware: Hardware) -> Preset:
    """The largest preset this box can carry, by CPU first and RAM second.

    A box that cannot be matched exactly keeps the biggest preset that fits on
    both counts; a tiny box gets the smallest one.
    """
    # An unknown memory size must not veto every preset: fall back to CPU only.
    memory = hardware.memory_gb if hardware.memory_gb > 0 else float("inf")
    fitting = [
        preset
        for preset in PRESETS
        if preset.cpu <= hardware.cpu and preset.memory_gb <= memory
    ]
    if fitting:
        return fitting[-1]
    return PRESETS[0]


# ---------------------------------------------------------------------------
# Applying the limits to the engine
# ---------------------------------------------------------------------------

DEFAULT_KOYURA_ENGINE_URL = "http://koyura-engine:8899"


def koyura_engine_url() -> str:
    """Address of the media engine (``KOYURA_ENGINE_URL``, else the compose one)."""
    value = os.environ.get("KOYURA_ENGINE_URL", "").strip() or DEFAULT_KOYURA_ENGINE_URL
    return value.rstrip("/")


def apply_to_engine(limits: Limits, *, timeout: float = 5.0) -> tuple[bool, str]:
    """Push the limits to the engine (``POST /api/settings``).

    Returns ``(ok, detail)``: a failure is reported, never raised, because the
    panel must stay usable when the engine is restarting — the values are
    already saved and the next save (or the dashboard's startup push) retries.
    """
    token = (os.environ.get("INTERNAL_API_TOKEN") or "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Internal-Token"] = token
    request_obj = urllib.request.Request(  # noqa: S310 - fixed internal URL
        f"{koyura_engine_url()}/api/settings",
        data=json.dumps(limits.as_engine_payload()).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else str(exc)
        try:
            parsed = json.loads(detail)
            detail = str(parsed.get("error") or detail)
        except ValueError:
            pass
        logger.warning("Engine rejected the download limits (HTTP %s): %s", exc.code, detail)
        return False, detail
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.warning("Could not reach the engine to apply the download limits: %s", exc)
        return False, "the media engine is not reachable right now"
    try:
        parsed = json.loads(body)
    except ValueError:
        return True, "saved"
    if isinstance(parsed, dict) and parsed.get("error"):
        return False, str(parsed["error"])
    return True, "applied"


def describe_change(before: Limits, after: Limits) -> str:
    """One log line: what changed and from which value to which."""
    changes = []
    for key in FIELD_NAMES:
        old = before.as_dict()[key]
        new = after.as_dict()[key]
        if old != new:
            changes.append(f"{key} {_pretty(old)}->{_pretty(new)}")
    return ", ".join(changes) if changes else "no value changed"


FIELD_HINTS: dict[str, str] = {
    "max_concurrent": "one slot per vCPU: yt-dlp and ffmpeg are CPU-bound",
    "per_user_active": (
        "recommended max(1, slots // 2) capped at 3 — 1 is the fairest choice "
        "when more than one person uses the bot"
    ),
    "per_user_queued": "0 means a user with a running job cannot queue another",
    "max_queue_length": "0 disables queueing; over-limit requests get a 429",
    "max_wait_seconds": "longer predicted waits are refused instead of queued",
    "estimate_mbps": "fallback until the engine has measured real timings",
}
