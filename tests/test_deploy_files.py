"""Acceptance tests 1-11: the Dockerfile, .dockerignore and the two compose files.

These read the deployment files as text on purpose. The thing being pinned is what an operator
gets when they run ``docker compose up`` on VM-201, and none of it is reachable from inside the
application - a container that runs as root or an image with a baked-in token is a bug nothing
in ``cadence/`` can catch.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO / "Dockerfile").read_text()
DOCKERIGNORE = (REPO / ".dockerignore").read_text()
COMPOSE_PATHS = (
    REPO / "docker-compose.yml",
    REPO / "docker-compose.prod.yml",
    REPO / "docker-compose.dev.yml",
)

SECRET_KEY = re.compile(r"^\s*[A-Z0-9_]*(TOKEN|KEY|PASSWORD)[A-Z0-9_]*\s*[:=]\s*(\S.*)$", re.MULTILINE)


def _lines(text: str) -> list[str]:
    """Instruction lines only: comments would otherwise satisfy every assertion below."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text())


@pytest.fixture(scope="module")
def compose_prod() -> dict:
    return yaml.safe_load((REPO / "docker-compose.prod.yml").read_text())


@pytest.fixture(scope="module")
def compose_dev() -> dict:
    return yaml.safe_load((REPO / "docker-compose.dev.yml").read_text())


def test_dockerfile_uses_python_312() -> None:
    """1. The base image agrees with .python-version and D-006."""
    froms = [line for line in _lines(DOCKERFILE) if line.upper().startswith("FROM ")]
    assert froms, "no FROM instruction"
    for line in froms:
        assert "python:3.12-slim" in line, f"unexpected base image: {line}"
    assert (REPO / ".python-version").read_text().strip().startswith("3.12")


def test_dockerfile_runs_as_non_root() -> None:
    """2. USER cadence comes after the last RUN, and nothing switches back to root."""
    lines = _lines(DOCKERFILE)
    user_indexes = [i for i, line in enumerate(lines) if line.upper().startswith("USER ")]
    assert user_indexes, "the image never drops out of root"
    last_user = lines[user_indexes[-1]]
    assert "root" not in last_user.lower(), f"the final USER is root: {last_user}"
    assert last_user.split()[1] == "cadence"
    run_indexes = [i for i, line in enumerate(lines) if line.upper().startswith("RUN ")]
    assert max(run_indexes) < user_indexes[-1], "a RUN executes as root after USER cadence"


def test_dockerfile_has_healthcheck_hitting_api_health() -> None:
    """3."""
    assert "HEALTHCHECK" in DOCKERFILE
    assert "/api/health" in DOCKERFILE


def test_healthcheck_checks_ok_field_not_just_status() -> None:
    """4. /api/health answers 200 with ok:false on a database failure (PRP-00)."""
    healthcheck = DOCKERFILE[DOCKERFILE.index("HEALTHCHECK") :]
    healthcheck = healthcheck[: healthcheck.index("CMD [")]
    assert "ok" in healthcheck, "the healthcheck never inspects the ok field"
    assert "get('ok')" in healthcheck or 'get("ok")' in healthcheck


def test_dockerignore_excludes_env_and_data() -> None:
    """5. The one that stops a VitalForge token being baked into an image layer."""
    entries = {line.strip().rstrip("/") for line in DOCKERIGNORE.splitlines() if line.strip()}
    assert ".env" in entries
    assert "data" in entries
    assert "!.env" not in DOCKERIGNORE, "a negation re-includes an env file"


def test_compose_maps_8090_to_8000(compose: dict) -> None:
    """6. Host 8090 to container 8000, through the configurable bind address."""
    assert compose["services"]["cadence"]["ports"] == ["${CADENCE_BIND_ADDR:-0.0.0.0}:8090:8000"]


