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


# ------------------------------------------------- D-281: the wrong-directory trap

#: A stub `ssh` that answers the two probes and logs every other remote command, so a test can
#: assert what the deploy *would* have run without a host to run it on. `$1` is `--`, `$2` the
#: host, `$3` the remote command - the shape every `ssh --` call in the script uses.
_FAKE_SSH = """#!/usr/bin/env bash
# The reachability probe: `ssh -o BatchMode=yes -o ConnectTimeout=10 -- HOST true`.
for arg in "$@"; do [ "$arg" = "true" ] && exit 0; done
cmd="${@: -1}"
case "$cmd" in
  *"if [ ! -d "*) echo "__STATE__" ;;
  *) echo "$cmd" >> "$SSH_LOG" ;;
esac
exit 0
"""

_FAKE_RSYNC = """#!/usr/bin/env bash
echo "rsync $*" >> "$SSH_LOG"
exit 0
"""


def _run_against(tmp_path, state: str, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the deploy with a stubbed `ssh` reporting `state` for the remote directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(_FAKE_SSH.replace("__STATE__", state))
    (bin_dir / "rsync").write_text(_FAKE_RSYNC)
    for name in ("ssh", "rsync"):
        (bin_dir / name).chmod(0o755)

    log = tmp_path / "remote.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
        "CADENCE_DEPLOY_HOST": "vm-201",
        "CADENCE_DEPLOY_PATH": "/opt/cadence",
        "SSH_LOG": str(log),
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False, timeout=60
    )
    return result, log.read_text() if log.exists() else ""


def test_deploy_refuses_a_remote_path_that_does_not_exist(tmp_path) -> None:
    """D-281. `mkdir -p` used to create the wrong directory and deploy into it.

    The trap is not that it failed — it is *how*. The rsync succeeded, compose stopped at a
    missing `.env`, and `docs/deploy.md` §2 calls that stop expected on a first deploy. So the
    operator read a normal message while the running app at `~/docker/cadence` was never touched.
    """
    result, remote = _run_against(tmp_path, "missing")

    assert result.returncode == 4, result.stderr
    assert "does not exist" in result.stderr
    assert "CADENCE_DEPLOY_PATH" in result.stderr, "the refusal does not say how to fix it"
    assert "rsync" not in remote, f"it deployed anyway: {remote}"
    assert "up -d" not in remote, f"it brought a stack up in the wrong directory: {remote}"


def test_deploy_never_creates_the_remote_directory(tmp_path) -> None:
    """The `mkdir -p` that made the trap possible must not come back.

    `mkdir -p data/backups` *inside* an existing install is a different thing and stays.
    """
    assert "mkdir -p '$REMOTE_PATH'" not in TEXT, "deploy.sh creates the remote directory again"
    result, remote = _run_against(tmp_path, "missing")
    assert result.returncode == 4
    assert "mkdir -p /opt/cadence" not in remote, f"it created the directory: {remote}"


def test_deploy_proceeds_into_a_prepared_directory(tmp_path) -> None:
    """An existing but empty directory is the documented first deploy, and must still work.

    §2 has the operator create it by hand before the first `make deploy`, so "exists and is
    bare" is a legitimate state — the guard is about the directory being *absent*, not about it
    being populated.
    """
    result, remote = _run_against(tmp_path, "bare")

    assert result.returncode == 0, result.stderr
    assert "rsync" in remote, "a prepared first deploy was refused"
    assert "up -d" in remote


def test_deploy_proceeds_into_an_existing_install(tmp_path) -> None:
    """The routine case: a directory carrying `.env` or `docker-compose.yml`."""
    result, remote = _run_against(tmp_path, "install")
    assert result.returncode == 0, result.stderr
    assert "rsync" in remote


def test_git_mode_refuses_a_directory_that_is_not_a_clone(tmp_path) -> None:
    """`--mode git` pulls, so a bare directory fails — caught before the chown and `up` queue."""
    result, remote = _run_against(tmp_path, "bare", "--mode", "git")

    assert result.returncode == 4, result.stderr
    assert "not a git clone" in result.stderr
    assert "git pull" not in remote, f"it pulled anyway: {remote}"
    assert "up -d" not in remote


def test_git_mode_accepts_a_clone(tmp_path) -> None:
    result, remote = _run_against(tmp_path, "clone", "--mode", "git")
    assert result.returncode == 0, result.stderr
    assert "git pull --ff-only" in remote


def test_deploy_refuses_an_unreadable_remote_state(tmp_path) -> None:
    """Fails closed. An empty or unexpected probe answer is not permission to deploy anyway."""
    result, remote = _run_against(tmp_path, "")

    assert result.returncode == 4, result.stderr
    assert "could not tell" in result.stderr
    assert "rsync" not in remote, f"it deployed on an unreadable answer: {remote}"


def test_the_refusal_does_not_hardcode_one_host_path(tmp_path) -> None:
    """The script is the generic artifact; the host-specific value belongs in the runbook.

    D-281 declined to bake a username into the repo's *default* for this reason, and an example
    in the error message is the same decision one layer down.
    """
    result, _ = _run_against(tmp_path, "missing")
    assert "/home/user/" not in result.stderr, f"the refusal names one operator's home: {result.stderr}"
    assert "docs/deploy.md" in result.stderr, "the refusal does not say where the real value lives"
