"""Acceptance tests 24-28: the runbook, and the repo-wide secret sweep.

A runbook is only load-bearing where it records something a reader would otherwise get wrong.
These five pin the entries that were reasoned about rather than transcribed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = (REPO / "docs" / "deploy.md").read_text()

SECRET_NAMES = ("VITALFORGE_TOKEN", "OMNIROUTE_KEY")

# `NAME=value` or `NAME: value`, on one line. `[^\S\n]*` rather than `\s*` so a blank
# assignment does not swallow the newline and match whatever the next line happens to be.
ASSIGNED = re.compile(rf"\b({'|'.join(SECRET_NAMES)})[^\S\n]*[:=][^\S\n]*(\S+)")

# What a real one looks like: VitalForge mints `secrets.token_urlsafe(32)`, which is always 43
# URL-safe characters (contract section 1.3). The threshold is keyed to that generator rather
# than picked by feel - 32 sits well below a real token and well above every placeholder the
# suite writes ("sekret", "vf-test-token-abc123", "e2e-not-a-real-token", the longest of which
# is 21). A test that fires on obvious fakes is a test that gets switched off within a week.
REAL_TOKEN_CHARS = 43
CREDENTIAL_SHAPED = re.compile(r"^[A-Za-z0-9_\-]{32,}$")


def test_deploy_doc_documents_npm_body_size() -> None:
    """24. Without it, a large paste into /api/import is a 413 that reads like an app bug."""
    assert "client_max_body_size" in DOC


def test_deploy_doc_documents_tailscale_cidr() -> None:
    """25."""
    assert "100.64.0.0/10" in DOC


def test_deploy_doc_documents_restore_removes_wal() -> None:
    """26. The step everyone forgets: a stale WAL is replayed over a restored database."""
    restore = DOC[DOC.index("### Restore") :]
    assert "-wal" in restore
    assert "-shm" in restore


def test_deploy_doc_documents_data_ownership() -> None:
    """27."""
    assert "10001" in DOC


def test_deploy_doc_covers_every_required_section() -> None:
    """Not numbered in the PRP, but section 5.6 lists nine sections the runbook must have."""
    lowered = DOC.lower()
    for heading in (
        "prerequisites",
        "first deploy",
        "routine deploy",
        "nginx proxy manager",
        "tailscale",
        "backups and restore",
        "smoke test",
        "troubleshooting",
        "rollback",
    ):
        assert heading in lowered, f"docs/deploy.md has no {heading!r} section"


def test_no_real_secrets_in_repo() -> None:
    """28. No credential-shaped value is assigned to either secret anywhere in the tree."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(REPO), capture_output=True, text=True, check=True
    ).stdout.split()
    assert ".env" not in tracked, ".env is committed"

    offenders: list[str] = []
    for name in tracked:
        # This module carries both literal names as its own test data.
        if name == "tests/test_docs_deploy.py":
            continue
        path = REPO / name
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:  # pragma: no cover - the repo is all text today
            continue
        for match in ASSIGNED.finditer(text):
            value = match.group(2).strip("\"',;`")
            if CREDENTIAL_SHAPED.match(value):
                offenders.append(f"{name}: {match.group(1)} = {value[:8]}...")
    assert not offenders, "a credential-shaped secret is assigned in a tracked file: " + "; ".join(offenders)


def test_the_credential_threshold_still_catches_a_real_token() -> None:
    """The sweep is only worth having if it fires on the thing it is looking for."""
    import secrets

    real = secrets.token_urlsafe(32)
    assert len(real) == REAL_TOKEN_CHARS
    assert CREDENTIAL_SHAPED.match(real), "a real VitalForge token would slip through the sweep"
    for placeholder in ("sekret", "token", "vf-test-token-abc123", "e2e-not-a-real-token", "x"):
        assert not CREDENTIAL_SHAPED.match(placeholder), placeholder


def test_env_example_assigns_no_secret_value() -> None:
    """The other half of 28, stated the way the PRP states it: in .env.example both are blank."""
    for line in (REPO / ".env.example").read_text().splitlines():
        for name in SECRET_NAMES:
            if line.startswith(f"{name}="):
                assert line == f"{name}=", f"{name} carries a value in .env.example: {line!r}"


