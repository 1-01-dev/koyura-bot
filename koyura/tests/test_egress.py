"""The engine's egress: the rule, the cache, ``--proxy`` and `blocked`.

What these tests pin is the part that only shows up in production:

* the panel's answer is read **tolerantly** — an unknown mode, a URL with a
  scheme yt-dlp cannot dial, or no panel at all means "direct", never a failed
  job;
* the assignment rule decides **per service**, in all three shapes, using the
  same extractor table the cookie lookup uses;
* the ``--proxy`` flag reaches the real yt-dlp argument vector, before the URL,
  and a WireGuard rule without a live tunnel adds nothing;
* a datacentre block (`blocked`, `429`, `captcha`, Reddit's API 403) is
  classified as ``blocked`` — before ``auth``, because another cookie jar
  answers the same 403 — and ends the job with ``service_blocked``.

The panel is never reached: ``KOYURA_DASHBOARD_URL`` points at a closed port.
"""
from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token-0123456789abcdef")
os.environ["DOWNLOADS_PATH"] = tempfile.mkdtemp()
# ``COOKIES_PATH`` is deliberately not touched: ``test_cookies.py`` owns that
# variable for the whole session, and a second module assigning it would point
# every cookie test at the wrong tree.
# Never let a test reach a real panel.
os.environ["KOYURA_DASHBOARD_URL"] = "http://127.0.0.1:9"

import pytest  # noqa: E402

import app as koyura_app  # noqa: E402
import cookies as cookie_store  # noqa: E402
import egress  # noqa: E402

TOKEN = os.environ["INTERNAL_API_TOKEN"]
HEADERS = {"X-Internal-Token": TOKEN}

PROXY = "socks5h://user:secret@10.0.0.5:1080"
SOCKS = "10.8.0.2:11080"

#: The exact strings the task recorded on the server.
REDDIT_BLOCKED = (
    "ERROR: [Reddit] reddit.com:456: Unable to download webpage: "
    "HTTP Error 403: Blocked (caused by HTTPError()); "
    "please report this issue on https://github.com/yt-dlp/yt-dlp/issues "
    "(Your IP address is unable to access the Reddit API)"
)
BLOCKED_403 = "ERROR: [Reddit] Unable to download JSON metadata: HTTP Error 403: Blocked"


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    """A clean rule, cache and job table for every test."""
    egress.reset_cache()
    cookie_store.reset_registry_cache()
    koyura_app.jobs.clear()
    yield
    egress.reset_cache()
    cookie_store.reset_registry_cache()
    koyura_app.jobs.clear()


