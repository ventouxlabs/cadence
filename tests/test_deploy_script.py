"""Acceptance tests 21-23: ``scripts/deploy.sh``.

Test 21 guards the single most destructive omission possible in this PRP: an ``rsync --delete``
that does not exclude ``.env`` and ``data`` deletes the production database and both secrets in
one command, from the workstation, with no confirmation.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "deploy.sh"
TEXT = SCRIPT.read_text()


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run with an unreachable ssh host, so nothing can leave this machine.

    ``CADENCE_DEPLOY_HOST`` points at an alias that cannot resolve; every path in the script
    reaches the BatchMode reachability probe before it rsyncs or pulls anything.
    """
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "CADENCE_DEPLOY_HOST": "cadence-deploy-test.invalid",
        "CADENCE_DEPLOY_PATH": "/tmp/cadence-deploy-test",
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False, timeout=60
    )


def test_rsync_excludes_env_and_data() -> None:
    """21."""
    assert "--delete" in TEXT
    assert "--exclude '.env'" in TEXT
    assert "--exclude 'data'" in TEXT
    rsync_block = TEXT[TEXT.index("rsync -az") : TEXT.index('"$REPO_ROOT/"')]
    assert "--exclude '.env'" in rsync_block, "the .env exclude is not on the rsync that deletes"
    assert "--exclude 'data'" in rsync_block, "the data exclude is not on the rsync that deletes"


def test_deploy_checks_ssh_before_acting() -> None:
    """22. A reachability probe precedes any rsync, so a missing key cannot half-deploy."""
    assert "BatchMode=yes" in TEXT
    assert TEXT.index("BatchMode=yes") < TEXT.index("rsync -az")
    assert TEXT.index("BatchMode=yes") < TEXT.index("git pull --ff-only")

    result = _run("--mode", "rsync")
    assert result.returncode == 3, result.stderr
    assert "cannot reach" in result.stderr


def test_deploy_supports_both_modes() -> None:
    """23. Both modes are handled; an unknown one exits non-zero without touching the VM."""
    assert '"rsync"' in TEXT and '"git"' in TEXT

    for mode in ("rsync", "git"):
        result = _run("--mode", mode)
        # Stopped by the unreachable host, which means the mode itself was accepted.
        assert result.returncode == 3, f"mode {mode} was rejected: {result.stderr}"

    bad = _run("--mode", "scp")
    assert bad.returncode != 0
    assert "unknown mode" in bad.stderr
    assert "cannot reach" not in bad.stderr, "an unknown mode still tried to contact the host"

    unknown_arg = _run("--yolo")
    assert unknown_arg.returncode != 0
    assert "unknown argument" in unknown_arg.stderr


def test_deploy_uses_the_prod_overlay() -> None:
    """Not in the PRP's list. The overlay carries log rotation (acceptance test 11); a deploy
    that forgets ``-f docker-compose.prod.yml`` fills VM-201's disk and takes VitalForge with
    it, and would look exactly like a working deploy until the day it did."""
    assert "-f docker-compose.yml -f docker-compose.prod.yml" in TEXT


def test_deploy_chowns_the_bind_mount_to_the_container_uid() -> None:
    """Not in the PRP's list, but it is the documented first-deploy failure: a root-owned bind
    mount under a non-root container is ``unable to open database file``."""
    assert "10001" in TEXT
    assert "chown" in TEXT


def test_deploy_never_writes_or_reads_the_env_file() -> None:
    """Test 21 covers the rsync exclude. This covers everything else the script could do to it.

    The exclude stops ``--delete`` from removing the VM's ``.env``; it says nothing about a
    later convenience that copies, truncates or sources one. The workstation's ``.env`` holds
    the same real tokens, and any of these would move them onto the VM or off it.
    """
    forbidden = (
        r"cp\s+[^\n]*\.env",
        r"scp\s+[^\n]*\.env",
        r">\s*[^\n]*\.env\b",
        r"rm\s+[^\n]*\.env",
        r"(source|\.)\s+[^\n]*\.env\b",
        r"cat\s+[^\n]*\.env\b",
    )
    body = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))
    for pattern in forbidden:
        assert not re.search(pattern, body), f"deploy.sh touches .env: {pattern}"


def _run_with(host: str, path: str) -> subprocess.CompletedProcess[str]:
    """Run with hostile values, and a sentinel that proves nothing executed.

    ``CADENCE_DEPLOY_HOST`` and ``CADENCE_DEPLOY_PATH`` both reach a shell: the path is
    interpolated into ``ssh HOST "cd '$PATH' && ..."`` and the host is ssh's own target, where a
    leading ``-`` is an option rather than a name. Each payload writes ``sentinel`` if it runs.
    """
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "CADENCE_DEPLOY_HOST": host, "CADENCE_DEPLOY_PATH": path}
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False, timeout=60)