def test_env_example_ships_both_secrets_blank() -> None:
    """The other half of 28: the template exists and carries no value."""
    example = (REPO / ".env.example").read_text()
    for name in SECRET_NAMES:
        assert f"{name}=\n" in example or example.rstrip().endswith(f"{name}="), f"{name} is not blank"


# ------------------------------------------------------- the runbook against the actual repo
#
# Every check above asks whether the runbook *mentions* something. These ask whether what it
# tells JD to run exists. A runbook is followed once, on a VM, at the moment it matters, and a
# command that was renamed three PRPs ago fails there rather than here.

MAKEFILE = (REPO / "Makefile").read_text()


def test_every_make_target_the_runbook_names_exists() -> None:
    """`make deploy`, `make smoke`, `make dev-docker` - and anything added later."""
    targets = {match.group(1) for match in re.finditer(r"^([a-z][a-z0-9-]*):", MAKEFILE, re.MULTILINE)}
    # Code only. Prose says things like "make the deploying user own data/", and a scan over the
    # whole document would read "the" as a target and fail on English rather than on the runbook.
    code = "\n".join(re.findall(r"`([^`\n]+)`", DOC) + re.findall(r"```bash\n(.*?)```", DOC, re.DOTALL))
    named = {match.group(1) for match in re.finditer(r"\bmake\s+([a-z][a-z0-9-]*)\b", code)}
    assert named, "the runbook names no make target at all"
    missing = named - targets
    assert not missing, f"docs/deploy.md tells JD to run make targets that do not exist: {sorted(missing)}"


def test_every_script_the_runbook_names_exists_and_is_executable() -> None:
    """A script the runbook calls by path, with no bit set, is a permission-denied at 03:17."""
    named = {match.group(0).lstrip("./") for match in re.finditer(r"\.?/?scripts/[a-z_]+\.sh", DOC)}
    assert named, "the runbook names no script"
    for relative in named:
        path = REPO / relative
        assert path.is_file(), f"docs/deploy.md names {relative}, which does not exist"
        assert path.stat().st_mode & 0o111, f"{relative} is not executable"


def test_the_proxy_forward_port_matches_the_compose_host_port() -> None:
    """The 502 in the troubleshooting table, prevented rather than diagnosed.

    NPM's Forward Port is host-side. If the compose publish ever moves off 8090 the runbook's
    proxy table silently points at nothing, and the symptom is a 502 that reads like the
    container being down.
    """
    import yaml

    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    # `ADDR:HOST:CONTAINER` - the host port is second from the right, and the address on the left
    # is a `${...}` variable that itself contains a colon, so index 0 is not it.
    host_port = compose["services"]["cadence"]["ports"][0].split(":")[-2]

    forward_row = next(line for line in DOC.splitlines() if "Forward Port" in line)
    assert f"`{host_port}`" in forward_row, f"the runbook forwards to {forward_row!r}, compose publishes {host_port}"
    assert f"192.168.1.21:{host_port}" in DOC


def test_the_cron_line_backs_up_through_a_path_that_can_write() -> None:
    """§6 explains that a host cron cannot write into data/backups/, owned by uid 10001.

    The installed cron line must therefore be the in-container form. A `./scripts/backup.sh`
    cron line would fail every night and the only evidence would be a log nobody reads.
    """
    cron = next(line for line in DOC.splitlines() if line.startswith("17 3 * * *"))
    assert "docker compose exec" in cron, f"the cron line runs on the host: {cron}"
    assert " -T " in cron, "docker compose exec without -T fails under cron, which has no TTY"


def test_every_dotenv_variant_is_ignored_except_the_example() -> None:
    """`.gitignore` listed `.env` alone; the convention has many more names (D-205).

    A variant that is not ignored is tracked silently, and the failure only becomes visible once
    a real token is already in the history — where removing it means rewriting published commits
    and rotating the credential anyway.
    """
    import subprocess

    variants = (".env", ".env.local", ".env.production", ".env.prod", ".env.vm-201", ".env.test")
    for name in variants:
        result = subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO, check=False, capture_output=True)
        assert result.returncode == 0, f"{name} is not git-ignored"

    example = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=REPO, check=False, capture_output=True)
    assert example.returncode != 0, ".env.example must stay tracked; the ignore negation is missing"