def _payload(**overrides) -> dict:
    payload = {
        "mode": "manual",
        "proxy_url": PROXY,
        "wg_socks": "",
        "assignment": {"mode": "all", "services": []},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Reading the panel's answer
# ---------------------------------------------------------------------------

class TestRuleParsing:
    def test_the_documented_shape_is_read(self):
        rule = egress.Rule.from_payload(_payload(assignment={"mode": "selected", "services": ["reddit"]}))
        assert rule.mode == "manual"
        assert rule.proxy_url == PROXY
        assert rule.assignment_mode == "selected"
        assert rule.services == ("reddit",)

    def test_an_unreadable_payload_is_a_direct_connection(self):
        for payload in (None, [], "manual", 3, {}):
            rule = egress.Rule.from_payload(payload)
            assert rule.mode == "off", payload
            assert rule.proxy_for("reddit") == ""

    def test_an_unknown_mode_is_off(self):
        assert egress.Rule.from_payload(_payload(mode="tunnel")).mode == "off"

    def test_an_unknown_scheme_is_dropped(self):
        rule = egress.Rule.from_payload(_payload(proxy_url="ftp://10.0.0.5:1"))
        assert rule.proxy_url == ""

    def test_a_url_with_whitespace_is_dropped(self):
        rule = egress.Rule.from_payload(_payload(proxy_url="socks5h://x y:1"))
        assert rule.proxy_url == ""

    @pytest.mark.parametrize(
        "scheme", ["socks5", "socks5h", "http", "https"]
    )
    def test_all_four_schemes_survive(self, scheme: str):
        url = f"{scheme}://10.0.0.5:1080"
        assert egress.Rule.from_payload(_payload(proxy_url=url)).proxy_url == url

    def test_an_unknown_assignment_mode_becomes_all(self):
        rule = egress.Rule.from_payload(_payload(assignment={"mode": "some", "services": []}))
        assert rule.assignment_mode == "all"

    def test_a_missing_assignment_block_becomes_all(self):
        payload = _payload()
        payload.pop("assignment")
        assert egress.Rule.from_payload(payload).assignment_mode == "all"

    def test_a_bare_socks_address_gets_socks5h(self):
        payload = _payload(mode="wireguard", proxy_url="", wg_socks=SOCKS)
        assert egress.Rule.from_payload(payload).wg_socks == f"socks5h://{SOCKS}"

    def test_a_full_socks_url_is_kept(self):
        payload = _payload(mode="wireguard", proxy_url="", wg_socks=f"socks5://{SOCKS}")
        assert egress.Rule.from_payload(payload).wg_socks == f"socks5://{SOCKS}"

    def test_an_unknown_socks_scheme_is_dropped(self):
        payload = _payload(mode="wireguard", proxy_url="", wg_socks=f"ftp://{SOCKS}")
        assert egress.Rule.from_payload(payload).wg_socks == ""


# ---------------------------------------------------------------------------
# The assignment rule
# ---------------------------------------------------------------------------

class TestAssignment:
    def test_all_covers_every_service(self):
        rule = egress.Rule.from_payload(_payload())
        for service in ("reddit", "youtube", "", "anything"):
            assert rule.applies_to(service) is True

    def test_selected_covers_only_the_list(self):
        rule = egress.Rule.from_payload(
            _payload(assignment={"mode": "selected", "services": ["reddit"]})
        )
        assert rule.applies_to("reddit") is True
        assert rule.applies_to("youtube") is False
        assert rule.applies_to("") is False

    def test_except_covers_everything_but_the_list(self):
        rule = egress.Rule.from_payload(
            _payload(assignment={"mode": "except", "services": ["youtube"]})
        )
        assert rule.applies_to("reddit") is True
        assert rule.applies_to("youtube") is False
        assert rule.applies_to("") is True

    def test_off_never_covers_anything(self):
        rule = egress.Rule.off()
        assert rule.applies_to("reddit") is False
        assert rule.proxy_for("reddit") == ""

    def test_manual_answers_with_the_url(self):
        assert egress.Rule.from_payload(_payload()).proxy_for("reddit") == PROXY

    def test_wireguard_answers_with_the_tunnel(self):
        rule = egress.Rule.from_payload(
            _payload(mode="wireguard", proxy_url="", wg_socks=SOCKS)
        )
        assert rule.proxy_for("reddit") == f"socks5h://{SOCKS}"

    def test_a_wireguard_rule_without_a_tunnel_is_direct(self):
        """A dead tunnel must never become a half-configured proxy."""
        rule = egress.Rule.from_payload(_payload(mode="wireguard", proxy_url="", wg_socks=""))
        assert rule.proxy_for("reddit") == ""

    def test_the_service_name_is_normalised(self):
        rule = egress.Rule.from_payload(
            _payload(assignment={"mode": "selected", "services": ["reddit"]})
        )
        assert rule.applies_to("Reddit") is True
        assert rule.applies_to(" reddit ") is True


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

class TestCache:
    def test_an_unreachable_panel_means_direct(self):
        assert egress.rule().mode == "off"

    def test_a_fetched_rule_is_cached(self, monkeypatch):
        calls: list[int] = []

        def _fetch():
            calls.append(1)
            return egress.Rule.from_payload(_payload())

        monkeypatch.setattr(egress, "_fetch", _fetch)
        first = egress.rule()
        second = egress.rule()
        assert first.mode == "manual"
        assert second is first
        assert len(calls) == 1, "a second call inside the TTL must not refetch"

    def test_a_failed_fetch_keeps_the_last_good_rule(self, monkeypatch):
        monkeypatch.setattr(
            egress, "_fetch", lambda: egress.Rule.from_payload(_payload())
        )
        assert egress.rule().mode == "manual"
        # Expire the cache, then make the panel vanish.
        monkeypatch.setattr(egress, "_expires", 0.0)
        monkeypatch.setattr(egress, "_fetch", lambda: None)
        assert egress.rule().mode == "manual", "a panel restart must not reroute jobs"

    def test_reset_forces_a_refetch(self, monkeypatch):
        """``reset_cache`` drops the answer; the next call asks the panel again.

        It does not pin "off": the panel is the authority, so a reset followed
        by a reachable panel must show that panel's rule, not a direct fallback.
        """
        calls: list[int] = []

        def _fetch():
            calls.append(1)
            return egress.Rule.from_payload(_payload())

        monkeypatch.setattr(egress, "_fetch", _fetch)
        assert egress.rule().mode == "manual"
        egress.reset_cache()
        assert egress.rule().mode == "manual"
        assert len(calls) == 2, "the cache was dropped, so the panel was asked again"

    def test_reset_without_a_panel_falls_back_to_direct(self):
        egress.reset_cache()
        assert egress.rule().mode == "off"

    def test_the_ttl_is_under_a_minute(self):
        assert 30 <= egress.CACHE_TTL_SECONDS <= 60

    def test_describe_names_the_mode_and_scope_but_never_the_url(self):
        rule = egress.Rule.from_payload(
            _payload(assignment={"mode": "selected", "services": ["reddit"]})
        )
        described = rule.describe()
        assert "manual" in described
        assert "reddit" in described
        assert "secret" not in described
        assert PROXY not in described


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

class TestEndpoint:
    def test_it_answers_the_rule(self, monkeypatch):
        monkeypatch.setattr(
            egress, "_fetch", lambda: egress.Rule.from_payload(_payload())
        )
        client = koyura_app.app.test_client()
        response = client.get("/api/egress", headers=HEADERS)
        assert response.status_code == 200
        assert response.get_json() == _payload()

    def test_it_needs_the_internal_token(self, monkeypatch):
        monkeypatch.setattr(
            koyura_app, "INTERNAL_API_AUTH_DISABLED", False, raising=False
        )
        client = koyura_app.app.test_client()
        assert client.get("/api/egress").status_code == 401

    def test_an_off_rule_answers_the_documented_shape(self):
        body = koyura_app.app.test_client().get(
            "/api/egress", headers=HEADERS
        ).get_json()
        assert body == {
            "mode": "off",
            "proxy_url": "",
            "wg_socks": "",
            "assignment": {"mode": "all", "services": []},
        }


# ---------------------------------------------------------------------------
# The argument vector
# ---------------------------------------------------------------------------

class TestCommand:
    def test_the_proxy_flag_goes_before_the_url(self):
        cmd = ["python", "-m", "yt_dlp", "--no-playlist", "https://x/y"]
        with_proxy = egress.with_proxy(cmd, PROXY)
        assert with_proxy[-1] == "https://x/y"
        assert with_proxy[-3:] == ["--proxy", PROXY, "https://x/y"]
        assert cmd == ["python", "-m", "yt_dlp", "--no-playlist", "https://x/y"]

    def test_an_empty_command_still_gets_the_flag(self):
        assert egress.with_proxy([], PROXY) == ["--proxy", PROXY]

    def test_the_flag_and_the_cookie_flag_coexist(self):
        """Both are options in the option block, and the URL stays last."""
        cmd = koyura_app._build_download_cmd(
            "job1", "https://www.reddit.com/r/x", "video", None
        )
        cmd = cookie_store.with_cookies(cmd, "/cookies/reddit/a.txt")
        cmd = egress.with_proxy(cmd, PROXY)
        assert cmd[-1] == "https://www.reddit.com/r/x"
        assert "--cookies" in cmd
        assert "--proxy" in cmd
        assert cmd[cmd.index("--proxy") + 1] == PROXY


# ---------------------------------------------------------------------------
# Which URL gets the proxy
# ---------------------------------------------------------------------------

class TestProxyForUrl:
    def test_a_manual_rule_covers_a_reddit_url(self, monkeypatch):
        monkeypatch.setattr(
            egress, "_fetch", lambda: egress.Rule.from_payload(_payload())
        )
        assert egress.proxy_for_url("https://www.reddit.com/r/x") is not None
        assert egress.proxy_for_url("https://www.reddit.com/r/x") != ""

    def test_a_selected_rule_skips_an_unlisted_service(self, monkeypatch):
        monkeypatch.setattr(
            egress,
            "_fetch",
            lambda: egress.Rule.from_payload(
                _payload(assignment={"mode": "selected", "services": ["reddit"]})
            ),
        )
        assert egress.proxy_for_url("https://www.reddit.com/r/x") != ""
        assert egress.proxy_for_url("https://www.youtube.com/watch?v=x") == ""

    def test_an_except_rule_skips_the_excluded_service(self, monkeypatch):
        monkeypatch.setattr(
            egress,
            "_fetch",
            lambda: egress.Rule.from_payload(
                _payload(assignment={"mode": "except", "services": ["youtube"]})
            ),
        )
        assert egress.proxy_for_url("https://www.reddit.com/r/x") != ""
        assert egress.proxy_for_url("https://www.youtube.com/watch?v=x") == ""

    def test_an_unreachable_panel_means_direct(self):
        assert egress.proxy_for_url("https://www.reddit.com/r/x") == ""

    def test_a_broken_rule_never_breaks_a_download(self, monkeypatch):
        def _boom(_url):
            raise RuntimeError("boom")

        monkeypatch.setattr(cookie_store, "service_for_url", _boom)
        assert egress.proxy_for_url("https://www.reddit.com/r/x") == ""


# ---------------------------------------------------------------------------
# The flag reaching the real vector
# ---------------------------------------------------------------------------

class TestJobCommand:
    def test_the_proxy_reaches_the_real_command(self, monkeypatch):
        captured: dict[str, list[str]] = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            raise RuntimeError("stop before the process runs")

        monkeypatch.setattr(koyura_app.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(koyura_app, "_cleanup_partial_files", lambda job_id: None)

        with pytest.raises(RuntimeError):
            koyura_app._attempt_download(
                "job2", "https://www.reddit.com/r/x", "video", None,
                cookies_path=None, proxy_url=PROXY,
            )

        cmd = captured["cmd"]
        assert "--proxy" in cmd
        assert cmd[cmd.index("--proxy") + 1] == PROXY
        assert cmd[-1] == "https://www.reddit.com/r/x"

    def test_no_proxy_means_no_flag(self, monkeypatch):
        captured: dict[str, list[str]] = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            raise RuntimeError("stop")

        monkeypatch.setattr(koyura_app.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(koyura_app, "_cleanup_partial_files", lambda job_id: None)

        with pytest.raises(RuntimeError):
            koyura_app._attempt_download(
                "job3", "https://www.youtube.com/watch?v=x", "video", None,
                cookies_path=None, proxy_url="",
            )

        assert "--proxy" not in captured["cmd"]

    def test_do_download_passes_the_rule_to_every_attempt(self, monkeypatch):
        seen: list[str | None] = []

        def fake_attempt(job_id, url, fmt, format_id, *, cookies_path, proxy_url=None):
            seen.append(proxy_url)
            return "done"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(koyura_app, "cookie_paths", lambda service: [])
        monkeypatch.setattr(egress, "proxy_for_url", lambda url: PROXY)
        koyura_app._do_download("job4", "https://www.reddit.com/r/x", "video", None)

        assert seen == [PROXY]

    def test_the_proxy_travels_with_each_cookie_retry(self, monkeypatch):
        """Both flags have to be on every attempt, not just the first."""
        seen: list[tuple[str | None, str | None]] = []

        def fake_attempt(job_id, url, fmt, format_id, *, cookies_path, proxy_url=None):
            seen.append((cookies_path, proxy_url))
            return "auth" if str(cookies_path).endswith("one.txt") else "done"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(
            koyura_app, "cookie_paths",
            lambda service: ["/cookies/reddit/one.txt", "/cookies/reddit/two.txt"],
        )
        monkeypatch.setattr(egress, "proxy_for_url", lambda url: PROXY)
        koyura_app.jobs["job5"] = {"status": "downloading"}
        koyura_app._do_download("job5", "https://www.reddit.com/r/x", "video", None)

        assert seen == [
            ("/cookies/reddit/one.txt", PROXY),
            ("/cookies/reddit/two.txt", PROXY),
        ]


# ---------------------------------------------------------------------------
# blocked vs auth
# ---------------------------------------------------------------------------

class TestBlockedClassification:
    @pytest.mark.parametrize(
        "stderr",
        [
            REDDIT_BLOCKED,
            BLOCKED_403,
            "ERROR: HTTP Error 429: Too Many Requests",
            "ERROR: 429 Too Many Requests",
            "ERROR: [Reddit] blocked",
            "ERROR: Please solve the captcha",
        ],
    )
    def test_a_refused_address_is_blocked(self, stderr: str):
        assert cookie_store.looks_blocked(stderr) is True

    @pytest.mark.parametrize(
        "stderr",
        [
            "ERROR: Sign in to confirm your age",
            "ERROR: login required",
            "ERROR: This video is private",
            "ERROR: Video unavailable",
            "",
        ],
    )
    def test_a_session_problem_is_not_blocked(self, stderr: str):
        assert cookie_store.looks_blocked(stderr) is False

    def test_the_real_reddit_block_is_reported_as_a_proxy_problem(self):
        """The task's exact string: the advice has to be "proxy", not "cookies"."""
        message = cookie_store.blocked_message("reddit")
        assert "reddit" in message
        assert "proxy" in message.lower()
        assert "cookies will not help" in message.lower()

    def test_the_blocked_message_names_the_panel_page(self):
        assert "Proxy" in cookie_store.blocked_message("reddit")

    def test_an_unrelated_failure_keeps_the_plain_error(self):
        assert cookie_store.looks_like_auth_failure("ERROR: Video unavailable") is False


class TestBlockedJob:
    def _run(self, monkeypatch, stderr: str) -> str:
        """Run one attempt with a fake yt-dlp that exits 1 with ``stderr``."""

        class _FakeProcess:
            def __init__(self):
                self.stdout = iter(())
                self.stderr = iter([stderr])
                self.returncode = 1

            def wait(self, timeout=None):
                return 1

            def poll(self):
                return 1

        monkeypatch.setattr(
            koyura_app.subprocess, "Popen", lambda *a, **k: _FakeProcess()
        )
        monkeypatch.setattr(koyura_app, "_cleanup_partial_files", lambda job_id: None)
        koyura_app.jobs["job9"] = {"status": "downloading"}
        return koyura_app._attempt_download(
            "job9", "https://www.reddit.com/r/x", "video", None,
            cookies_path="/cookies/reddit/a.txt", proxy_url="",
        )

    def test_a_blocked_address_ends_the_attempt(self, monkeypatch):
        assert self._run(monkeypatch, REDDIT_BLOCKED) == "blocked"

    def test_the_job_carries_the_code_and_the_sentence(self, monkeypatch):
        self._run(monkeypatch, REDDIT_BLOCKED)
        job = koyura_app.jobs["job9"]
        assert job["status"] == "error"
        assert job["error_code"] == "service_blocked"
        assert "proxy" in job["error"].lower()
        assert job["cookies_service"] == "reddit"

    def test_a_blocked_job_does_not_try_the_next_jar(self, monkeypatch):
        """Another jar answers the same 403 — retrying only costs a minute."""
        attempted: list[str | None] = []

        def fake_attempt(job_id, url, fmt, format_id, *, cookies_path, proxy_url=None):
            attempted.append(cookies_path)
            return "blocked"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(
            koyura_app, "cookie_paths",
            lambda service: ["/cookies/reddit/one.txt", "/cookies/reddit/two.txt"],
        )
        koyura_app.jobs["job10"] = {"status": "downloading"}
        koyura_app._do_download("job10", "https://www.reddit.com/r/x", "video", None)

        assert attempted == ["/cookies/reddit/one.txt"]

    def test_a_session_problem_still_tries_the_next_jar(self, monkeypatch):
        """The 403 that is *not* a block must keep the cookie retry."""
        attempted: list[str | None] = []

        def fake_attempt(job_id, url, fmt, format_id, *, cookies_path, proxy_url=None):
            attempted.append(cookies_path)
            return "auth"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(
            koyura_app, "cookie_paths",
            lambda service: ["/cookies/reddit/one.txt", "/cookies/reddit/two.txt"],
        )
        koyura_app.jobs["job11"] = {"status": "downloading"}
        koyura_app._do_download("job11", "https://www.reddit.com/r/x", "video", None)

        assert attempted == ["/cookies/reddit/one.txt", "/cookies/reddit/two.txt"]
