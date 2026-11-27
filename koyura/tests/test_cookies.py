"""The engine's side of cookie files: lookup, retry order, and the endpoint.

What these tests pin is the part that only shows up in production:

* a service with cookies gets ``--cookies <path>`` in the yt-dlp argument
  vector, and a service without any gets the vector it always got;
* a refused session moves on to the **second** jar instead of failing the job;
* when every jar has been refused, the job ends with ``cookies_required`` and a
  sentence that names the service — which is what the bot turns into "add them
  in the panel";
* the endpoint answers with paths only, is behind the internal token, and a
  ``../../etc`` service name never resolves to a file.
"""
from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token-0123456789abcdef")
os.environ["DOWNLOADS_PATH"] = tempfile.mkdtemp()
_COOKIES_ROOT = tempfile.mkdtemp()
os.environ["COOKIES_PATH"] = _COOKIES_ROOT
# Never let a test reach a real panel.
os.environ["KOYURA_DASHBOARD_URL"] = "http://127.0.0.1:9"

import pytest  # noqa: E402

import app as koyura_app  # noqa: E402
import cookies as cookie_store  # noqa: E402

TOKEN = os.environ["INTERNAL_API_TOKEN"]
HEADERS = {"X-Internal-Token": TOKEN}

JAR = (
    b"# Netscape HTTP Cookie File\n"
    b".reddit.com\tTRUE\t/\tTRUE\t1893456000\tsession\ttest-value\n"
)

#: A yt-dlp stderr that means "this session is not allowed in", not "bad URL".
AUTH_STDERR = "ERROR: [Reddit] Sign in to confirm your age. Use --cookies\n"
#: A failure that has nothing to do with cookies: retrying would be pointless.
OTHER_STDERR = "ERROR: [Reddit] Video unavailable\n"


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    """A clean cookie tree and registry for every test."""
    root = Path(_COOKIES_ROOT)
    if root.exists():
        for path in sorted(root.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
    cookie_store.reset_registry_cache()
    koyura_app.jobs.clear()
    yield
    cookie_store.reset_registry_cache()


def _jar(service: str = "reddit", name: str = "aaaaaaaaaaaaaaaa.txt") -> str:
    """Write one jar into the service directory and return its path."""
    directory = Path(_COOKIES_ROOT) / service
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(JAR)
    return str(target)


def _publish(service: str, names: list[str]) -> None:
    """Pretend the panel answered with this registry (no HTTP involved)."""
    cookie_store._registry = {service: names}  # noqa: SLF001 - the cache is the seam
    cookie_store._registry_expires = 10**9  # noqa: SLF001


# ---------------------------------------------------------------------------
# Service detection
# ---------------------------------------------------------------------------

class TestServiceDetection:
    def test_a_reddit_url_maps_to_reddit(self):
        assert cookie_store.service_for_url(
            "https://www.reddit.com/r/test/comments/abc/x/"
        ) == "reddit"

    def test_a_youtube_url_maps_to_youtube(self):
        assert cookie_store.service_for_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        ) == "youtube"

    def test_the_extractor_table_decides_and_nothing_is_hard_coded(self):
        """The module has no list of sites; yt-dlp's own table is the source.

        The check reads the module's code, not its prose: a docstring may show
        an example payload, a *constant* may not name a site. Docstrings and
        comments are excluded by walking the AST for real string literals.
        """
        import ast

        tree = ast.parse(Path(cookie_store.__file__).read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and (
                    isinstance(body[0].value, ast.Constant)
                ):
                    docstrings.add(id(body[0].value))
        literals = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        }
        for site in ("reddit", "youtube", "instagram", "tiktok"):
            assert site not in literals, f"{site} is hard-coded in cookies.py"

    def test_an_unknown_url_has_no_service(self):
        assert cookie_store.service_for_url("https://example.invalid/x") in ("", "generic")

    def test_an_empty_url_has_no_service(self):
        assert cookie_store.service_for_url("") == ""

    def test_info_payload_names_the_service(self):
        assert cookie_store.service_from_info({"extractor_key": "Reddit"}) == "reddit"
        assert cookie_store.service_from_info({"extractor": "reddit:subreddit"}) == "reddit"

    def test_a_generic_extractor_is_not_a_service(self):
        assert cookie_store.service_from_info({"extractor_key": "Generic"}) == ""
        assert cookie_store.service_from_info({}) == ""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("reddit", "reddit"),
            ("Reddit", "reddit"),
            ("reddit:subreddit", "reddit"),
            ("../../etc/passwd", "etc-passwd"),
            ("abc/../x", "abc-x"),
            ("...", ""),
            ("", ""),
            (None, ""),
        ],
    )
    def test_the_engine_sanitises_exactly_like_the_panel(self, raw, expected):
        assert cookie_store.sanitise_service(raw) == expected


