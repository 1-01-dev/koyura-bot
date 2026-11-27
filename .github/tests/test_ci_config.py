"""Guards for the deployment configuration: workflows, compose and images.

These tests never run Docker. They read the YAML and the Dockerfiles and assert
the invariants the project depends on, so a config-only change (the kind that
does not touch Python and therefore feels safe) still fails CI when it breaks
one of them:

* CI runs every suite, including this one;
* images are built from the service directory and pushed under the repository
  name with both a version tag and ``latest``, never from a prerelease;
* compose publishes nothing but the dashboard, and only on the loopback;
* every container drops capabilities, runs unprivileged and restarts;
* no image, context or commit carries a secret.
"""
from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github/workflows"
CI_PATH = WORKFLOWS / "ci.yml"
RELEASE_PATH = WORKFLOWS / "release.yml"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

#: Services that are built from this repository (the Bot API is upstream).
BUILT_SERVICES = ["koyura-engine", "bot", "dashboard"]

#: Services that speak to Telegram. They must never be published.
INTERNAL_SERVICES = ["koyura-engine", "bot", "telegram-bot-api"]


def load(path: Path) -> dict:
    with open(path) as handle:
        document = yaml.safe_load(handle)
    assert isinstance(document, dict), f"{path.name} does not parse to a mapping"
    return document


@pytest.fixture(scope="module")
def compose() -> dict:
    return load(COMPOSE_PATH)


@pytest.fixture(scope="module")
def release() -> dict:
    return load(RELEASE_PATH)


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _step_with(job: dict, key: str) -> dict:
    """The single step that passes ``key`` to its action."""
    matches = [
        step for step in _steps(job) if key in (step.get("with") or {})
    ]
    assert matches, f"no step passes {key!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# Workflows parse and cover every service
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [CI_PATH, RELEASE_PATH])
def test_workflow_is_valid_yaml(path: Path):
    workflow = load(path)
    assert "jobs" in workflow and workflow["jobs"], f"{path.name} has no jobs"


def test_compose_is_valid_yaml(compose):
    assert "services" in compose


def image_prefix(release) -> str:
    """The ``ghcr.io/<owner>/koyura-bot/<service>`` prefix the build step uses."""
    step = _step_with(release["jobs"]["build-and-push"], "tags")
    prefix = (step.get("env") or {}).get("IMAGE", "")
    assert prefix.startswith("ghcr.io/"), prefix
    return prefix


def _release_matrix(release) -> dict[str, str]:
    """The release matrix as {published image name: build directory}."""
    matrix = release["jobs"]["build-and-push"]["strategy"]["matrix"]
    return {entry["service"]: entry["directory"] for entry in matrix["include"]}


def test_release_matrix_builds_exactly_the_built_services(release):
    assert set(_release_matrix(release)) == set(BUILT_SERVICES)


def test_release_never_builds_the_upstream_bot_api(release):
    assert "telegram-bot-api" not in _release_matrix(release)


def test_ci_runs_all_four_suites():
    """bot, dashboard, engine and this config suite each need their own job."""
    matrix = load(CI_PATH)["jobs"]["tests"]["strategy"]["matrix"]
    services = {entry["service"] for entry in matrix["include"]}
    assert services == {"bot", "dashboard", "koyura-engine", "config"}

    for entry in matrix["include"]:
        assert (REPO_ROOT / entry["working-directory"]).is_dir()
        assert entry["test-path"].endswith("/")


def test_ci_installs_the_requirements_it_declares():
    matrix = load(CI_PATH)["jobs"]["tests"]["strategy"]["matrix"]
    for entry in matrix["include"]:
        requirements = REPO_ROOT / entry["working-directory"] / entry["requirements"]
        assert requirements.is_file(), f"{requirements} is missing"


# ---------------------------------------------------------------------------
# Build context and image naming
# ---------------------------------------------------------------------------

def test_release_builds_each_service_from_its_own_directory(release):
    """A matrix entry without a directory would build ./<image-name> and fail."""
    step = _step_with(release["jobs"]["build-and-push"], "context")
    assert step["with"]["context"] == "./${{ matrix.directory }}"

    for service, directory in _release_matrix(release).items():
        assert (REPO_ROOT / directory / "Dockerfile").is_file(), (
            f"{service} builds from {directory!r}, which has no Dockerfile"
        )


#: Where each built service lives; the engine image is published under its
#: compose service name, which differs from its directory.
SERVICE_DIRECTORIES = {"koyura-engine": "koyura", "bot": "bot", "dashboard": "dashboard"}


def test_compose_build_context_matches_the_service(compose):
    for service, directory in SERVICE_DIRECTORIES.items():
        context = compose["services"][service]["build"]
        assert context == f"./{directory}", f"{service} builds from {context!r}"