def test_the_published_port_is_one_entry_with_a_variable_address() -> None:
    """The publish must stay a single entry, and the address must stay a variable.

    Compose *appends* `ports` across `-f` files rather than overriding them, so a restricted
    mapping added to an overlay publishes both — 0.0.0.0 still open, and two rules fighting over
    host 8090. Binding is therefore expressed here or nowhere (D-206).
    """
    for path in COMPOSE_PATHS:
        document = yaml.safe_load(path.read_text())
        service = document.get("services", {}).get("cadence") or {}
        ports = service.get("ports")
        if path.name == "docker-compose.yml":
            assert ports == ["${CADENCE_BIND_ADDR:-0.0.0.0}:8090:8000"], path.name
        else:
            assert ports is None, f"{path.name} declares ports; compose appends them rather than overriding"


def test_the_bind_address_defaults_to_todays_behaviour() -> None:
    """The default must not change what an existing deploy publishes.

    A default of 127.0.0.1 would have been the tempting one and is wrong here: NPM runs on
    VM-201 but is containerised and forwards to the host's LAN IP (docs/deploy.md section 4), so
    loopback-only would break the proxy. Closing the LAN gap is the operator setting
    CADENCE_BIND_ADDR to the Tailscale address, which this default leaves one variable away.
    """
    entry = yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]["cadence"]["ports"][0]
    assert ":-0.0.0.0}" in entry, "the default bind address changed; an existing deploy would move"


def test_compose_uses_env_file_not_inline_secrets(compose: dict) -> None:
    """7. Secrets arrive through env_file at runtime; none is written into a committed file."""
    assert compose["services"]["cadence"]["env_file"] == ".env"
    for path in COMPOSE_PATHS:
        for match in SECRET_KEY.finditer(path.read_text()):
            pytest.fail(f"{path.name} assigns a secret-shaped key: {match.group(0).strip()}")


def test_compose_restart_policy(compose: dict) -> None:
    """8."""
    assert compose["services"]["cadence"]["restart"] == "unless-stopped"


def test_compose_binds_data_volume(compose: dict) -> None:
    """9. ``./data`` reaches ``/app/data``, with or without an SELinux relabel option."""
    mounts = compose["services"]["cadence"]["volumes"]
    assert any(mount.split(":")[:2] == ["./data", "/app/data"] for mount in mounts), mounts


def test_compose_has_single_worker(compose: dict, compose_prod: dict) -> None:
    """10. A second worker double-drains the PRP-06 sync queue into Garmin."""
    for path in (*COMPOSE_PATHS, REPO / "Dockerfile"):
        text = path.read_text()
        for match in re.finditer(r"--workers[\"',\s]+(\d+)", text):
            assert int(match.group(1)) == 1, f"{path.name} runs {match.group(1)} workers"
    for document in (compose, compose_prod):
        command = document["services"]["cadence"].get("command")
        assert command is None or "--workers" not in str(command) or "--workers 1" in str(command)


def test_prod_overlay_rotates_logs(compose_prod: dict) -> None:
    """11. Unrotated json-file logs fill VM-201's disk and take VitalForge down with Cadence."""
    options = compose_prod["services"]["cadence"]["logging"]["options"]
    assert options["max-size"]
    assert options["max-file"]


def test_base_compose_is_production(compose: dict) -> None:
    """The base file states production's values plainly.

    ``CADENCE_VITALFORGE_MODE`` must stay **absent** from it: ``environment:`` overrides
    ``env_file:``, so declaring it here would silently ignore whatever the VM's .env says.
    """
    environment = compose["services"]["cadence"]["environment"]
    assert environment["CADENCE_ENV"] == "prod"
    assert "CADENCE_VITALFORGE_MODE" not in environment


def test_dev_overlay_is_dev_and_mock(compose_dev: dict) -> None:
    """`make dev-docker` must not run prod: settings validation refuses mock under prod."""
    environment = compose_dev["services"]["cadence"]["environment"]
    assert environment["CADENCE_ENV"] == "dev"
    assert environment["CADENCE_VITALFORGE_MODE"] == "mock"


