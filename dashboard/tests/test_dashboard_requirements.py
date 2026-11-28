"""The panel's runtime dependencies, and the one that broke a deploy.

``message_styles`` parses the preset style files with PyYAML, so the *panel*
needs that package at runtime — not just the test suite. It was a dev-only
dependency once and the production deploy answered ``POST
/admin/templates/apply-style`` with 400 "PyYAML is required to read a style
file", because the image is built from ``requirements.txt`` alone.

The tests here fail on the three ways that comes back:

* ``pyyaml`` drops out of ``dashboard/requirements.txt`` (or is only mentioned
  in a comment, which pip never reads);
* ``requirements-dev.txt`` stops pulling the runtime file in, so the CI job
  installs a set the image does not use and a regression passes it by;
* the import itself is missing or unguarded, so the panel dies with a traceback
  instead of the sentence an operator can act on.

No app is created here, so no database or secret is needed.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

import message_styles

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
REQUIREMENTS = DASHBOARD_DIR / "requirements.txt"
DEV_REQUIREMENTS = DASHBOARD_DIR / "requirements-dev.txt"
DOCKERFILE = DASHBOARD_DIR / "Dockerfile"

#: Dependencies the panel imports but deliberately does not require: yt-dlp
#: only feeds the *suggestions* list and ``service_catalog`` treats its absence
#: as normal, so it stays out of ``requirements.txt`` on purpose.
OPTIONAL_IMPORTS: frozenset[str] = frozenset({"yt_dlp"})

#: Import name -> distribution name, where the two differ. PyYAML is the whole
#: reason this module exists; the rest are here so the static check below stays
#: quiet about packages declared under the usual import name.
DISTRIBUTIONS: dict[str, str] = {
    "yaml": "pyyaml",
    "multipart": "python-multipart",
}


def _requirement_names(path: Path) -> set[str]:
    """The distributions a requirements file installs, includes resolved.

    Comments, blank lines and ``-r`` includes are handled: pip reads the
    include, so the names it names are part of this set. ``pyyaml>=6.0`` and
    ``PyYAML==6.0.4`` both normalise to ``pyyaml``.
    """
    names: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r", "--requirement")):
            included = line.split(maxsplit=1)[-1].split("=", 1)[-1].strip()
            target = (path.parent / included).resolve()
            if target.is_file():
                names |= _requirement_names(target)
            continue
        if line.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\[]", line, maxsplit=1)[0].strip().lower()
        name = name.replace("_", "-").replace(".", "-")
        if name:
            names.add(name)
    return names


def test_the_panel_declares_a_yaml_parser():
    """Without this line the image cannot read a style file at all."""
    assert "pyyaml" in _requirement_names(REQUIREMENTS), (
        "dashboard/requirements.txt must declare pyyaml: message_styles imports "
        "yaml at runtime, and the Docker image installs only that file"
    )


def test_the_dev_set_pulls_in_the_runtime_set():
    """CI installs requirements-dev.txt; it has to carry the runtime packages."""
    lines = [
        line.split("#", 1)[0].strip()
        for line in DEV_REQUIREMENTS.read_text().splitlines()
    ]
    assert "-r requirements.txt" in lines, (
        "dashboard/requirements-dev.txt must start with '-r requirements.txt' so "
        "the CI job installs what the image installs"
    )
    assert _requirement_names(DEV_REQUIREMENTS) >= _requirement_names(REQUIREMENTS)


def test_the_image_installs_the_runtime_set():
    """The Dockerfile is the deploy that failed; it must use requirements.txt."""
    dockerfile = DOCKERFILE.read_text()
    assert "pip install --no-cache-dir -r requirements.txt" in dockerfile


def test_every_runtime_import_is_declared_or_optional():
    """A new top-level import in the panel is a new line in requirements.txt.

    Cheap static check: the modules the panel ships at import time are compared
    with the declared set, so ``import yaml`` folded into some future module
    without the matching requirement fails here rather than on the server.
    """
    source = "\n".join(
        path.read_text()
        for path in [*DASHBOARD_DIR.glob("*.py"), *(DASHBOARD_DIR / "routes").glob("*.py")]
    )
    imported = set()
    for match in re.finditer(r"^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)", source, re.M):
        imported.add(match.group(1).split(".")[0])

    local = {path.stem for path in DASHBOARD_DIR.glob("*.py")} | {"routes"}
    stdlib = set(sys.stdlib_module_names)
    third_party = {
        name
        for name in imported
        if name not in local and name not in stdlib and name not in OPTIONAL_IMPORTS
    }
    # Pydantic and Starlette come in with FastAPI; a requirements file names
    # what it installs, not every transitive import.
    third_party -= {"pydantic", "starlette"}

    declared = _requirement_names(REQUIREMENTS)
    needed = set()
    for name in third_party:
        distribution: str = DISTRIBUTIONS.get(name, name) or name
        needed.add(distribution.replace("_", "-"))
    missing = needed - declared
    assert not missing, f"imported but not declared in requirements.txt: {sorted(missing)}"


def test_the_parser_is_importable_here():
    """The suite and the image agree: PyYAML is installed, not assumed."""
    yaml = importlib.import_module("yaml")
    # ``safe_load`` is the only call the panel makes; a stub that lacks it would
    # satisfy the import and still break the style read.
    assert hasattr(yaml, "safe_load")
    assert yaml.safe_load("a: 1") == {"a": 1}


def test_a_style_plan_works_in_this_environment():
    """The end-to-end proof: a shipped style parses and validates."""
    plan = message_styles.plan("04-friendly")
    assert plan.applied > 0, "no body of 04-friendly survived validation"
    assert plan.style.id == "04-friendly"


def test_a_missing_parser_is_reported_as_a_sentence(monkeypatch):
    """No traceback: the operator gets the line the deploy ticket quoted.

    ``sys.modules['yaml'] = None`` makes ``import yaml`` raise ImportError,
    which is exactly what the image did before the requirement was declared.
    """
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(message_styles.StyleError) as error:
        message_styles.read("04-friendly")
    assert "PyYAML is required" in str(error.value)
    assert "Traceback" not in str(error.value)
