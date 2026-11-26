"""Tests for the bot-side URL validation."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import url_safety
from url_safety import UnsafeURLError, validate_url


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    monkeypatch.setattr(url_safety, "_resolve_host", lambda host: ["93.184.216.34"])


def test_accepts_public_https_link():
    link = "https://youtu.be/KOYURA00001"
    assert validate_url(link) == link


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/plain,hello",
        "javascript:alert(1)",
        "ftp://example.com/x",
        "https://user:secret@example.com/x",
        "",
        "https://",
    ],
)
def test_rejects_non_http_and_malformed(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.0.5/",
        "http://169.254.169.254/",
        "http://[::1]/",
    ],
)
def test_rejects_local_targets(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url)


def test_rejects_overlong_url():
    with pytest.raises(UnsafeURLError):
        validate_url("https://example.com/" + "a" * 3000)