def test_compose_images_come_from_ghcr_under_the_repository_name(compose):
    for service in BUILT_SERVICES:
        image = compose["services"][service]["image"]
        assert image.startswith("ghcr.io/"), image
        assert "/koyura-bot/" in image, image
        assert image.endswith(f"/{service}:${{IMAGE_TAG:-latest}}"), image


def test_release_tags_are_versioned_and_also_latest(release):
    tags = _step_with(release["jobs"]["build-and-push"], "tags")["with"]["tags"]

    assert "ghcr.io/" in image_prefix(release)
    assert ":${{ steps.version.outputs.version }}" in tags
    assert ":latest" in tags


def test_release_derives_the_image_path_from_the_repository(release):
    """A hard-coded owner would break every fork."""
    assert "${{ steps.repo.outputs.repo }}" in image_prefix(release)

    lowercase = next(
        step for step in _steps(release["jobs"]["build-and-push"])
        if step.get("id") == "repo"
    )
    assert "tr '[:upper:]' '[:lower:]'" in lowercase["run"]


def test_release_passes_the_version_as_a_build_argument(release):
    step = _step_with(release["jobs"]["build-and-push"], "build-args")
    assert step["with"]["build-args"] == "VERSION=${{ steps.version.outputs.version }}"


def test_prereleases_are_not_published(release):
    job = release["jobs"]["build-and-push"]

    assert "if" in job, "the job must be guarded"
    assert "!github.event.release.prerelease" in job["if"]


def test_release_is_triggered_by_a_published_release(release):
    # ``on`` is parsed as True by YAML 1.1 if unquoted, so accept both keys.
    trigger = release.get("on") or release.get(True)
    assert trigger["release"]["types"] == ["published"]


# ---------------------------------------------------------------------------
# Compose hardening
# ---------------------------------------------------------------------------

def test_only_the_dashboard_publishes_a_port(compose):
    for service in INTERNAL_SERVICES:
        assert "ports" not in compose["services"][service], service


def test_the_dashboard_port_is_loopback_only(compose):
    ports = compose["services"]["dashboard"]["ports"]

    assert ports, "the dashboard must be reachable"
    for mapping in ports:
        assert str(mapping).startswith("127.0.0.1:"), mapping


@pytest.mark.parametrize("service", ["koyura-engine", "bot", "telegram-bot-api", "dashboard"])
def test_every_service_drops_capabilities(compose, service):
    definition = compose["services"][service]

    assert "ALL" in definition.get("cap_drop", []), service
    assert "no-new-privileges:true" in definition.get("security_opt", []), service


@pytest.mark.parametrize("service", ["koyura-engine", "bot", "telegram-bot-api", "dashboard"])
def test_every_service_restarts(compose, service):
    assert compose["services"][service]["restart"] == "unless-stopped"


def test_the_internal_token_reaches_every_internal_service(compose):
    for service in BUILT_SERVICES:
        environment = compose["services"][service]["environment"]
        assert "INTERNAL_API_TOKEN" in environment, service
        # Required, not defaulted: an empty shared secret disables auth.
        assert "INTERNAL_API_TOKEN is required" in environment["INTERNAL_API_TOKEN"]


def test_the_bot_api_image_is_pinned(compose):
    image = compose["services"]["telegram-bot-api"]["image"]

    assert not image.endswith(":latest"), image
    assert ":" in image.rsplit("/", 1)[-1], f"{image!r} carries no tag"


@pytest.mark.parametrize("service", ["koyura-engine", "dashboard"])
def test_http_services_declare_a_healthcheck(compose, service):
    assert "healthcheck" in compose["services"][service]


def test_dependents_wait_for_a_healthy_engine(compose):
    for service in ("bot", "dashboard"):
        depends = compose["services"][service]["depends_on"]
        assert depends["koyura-engine"]["condition"] == "service_healthy", service


def test_the_queue_state_volume_is_not_the_downloads_volume(compose):
    """The downloads volume is purged; the throughput estimate must survive it."""
    mounts = compose["services"]["koyura-engine"]["volumes"]

    assert any(mount.endswith(":/downloads") for mount in mounts)
    assert any(mount.endswith(":/state") for mount in mounts)


def test_the_panel_and_the_bot_share_one_database_volume(compose):
    """The dashboard reads the bot's DB, so both must mount the same volume."""
    dashboard = compose["services"]["dashboard"]["volumes"]
    bot = compose["services"]["bot"]["volumes"]

    assert any(mount.endswith(":/data") for mount in dashboard)
    assert any(mount.endswith(":/data") for mount in bot)