HOSTILE_PATHS = (
    "/opt/cadence'; touch {sentinel}; '",
    "/opt/cadence$(touch {sentinel})",
    "/opt/cadence`touch {sentinel}`",
    "/opt/../etc",
    "relative/path",
)
# Deliberately not in the list: "". `${CADENCE_DEPLOY_PATH:-/opt/cadence}` treats an empty value
# as unset, so it is the default rather than a hostile one, and asserting a refusal would be
# asserting the wrong behaviour.

HOSTILE_HOSTS = (
    "-oProxyCommand=touch {sentinel}",
    "cadence-deploy-test.invalid -oProxyCommand=touch {sentinel}",
    "a;touch {sentinel}",
    "$(touch {sentinel})",
)


def test_a_hostile_remote_path_is_refused_and_never_executed(tmp_path: Path) -> None:
    """The path is interpolated into a remote shell command; single quotes alone do not hold.

    A quote inside the value closes the one in the script, and what follows runs on VM-201 as
    the deploying user - who has passwordless sudo by design (docs/deploy.md section 1).
    """
    for template in HOSTILE_PATHS:
        sentinel = tmp_path / f"pwn-{abs(hash(template))}"
        result = _run_with("cadence-deploy-test.invalid", template.format(sentinel=sentinel))
        assert result.returncode == 2, f"{template!r} was not refused: {result.stdout}{result.stderr}"
        assert "refusing remote path" in result.stderr, result.stderr
        assert not sentinel.exists(), f"{template!r} executed"


def test_a_hostile_host_is_refused_and_never_executed(tmp_path: Path) -> None:
    """``-oProxyCommand=...`` runs on *this* machine, before any connection is opened."""
    for template in HOSTILE_HOSTS:
        sentinel = tmp_path / f"pwn-{abs(hash(template))}"
        result = _run_with(template.format(sentinel=sentinel), "/tmp/cadence-deploy-test")
        assert result.returncode == 2, f"{template!r} was not refused: {result.stdout}{result.stderr}"
        assert "refusing host" in result.stderr, result.stderr
        assert not sentinel.exists(), f"{template!r} executed"


def test_the_validation_runs_before_the_ssh_probe() -> None:
    """Order matters: the probe is itself the first thing that hands the host to ssh."""
    body = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))
    assert body.index("refusing host") < body.index("checking ssh to")
    assert body.index("refusing remote path") < body.index("checking ssh to")


def test_legitimate_hosts_and_paths_still_pass_validation() -> None:
    """A guard that refused the real values would be found the hard way, on a deploy."""
    for host in ("vm-201", "user@192.168.1.21", "vm-201.tail1234.ts.net", "cadence_host"):
        result = _run_with(host, "/opt/cadence")
        assert "refusing host" not in result.stderr, f"{host!r} was refused: {result.stderr}"
    for path in ("/opt/cadence", "/srv/apps/cadence-1", "/opt/cadence_2"):
        result = _run_with("cadence-deploy-test.invalid", path)
        assert "refusing remote path" not in result.stderr, f"{path!r} was refused: {result.stderr}"


def test_ssh_and_rsync_targets_are_after_a_double_dash() -> None:
    """Belt to validation's braces: `--` stops a target being read as an option."""
    body = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))
    for call in re.findall(r"^\s*ssh [^\n]*", body, re.MULTILINE):
        assert " -- " in call, f"ssh call without --: {call.strip()}"
    assert '-- "$REPO_ROOT/" "$HOST:$REMOTE_PATH/"' in body, "the rsync target is not after a --"


def test_deploy_locks_down_the_backup_directory_it_creates() -> None:
    """deploy.sh creates data/backups before backup.sh's own umask ever runs (D-201)."""
    body = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))
    assert "mkdir -p data/backups && chmod 700 data/backups" in body


def test_neither_variable_accepts_a_leading_dash_or_a_quote(tmp_path: Path) -> None:
    """The two shapes stated as their own property, independent of the patterns that enforce it.

    D-204's whitelists already subsume this — a character class of ``[A-Za-z0-9._/-]`` cannot
    contain a quote, and the anchored first character cannot be ``-``. That is exactly why this
    is worth asserting separately: it survives the patterns being rewritten or widened later,
    which is when the property would otherwise be lost without anything noticing.
    """
    for value in ("-rf", "-oProxyCommand=id", "has'quote", "/opt/has'quote"):
        host = _run_with(value, "/opt/cadence")
        assert host.returncode == 2, f"host {value!r} accepted: {host.stdout}{host.stderr}"
        assert "refusing host" in host.stderr, host.stderr

        path = _run_with("cadence-deploy-test.invalid", value)
        assert path.returncode == 2, f"path {value!r} accepted: {path.stdout}{path.stderr}"
        assert "refusing remote path" in path.stderr, path.stderr