def test_no_dotenv_file_other_than_the_example_is_tracked() -> None:
    """The state the ignore rules exist to produce, asserted directly against the index."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", ".env*"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout
    names = [name for name in tracked.split("\0") if name]
    assert names == [".env.example"], f"tracked dotenv files: {names}"


def test_dockerignore_excludes_every_dotenv_form() -> None:
    """The image half. Already true before D-205 — this pins it so it stays true."""
    lines = {line.strip() for line in (REPO / ".dockerignore").read_text().splitlines()}
    assert ".env" in lines
    assert ".env.*" in lines


# ------------------------------------------------------------------- D-280: the bind-address trap

#: Both runbooks confirm a deploy by asking the app for its health. The address that check uses
#: has to follow `CADENCE_BIND_ADDR`, because §5 narrows it away from the LAN.
HANDOFF = (REPO / "docs" / "HANDOFF.md").read_text()
_HEALTH_CURL = re.compile(r"^.*curl[^\n]*/api/health.*$", re.MULTILINE)


def test_no_health_check_hardcodes_the_lan_address() -> None:
    """D-280. `192.168.1.21:8090` refuses once the bind is narrowed, and both docs narrow it.

    The cost of getting this wrong is not a typo: a refused connection arrives at the exact
    moment an operator is trying to prove the container came up, and reads like one that did not.
    `docker compose port` asks the publish where it is and is correct under either bind.
    """
    for name, text in (("deploy.md", DOC), ("HANDOFF.md", HANDOFF)):
        checks = _HEALTH_CURL.findall(text)
        assert checks, f"{name} lost its health check, so this would pass vacuously"
        for line in checks:
            assert "192.168.1.21" not in line, f"{name} health-checks an address the bind refuses: {line.strip()}"
        # A line naming the Tailscale address outright is fine - that one is reachable. What has
        # to exist is the bind-agnostic form, for the confirmation step a first deploy runs
        # before anybody knows which address this host ended up on.
        assert any("docker compose port" in line for line in checks), (
            f"{name} has no health check that asks the publish where it is: {[line.strip() for line in checks]}"
        )


def test_the_proxy_forward_host_is_read_not_copied() -> None:
    """D-280. NPM must forward to whatever the publish is bound to, or it 502s a healthy app.

    Asserted as "the row does not name a bare address" rather than "the row names the Tailscale
    one": the correct value is machine-specific and lives in `.env`, so a document that pins any
    single address is wrong the moment somebody deploys a second one.
    """
    row = next(line for line in DOC.splitlines() if "Forward Hostname" in line)
    assert "docker compose port" in row, f"the runbook hands NPM an address to copy: {row}"


def _fenced_command_lines(text: str) -> list[str]:
    """Every line inside a ``` fence that is a command rather than a comment or a fence marker."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        stripped = line.strip()
        if inside and stripped and not stripped.startswith("#"):
            out.append(line)
    return out


def test_no_runnable_command_names_the_default_path_on_this_host(tmp_path=None) -> None:
    """D-281. Both runbooks document VM-201, whose install is not at the script's default.

    Prose may name `/opt/cadence` — it *is* the default, and saying so is the point. A command
    somebody pastes may not, because on this host it targets a directory beside the running app.
    D-280 established that a "substitute this path throughout" note does not count as recording
    the difference; this is that rule, enforced.
    """
    for name, text in (("deploy.md", DOC), ("HANDOFF.md", HANDOFF)):
        offenders = [line for line in _fenced_command_lines(text) if "/opt/cadence" in line]
        assert not offenders, f"{name} still names the default path in a runnable command: " + "; ".join(
            line.strip() for line in offenders
        )


def test_the_backup_cron_targets_the_install() -> None:
    """The cron is installed by copy-paste and then never read again until a restore needs it."""
    cron = next(line for line in DOC.splitlines() if line.startswith("17 3 * * *"))
    assert "/opt/cadence" not in cron, f"the nightly backup cds to a directory beside the app: {cron}"