# ---------------------------------------------------------------------------
# Images never run as root and never ship secrets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("service", BUILT_SERVICES)
def test_dockerfile_runs_as_an_unprivileged_user(service: str):
    dockerfile = (REPO_ROOT / SERVICE_DIRECTORIES[service] / "Dockerfile").read_text()

    assert "USER " in dockerfile, f"{service} never drops privileges"
    assert "USER root" not in dockerfile
    assert "USER 1000:1000" in dockerfile or "USER koyura" in dockerfile


@pytest.mark.parametrize("service", BUILT_SERVICES)
def test_dockerignore_keeps_secrets_and_caches_out_of_the_image(service: str):
    path = REPO_ROOT / SERVICE_DIRECTORIES[service] / ".dockerignore"

    assert path.is_file(), f"{service}/.dockerignore is missing"
    patterns = {line.rstrip("/") for line in path.read_text().splitlines()}
    for required in (".env", ".git", "__pycache__", "downloads"):
        assert required in patterns, f"{service}/.dockerignore must ignore {required!r}"


def test_gitignore_covers_every_runtime_artifact():
    patterns = (REPO_ROOT / ".gitignore").read_text().splitlines()

    for required in (".env", ".env.*", "*.db", "downloads/", ".venv/", "__pycache__/"):
        assert required in patterns, f".gitignore must ignore {required!r}"
    # The template is the one .env-shaped file that is meant to be committed.
    assert "!.env.example" in patterns


def test_no_env_file_is_tracked():
    assert not (REPO_ROOT / ".env").exists()


def test_the_env_template_is_complete_enough_to_start():
    """it must not ship a placeholder that the dashboard rejects at boot."""
    template = (REPO_ROOT / ".env.example").read_text()

    for variable in ("BOT_TOKEN", "INTERNAL_API_TOKEN", "DASHBOARD_SECRET_KEY",
                     "TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        assert f"{variable}=" in template, variable


def test_the_conversion_policy_is_documented_and_shipped_off():
    """A default that encodes on a shared vCPU is a production incident.

    The template, the compose file and the docs have to agree on one thing: a
    fresh deployment does **not** convert AV1/VP9 to H.264. The mode is spelled
    out in all three so an operator who wants it can find it.
    """
    template = (REPO_ROOT / ".env.example").read_text()
    assert "TRANSCODE_MODE=off" in template
    assert "TRANSCODE_MAX_MB=300" in template

    compose = (COMPOSE_PATH).read_text()
    assert "TRANSCODE_MODE: ${TRANSCODE_MODE:-off}" in compose
    assert "TRANSCODE_MAX_MB: ${TRANSCODE_MAX_MB:-300}" in compose

    docs = (REPO_ROOT / "docs" / "configuration.md").read_text()
    for variable in ("TRANSCODE_MODE", "TRANSCODE_MAX_MB"):
        assert f"`{variable}`" in docs, variable
    for mode in ("`off`", "`auto`", "`always`"):
        assert mode in docs, mode


def test_the_compose_validation_step_never_reads_the_real_env():
    """CI builds the config from placeholders; a tracked .env would hide typos."""
    job = load(CI_PATH)["jobs"]["compose-config"]
    step = next(step for step in _steps(job) if "compose" in (step.get("run") or ""))
    command = shlex.split(step["run"])

    assert command[:2] == ["docker", "compose"]
    assert "--quiet" in command
    assert step["env"]["DASHBOARD_SECRET_KEY"], "the placeholder secret must be set"


def test_the_cookie_volume_is_separate_and_shared(compose):
    """The panel writes the jars, the engine reads them, cleanup cannot purge them.

    Cookies sit on their own volume because ``downloads`` is deleted on a
    schedule: a sign-in session that disappears with a midnight purge would
    look like the site broke.
    """
    dashboard = compose["services"]["dashboard"]["volumes"]
    engine = compose["services"]["koyura-engine"]["volumes"]

    dashboard_mounts = [v for v in dashboard if "cookies" in v]
    engine_mounts = [v for v in engine if "cookies" in v]
    assert dashboard_mounts, "the dashboard must mount the cookie tree to write it"
    assert engine_mounts, "the engine must mount the cookie tree to read it"

    # The panel writes, the engine only reads.
    assert dashboard_mounts[0].endswith(":/var/lib/koyura2/cookies"), dashboard_mounts
    assert engine_mounts[0].endswith(":/var/lib/koyura2/cookies:ro"), engine_mounts

    # And it is never the purgeable downloads volume.
    assert not any("/downloads" in mount for mount in dashboard_mounts + engine_mounts)
    assert "cookies" in compose["volumes"], "the volume must be declared"


def test_both_services_are_told_where_the_cookies_live(compose):
    """One name, both containers: the engine and the panel agree on the tree."""
    for service in ("dashboard", "koyura-engine"):
        environment = compose["services"][service]["environment"]
        assert environment["COOKIES_PATH"] == "/var/lib/koyura2/cookies", service