def test_no_compose_file_puts_mock_under_prod() -> None:
    """Negative. The pairing that reports every session synced while sending nothing (D-139)."""
    for path in COMPOSE_PATHS:
        document = yaml.safe_load(path.read_text())
        environment = (document.get("services", {}).get("cadence") or {}).get("environment") or {}
        if environment.get("CADENCE_ENV") == "prod":
            assert environment.get("CADENCE_VITALFORGE_MODE") != "mock", path.name


def test_dev_docker_uses_the_dev_overlay() -> None:
    """The overlay only helps if the target actually passes it."""
    makefile = (REPO / "Makefile").read_text()
    dev_docker = makefile[makefile.index("dev-docker:") :]
    dev_docker = dev_docker[: dev_docker.index("\nsmoke:")]
    assert "-f docker-compose.yml -f docker-compose.dev.yml" in dev_docker


def test_dockerignore_excludes_the_rest_of_the_build_context() -> None:
    """5, widened. ``.env`` and ``data`` are the security half; these are the correctness half.

    ``tests/`` and ``docs/`` in an image are dead weight that also change the layer on every
    docs commit, and ``.git`` carries the whole history - including, on a machine where someone
    once committed one by accident, a secret that ``.dockerignore``'s ``.env`` rule would not
    catch. Verified against the built image in CI by the ``image`` job; asserted here so the
    entry cannot be dropped without a red test first.
    """
    entries = {line.strip().rstrip("/") for line in DOCKERIGNORE.splitlines() if line.strip()}
    for required in (".git", "tests", "docs", ".venv", "__pycache__"):
        assert required in entries, f".dockerignore does not exclude {required!r}"


def test_ci_image_job_never_pushes_and_names_no_registry() -> None:
    """PRP-09 risk 2's other half: an image that reaches a registry is a place a secret can sit.

    The PRP keeps the image out of any registry - VM-201 builds from the rsynced tree. A test
    on the workflow rather than the Dockerfile because ``push: true`` plus a login step is one
    small diff away and would not fail anything else in this suite.
    """
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    image = workflow["jobs"]["image"]
    text = yaml.safe_dump(image)

    assert "--push" not in text
    for step in image["steps"]:
        if "build-push-action" in str(step.get("uses", "")):
            assert step["with"]["push"] is False, "the image job pushes to a registry"
    assert "login-action" not in text, "the image job authenticates to a registry"
    assert "docker.io" not in text and "ghcr.io" not in text, "the image job names a registry"
    assert "secrets." not in text, "the image job references a repository secret"


def test_ci_image_job_proves_the_container_is_non_root() -> None:
    """Acceptance test 2 is a grep over the Dockerfile; the CI job runs the image and asks it.

    Worth pinning because it is the only place in the build that exercises the real container:
    a ``USER cadence`` line that named a user the final stage never created would satisfy the
    grep and fail here.
    """
    steps = yaml.safe_dump(yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["image"])
    assert "10001" in steps, "CI never checks the running uid"
    assert "docker run" in steps


def test_scripts_stay_world_executable_for_the_non_root_runtime_user() -> None:
    """D-202 copies /app and scripts/ as root, so the runtime uid is never their owner.

    That is fine only while the other-execute bit is set: uid 10001 reaches
    ``scripts/backup.sh`` through the *other* class, not owner or group. ``docs/deploy.md`` §6
    makes the in-container backup the documented default and the installed cron line, so a
    ``chmod 750`` here — or a file committed 0644 — breaks the nightly backup on VM-201 and
    nothing else in the suite notices.

    Asserted against the git index rather than the working tree: the index mode is what a fresh
    clone and therefore the Docker build context gets, and a local ``chmod`` would otherwise
    mask a file that is 100644 in the repository.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-s", "scripts/"], cwd=str(REPO), capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert listing, "no scripts are tracked"
    for line in listing:
        mode, _, _, name = line.replace("\t", " ").split()
        assert mode == "100755", f"{name} is {mode} in git; uid 10001 cannot execute it in the image"