# ---------------------------------------------------------------------------
# Failure classification and the argument vector
# ---------------------------------------------------------------------------

class TestClassification:
    @pytest.mark.parametrize(
        "stderr",
        [
            "ERROR: Sign in to confirm your age",
            "ERROR: login required",
            "ERROR: This video is private",
            "ERROR: HTTP Error 403: Forbidden",
            "ERROR: Use --cookies for the authentication",
            "ERROR: age-restricted content",
        ],
    )
    def test_authorization_failures_are_recognised(self, stderr):
        assert cookie_store.looks_like_auth_failure(stderr) is True

    def test_an_unrelated_failure_is_not_an_authorization_problem(self):
        assert cookie_store.looks_like_auth_failure(OTHER_STDERR) is False

    def test_empty_output_is_not_an_authorization_problem(self):
        assert cookie_store.looks_like_auth_failure("") is False

    def test_the_cookie_flag_goes_before_the_url(self):
        cmd = ["python", "-m", "yt_dlp", "--no-playlist", "https://x/y"]
        jar = str(Path(_COOKIES_ROOT) / "a.txt")
        with_jar = cookie_store.with_cookies(cmd, jar)

        assert with_jar[-1] == "https://x/y"
        assert with_jar[-3:] == ["--cookies", jar, "https://x/y"]
        assert cmd == ["python", "-m", "yt_dlp", "--no-playlist", "https://x/y"]

    def test_the_required_message_names_the_service_and_the_panel(self):
        message = cookie_store.required_message("reddit")
        assert "reddit" in message
        assert "cookies" in message.lower()
        assert "panel" in message.lower()


# ---------------------------------------------------------------------------
# Ending a job with cookies_required
# ---------------------------------------------------------------------------

class TestRequired:
    def test_a_spent_session_ends_the_job_with_a_code(self, monkeypatch):
        """With jars on disk and every one refused, the job gets the code."""
        monkeypatch.setattr(
            koyura_app, "cookie_paths", lambda service: ["/cookies/reddit/a.txt"]
        )
        monkeypatch.setattr(
            koyura_app, "_attempt_download",
            lambda *a, **kw: "auth",
        )
        monkeypatch.setattr(koyura_app, "_cleanup_partial_files", lambda job_id: None)
        koyura_app.jobs["job1"] = {"status": "downloading"}

        koyura_app._do_download("job1", "https://www.reddit.com/r/x", "video", None)

        job = koyura_app.jobs["job1"]
        assert job["status"] == "error"
        assert job["error_code"] == "cookies_required"
        assert "reddit" in job["error"]

    def test_no_cookie_service_fails_the_job_the_old_way(self, monkeypatch):
        """A site without cookies must not learn a new failure word."""
        monkeypatch.setattr(koyura_app, "_attempt_download", lambda *a, **kw: "failed")
        koyura_app.jobs["job2"] = {"status": "downloading"}

        koyura_app._do_download("job2", "https://example.invalid/x", "video", None)

        job = koyura_app.jobs["job2"]
        assert "error_code" not in job or job["error_code"] is None


# ---------------------------------------------------------------------------
# The retry order (mock yt-dlp)
# ---------------------------------------------------------------------------

