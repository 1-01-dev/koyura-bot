"""Static checks for the native systemd assets in deploy/.

These catch the regressions that broke the first native deployment:
`Restart=unless-stopped` (a Docker value, invalid in systemd), a missing
`PATH`/venv wiring, and a missing runtime state directory.
"""
from __future__ import annotations

import configparser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_DIR = REPO_ROOT / "deploy" / "systemd"

APP_UNITS = {
    "koyura-engine.service": "/opt/koyura/koyura",
    "koyura-dashboard.service": "/opt/koyura/dashboard",
    "koyura-bot.service": "/opt/koyura/bot",
}
BOT_API_UNIT = "koyura-telegram-bot-api.service"


def _identity(optionstr: str) -> str:
    """Keep systemd directive names case-sensitive in configparser."""
    return optionstr


def _load(unit: str) -> configparser.ConfigParser:
    path = UNIT_DIR / unit
    assert path.exists(), f"{path} is missing"
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = _identity
    with path.open() as handle:
        parser.read_file(handle)
    return parser


def _service(name: str) -> configparser.SectionProxy:
    return _load(name)["Service"]


def _all_unit_files() -> list[str]:
    return sorted(path.name for path in UNIT_DIR.glob("*.service"))


def test_expected_units_exist():
    assert set(_all_unit_files()) == set(APP_UNITS) | {BOT_API_UNIT}


@pytest.mark.parametrize("unit", sorted(APP_UNITS) + [BOT_API_UNIT])
def test_common_service_directives(unit):
    service = _service(unit)
    assert service["User"] == "koyura"
    assert service["EnvironmentFile"] == "/etc/koyura/koyura.env"
    # systemd has no "unless-stopped" restart policy; that is a Docker value.
    assert service["Restart"] == "always"
    # systemd creates /var/lib/koyura (mode/owner) and keeps it writable.
    assert service["StateDirectory"] == "koyura"
    assert service["NoNewPrivileges"] == "true"


@pytest.mark.parametrize("unit,working_directory", sorted(APP_UNITS.items()))
def test_app_units_use_the_virtualenv(unit, working_directory):
    service = _service(unit)
    assert service["WorkingDirectory"] == working_directory
    assert service["ExecStart"].startswith("/opt/koyura/.venv/bin/")
    path = service["Environment"]
    assert "PATH=" in path
    assert path.split("PATH=", 1)[1].split(":")[0] == "/opt/koyura/.venv/bin"


def test_koyura_unit_sets_path_for_ffmpeg():
    path = _service("koyura-engine.service")["Environment"]
    assert "/usr/bin" in path, "ffmpeg/ffprobe live in /usr/bin on Ubuntu"


def test_telegram_bot_api_unit_flags():
    service = _service(BOT_API_UNIT)
    exec_start = " ".join(service["ExecStart"].split())
    assert exec_start.startswith("/usr/local/bin/telegram-bot-api ")
    assert "--local" in exec_start
    assert "--http-ip-address=127.0.0.1" in exec_start
    assert "--http-port=8081" in exec_start
    assert "--dir=/var/lib/koyura/bot-api" in exec_start
    assert "${TELEGRAM_API_ID}" in exec_start
    assert "${TELEGRAM_API_HASH}" in exec_start


def test_deployment_docs_document_ffmpeg_and_bot_api():
    # The native install guide moved from deploy/README.md to docs/deployment.md;
    # deploy/README.md is now a short pointer and must keep linking to it.
    text = (REPO_ROOT / "docs" / "deployment.md").read_text()
    assert "ffmpeg" in text
    assert "submodule update --init --recursive" in text
    assert "--local" in text
    assert "20 MB" in text
    assert "systemd-analyze verify" in text

    pointer = (REPO_ROOT / "deploy" / "README.md").read_text()
    assert "docs/deployment.md" in pointer
