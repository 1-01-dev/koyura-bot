"""Tests for the URL safety checks that guard the media engine."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import url_safety
from url_safety import UnsafeURLError, validate_url, validate_urls


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Keep tests offline: resolve every hostname to a public address."""
    monkeypatch.setattr(
        url_safety, "_resolve_host", lambda host: ["93.184.216.34"]
    )


def test_accepts_https_url():
    url = "https://www.youtube.com/watch?v=KOYURA00001"
    assert validate_url(url) == url


def test_accepts_http_url():
    assert validate_url("http://example.com/video") == "http://example.com/video"


def test_strips_surrounding_whitespace():
    assert validate_url("  https://example.com/a  ") == "https://example.com/a"


def test_hostname_resolving_to_private_ip_is_blocked(monkeypatch):
    monkeypatch.setattr(url_safety, "_resolve_host", lambda host: ["192.168.0.10"])
    with pytest.raises(UnsafeURLError, match="local or private"):
        validate_url("https://internal.example.com/video")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        None,
        "file:///etc/passwd",
        "data:text/html;base64,PHNjcmlwdD4=",
        "ftp://example.com/file.mp4",
        "javascript:alert(1)",
        "gopher://example.com/",
        "https://",
        "not-a-url",
        "https://user:pass@example.com/video",
        "https://example.com/pa th",
        "https://example.com/" + "a" * 3000,
    ],
)
def test_rejects_unsafe_urls(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8899/api/file/abc",
        "http://127.10.0.1/",
        "http://10.0.0.5/video",
        "http://172.16.0.1/video",
        "http://192.168.1.1/video",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/video",
        "http://[fd00::1]/video",
        "http://localhost:8080/",
        "http://service.internal/video",
        "http://0.0.0.0/",
    ],
)
def test_blocks_local_and_private_targets(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url)


def test_allow_private_urls_flag(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_URLS", "true")
    monkeypatch.setattr(url_safety, "_resolve_host", lambda host: ["127.0.0.1"])
    assert validate_url("http://dev.example.com/video") == "http://dev.example.com/video"


def test_allow_private_urls_keeps_scheme_check(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_URLS", "true")
    with pytest.raises(UnsafeURLError):
        validate_url("file:///etc/passwd")


def test_validate_urls_drops_invalid_entries():
    urls = [
        "https://example.com/a",
        "file:///etc/passwd",
        "https://example.com/b",
    ]
    assert validate_urls(urls) == ["https://example.com/a", "https://example.com/b"]