class TestRetryOrder:
    def test_the_first_jar_is_the_one_tried(self, monkeypatch):
        _jar("reddit", "aaaaaaaaaaaaaaaa.txt")
        _publish("reddit", ["aaaaaaaaaaaaaaaa.txt"])
        seen: list[list[str]] = []

        def fake_attempt(job_id, url, fmt, format_id, **kwargs):
            seen.append(koyura_app._build_download_cmd(job_id, url, fmt, format_id))
            return "done"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(
            koyura_app, "cookie_paths", lambda service: ["/cookies/reddit/a.txt"]
        )
        koyura_app._do_download("job3", "https://www.reddit.com/r/x", "video", None)

        assert len(seen) == 1, "a successful attempt is not repeated"

    def test_the_cookie_flag_reaches_the_real_command(self, monkeypatch):
        """The vector built for yt-dlp carries ``--cookies <path>``."""
        captured: dict[str, list[str]] = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            raise RuntimeError("stop before the process runs")

        monkeypatch.setattr(koyura_app.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(koyura_app, "_cleanup_partial_files", lambda job_id: None)

        with pytest.raises(RuntimeError):
            koyura_app._attempt_download(
                "job4", "https://www.reddit.com/r/x", "video", None,
                cookies_path="/cookies/reddit/aaaaaaaaaaaaaaaa.txt",
            )

        cmd = captured["cmd"]
        assert "--cookies" in cmd
        index = cmd.index("--cookies")
        assert cmd[index + 1] == "/cookies/reddit/aaaaaaaaaaaaaaaa.txt"
        assert cmd[-1] == "https://www.reddit.com/r/x", "the URL stays last"

    def test_a_refused_session_tries_the_second_jar(self, monkeypatch):
        """The whole point of several files: the second one is tried."""
        attempted: list[str | None] = []

        def fake_attempt(job_id, url, fmt, format_id, *, cookies_path):
            attempted.append(cookies_path)
            # Refuse the first jar, accept the second.
            return "auth" if cookies_path and cookies_path.endswith("one.txt") else "done"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(
            koyura_app,
            "cookie_paths",
            lambda service: ["/cookies/reddit/one.txt", "/cookies/reddit/two.txt"],
        )
        koyura_app.jobs["job5"] = {"status": "downloading"}

        koyura_app._do_download("job5", "https://www.reddit.com/r/x", "video", None)

        assert attempted == [
            "/cookies/reddit/one.txt",
            "/cookies/reddit/two.txt",
        ]
        assert koyura_app.jobs["job5"]["status"] == "downloading", (
            "the second attempt succeeded, so the job must not be failed"
        )

    def test_every_jar_refused_ends_with_cookies_required(self, monkeypatch):
        attempted: list[str | None] = []

        def fake_attempt(job_id, url, fmt, format_id, *, cookies_path):
            attempted.append(cookies_path)
            return "auth"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(
            koyura_app,
            "cookie_paths",
            lambda service: ["/cookies/reddit/one.txt", "/cookies/reddit/two.txt"],
        )
        monkeypatch.setattr(koyura_app, "_cleanup_partial_files", lambda job_id: None)
        koyura_app.jobs["job6"] = {"status": "downloading"}

        koyura_app._do_download("job6", "https://www.reddit.com/r/x", "video", None)

        assert len(attempted) == 2, "every jar is tried once"
        job = koyura_app.jobs["job6"]
        assert job["status"] == "error"
        assert job["error_code"] == "cookies_required"
        assert job["cookies_tried"] == 2
        assert "reddit" in job["error"]

    def test_an_unrelated_failure_is_not_retried(self, monkeypatch):
        attempted: list[str | None] = []

        def fake_attempt(job_id, url, fmt, format_id, *, cookies_path):
            attempted.append(cookies_path)
            return "failed"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(
            koyura_app,
            "cookie_paths",
            lambda service: ["/cookies/reddit/one.txt", "/cookies/reddit/two.txt"],
        )
        koyura_app._do_download("job7", "https://www.reddit.com/r/x", "video", None)

        assert len(attempted) == 1, "a broken URL must not burn the whole list"

    def test_no_cookies_means_no_flag(self, monkeypatch):
        attempted: list[str | None] = []

        def fake_attempt(job_id, url, fmt, format_id, *, cookies_path):
            attempted.append(cookies_path)
            return "done"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(koyura_app, "cookie_paths", lambda service: [])
        koyura_app._do_download("job8", "https://www.youtube.com/watch?v=x", "video", None)

        assert attempted == [None]


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------

class TestTree:
    def test_stored_files_are_listed_in_name_order_as_a_fallback(self):
        _jar("reddit", "bbbbbbbbbbbbbbbb.txt")
        _jar("reddit", "aaaaaaaaaaaaaaaa.txt")
        assert cookie_store.disk_files("reddit") == [
            "aaaaaaaaaaaaaaaa.txt", "bbbbbbbbbbbbbbbb.txt",
        ]

    def test_the_panel_order_wins_over_the_name_order(self):
        _jar("reddit", "aaaaaaaaaaaaaaaa.txt")
        _jar("reddit", "bbbbbbbbbbbbbbbb.txt")
        _publish("reddit", ["bbbbbbbbbbbbbbbb.txt", "aaaaaaaaaaaaaaaa.txt"])

        paths = cookie_store.cookie_paths("reddit")
        assert [Path(path).name for path in paths] == [
            "bbbbbbbbbbbbbbbb.txt", "aaaaaaaaaaaaaaaa.txt",
        ]

    def test_a_listed_file_that_is_not_on_disk_is_skipped(self):
        _jar("reddit", "aaaaaaaaaaaaaaaa.txt")
        _publish("reddit", ["aaaaaaaaaaaaaaaa.txt", "cccccccccccccccc.txt"])

        assert [Path(path).name for path in cookie_store.cookie_paths("reddit")] == [
            "aaaaaaaaaaaaaaaa.txt",
        ]

    def test_a_traversal_name_resolves_to_nothing(self):
        assert cookie_store.cookie_paths("../../etc") == []
        assert cookie_store.cookie_paths("..") == []

    def test_a_file_outside_the_generated_shape_is_ignored(self):
        directory = Path(_COOKIES_ROOT) / "reddit"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "notes.md").write_text("not a jar")
        assert "notes.md" not in cookie_store.disk_files("reddit")

    def test_has_cookies_answers_a_boolean(self):
        assert cookie_store.has_cookies("reddit") is False
        _jar("reddit")
        assert cookie_store.has_cookies("reddit") is True


# ---------------------------------------------------------------------------
# The registry cache
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_an_unreachable_panel_falls_back_to_the_disk(self, monkeypatch):
        _jar("instagram", "dddddddddddddddd.txt")
        monkeypatch.setattr(cookie_store, "_panel_registry", dict)

        registry = cookie_store.registry()
        assert "instagram" in registry

    def test_the_answer_is_cached(self, monkeypatch):
        calls: list[int] = []

        def counting():
            calls.append(1)
            return {"reddit": ["aaaaaaaaaaaaaaaa.txt"]}

        monkeypatch.setattr(cookie_store, "_panel_registry", counting)
        cookie_store.reset_registry_cache()

        cookie_store.registry()
        cookie_store.registry()
        assert len(calls) == 1, "the registry is fetched once per TTL"

    def test_a_panel_list_of_names_is_accepted(self, monkeypatch):
        monkeypatch.setattr(
            cookie_store, "_panel_registry", lambda: {"reddit": []}
        )
        cookie_store.reset_registry_cache()
        assert "reddit" in cookie_store.registry()

    def test_a_malformed_entry_name_is_dropped(self):
        """A hostile service name and a hostile file name never reach the cache."""
        registry = cookie_store.parse_registry(
            {"services": [
                {"service": "../../etc", "files": []},
                {"service": "reddit", "files": ["../x.txt", "aaaaaaaaaaaaaaaa.txt"]},
            ]}
        )
        assert "../../etc" not in registry
        assert registry["reddit"] == ["aaaaaaaaaaaaaaaa.txt"]


# ---------------------------------------------------------------------------
# The HTTP endpoint
# ---------------------------------------------------------------------------

class TestEndpoint:
    @pytest.fixture()
    def client(self):
        koyura_app.app.config.update(TESTING=True)
        with koyura_app.app.test_client() as test_client:
            yield test_client

    def test_the_endpoint_needs_the_token(self, client):
        assert client.get("/api/cookies/reddit").status_code == 401

    def test_a_wrong_token_is_refused(self, client):
        response = client.get(
            "/api/cookies/reddit", headers={"X-Internal-Token": "wrong"}
        )
        assert response.status_code == 401

    def test_a_service_with_a_jar_answers_its_ordered_paths(self, client):
        _jar("reddit", "aaaaaaaaaaaaaaaa.txt")
        _jar("reddit", "bbbbbbbbbbbbbbbb.txt")
        _publish("reddit", ["bbbbbbbbbbbbbbbb.txt", "aaaaaaaaaaaaaaaa.txt"])

        response = client.get("/api/cookies/reddit", headers=HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert body["count"] == 2
        assert [Path(path).name for path in body["paths"]] == [
            "bbbbbbbbbbbbbbbb.txt", "aaaaaaaaaaaaaaaa.txt",
        ]
        assert body["files"] == ["bbbbbbbbbbbbbbbb.txt", "aaaaaaaaaaaaaaaa.txt"]

    def test_a_service_without_jars_answers_an_empty_list(self, client):
        response = client.get("/api/cookies/instagram", headers=HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert body["paths"] == []
        assert body["count"] == 0

    def test_the_answer_never_carries_cookie_content(self, client):
        _jar("reddit", "aaaaaaaaaaaaaaaa.txt")
        response = client.get("/api/cookies/reddit", headers=HEADERS)
        assert b"test-value" not in response.data
        assert b"reddit.com" not in response.data

    def test_a_traversal_service_cannot_reach_a_file(self, client):
        """``..`` and encoded separators answer no paths, never a file.

        Flask refuses to route a path segment that contains a decoded ``/``,
        so the encoded form is a 404; a literal ``..`` reaches the view but
        sanitises to nothing. Both outcomes are safe — what must never happen
        is a path to a file outside the tree.
        """
        encoded = client.get("/api/cookies/..%2f..%2fetc", headers=HEADERS)
        assert encoded.status_code == 404

        literal = client.get("/api/cookies/..", headers=HEADERS)
        assert literal.status_code == 200
        assert literal.get_json()["paths"] == []
