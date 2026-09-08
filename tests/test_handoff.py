"""`docs/HANDOFF.md` and `README.md`: the documents that must not rot.

Every assertion about these two lives here — the cheap greps that keep a heading or a quickstart
line from disappearing, and the harder half that checks the document against the things it
claims. HANDOFF is the only thing JD reads before running the app for the first time, so every
command it tells him to type has to exist, every PRP it lists has to be one that shipped, and the
checklists it says are quoted from another document have to still match that document. PRP-10
risk 10 is exactly this — a handoff written from memory, inventing what shipped — and a grep for
headings does not catch it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HANDOFF_MD = REPO / "docs" / "HANDOFF.md"
README_MD = REPO / "README.md"
HANDOFF = HANDOFF_MD.read_text()
README = README_MD.read_text()
BUILD_LOG = (REPO / "docs" / "BUILD-LOG.md").read_text()
MAKEFILE = (REPO / "Makefile").read_text()
CONTRACT = (REPO / "docs" / "vitalforge-contract.md").read_text()

#: Every heading `docs/HANDOFF.md` promises. A cheap grep, so the doc cannot quietly lose one.
HANDOFF_SECTIONS = (
    "## What Cadence is",
    "## What shipped, per PRP",
    "## What was descoped",
    "## Open decisions",
    "## The VitalForge branch to review",
    "## Deploy to VM-201",
    "## Day one",
    "## Where things live",
    "## Known limitations",
    "## Installing as an app",
)

#: `NAME=value` for the secrets, anywhere in the doc. A blank right-hand side is the point.
_SECRET_ASSIGNMENT = re.compile(r"\b(VITALFORGE_TOKEN|OMNIROUTE_KEY|CADENCE_ACCESS_TOKEN)\s*=\s*(\S*)")

#: A recipe line in the Makefile: `name:` at the start of a line, before any `=`.
_TARGET = re.compile(r"^([a-z][a-z0-9-]*):(?!=)", re.M)
#: `make <target>` as the document tells a person to type it.
_MAKE_CALL = re.compile(r"\bmake\s+([a-z][a-z0-9-]*)")
#: A row of the "What shipped, per PRP" table: `| 07 | `prp-07` | ... |`.
_SHIPPED_ROW = re.compile(r"^\|\s*(\d{2})\s*\|\s*`(prp-\d{2})`\s*\|", re.M)


def _section(body: str, heading: str) -> str:
    """Everything under `heading` up to the next heading at the same level or shallower.

    Shallower too: the live-probe checklist is an `###` inside an `##`, and a scan stopping only
    at the next `###` would swallow the two sections after it. Fenced blocks are skipped, because
    the deploy section is mostly shell and `# On VM-201, once.` is a comment, not a heading —
    a regex over the raw text ends the section at the first one and quietly asserts nothing.
    """
    depth = len(heading) - len(heading.lstrip("#"))
    lines = body.splitlines()
    start = lines.index(heading) + 1

    collected: list[str] = []
    fenced = False
    for line in lines[start:]:
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(rf"^#{{1,{depth}}} ", line):
            break
        collected.append(line)
    return "\n".join(collected)


# ------------------------------------------------------------------ the document still exists


def test_handoff_sections_present() -> None:
    assert HANDOFF_MD.exists(), "docs/HANDOFF.md is missing"
    missing = [heading for heading in HANDOFF_SECTIONS if heading not in HANDOFF]
    assert not missing, f"docs/HANDOFF.md has lost {missing}"


def test_handoff_carries_no_token() -> None:
    """The doc quotes the deploy commands verbatim, so it is where a secret would leak."""
    for name, value in _SECRET_ASSIGNMENT.findall(HANDOFF):
        assert value in {"", "''", '""', "..."}, f"{name} looks filled in: {value!r}"


def test_readme_has_quickstart() -> None:
    assert "make seed" in README and "make dev" in README
    assert "AGPL-3.0" in README
    assert "HANDOFF.md" in README


def test_handoff_calls_out_the_open_decisions() -> None:
    """The four defaults PRP-10 section 4 names explicitly, each a thing JD never confirmed."""
    for decision in ("D-009", "D-015", "D-016", "D-018"):
        assert decision in HANDOFF, f"{decision} is not called out in HANDOFF"


# --------------------------------------------------------------------- the commands are real


def test_every_make_command_handoff_quotes_is_a_real_target() -> None:
    """A handoff that tells JD to run a target nobody wrote is worse than one that says nothing."""
    targets = set(_TARGET.findall(MAKEFILE))
    assert {"deploy", "smoke", "perf", "screenshots"} <= targets, "the Makefile lost a PRP-10 target"

    quoted = set(_MAKE_CALL.findall(HANDOFF))
    assert quoted, "no `make` command in HANDOFF, so this would pass vacuously"
    assert quoted <= targets, f"HANDOFF names targets that do not exist: {sorted(quoted - targets)}"


def test_the_deploy_commands_are_the_ones_deploy_md_ships() -> None:
    """HANDOFF says these are quoted verbatim from `docs/deploy.md`; verify the claim."""
    deploy_doc = (REPO / "docs" / "deploy.md").read_text()
    section = _section(HANDOFF, "## Deploy to VM-201")
    assert "docs/deploy.md" in section, "the section no longer says where it was quoted from"

    for command in ("make deploy DEPLOY_MODE=git", "cp .env.example .env", "chmod 600 .env"):
        assert command in section, f"HANDOFF lost {command!r}"
        assert command in deploy_doc, f"{command!r} is in HANDOFF but not in docs/deploy.md"


def test_the_env_keys_handoff_lists_are_the_ones_the_template_ships() -> None:
    """Every key the fill-in table names has to appear in `.env.example`, or day one stalls."""
    example = (REPO / ".env.example").read_text()
    section = _section(HANDOFF, "## Deploy to VM-201")
    keys = [key for key in re.findall(r"`([A-Z][A-Z0-9_]+)`", section) if key != "STATUS"]
    assert keys, "the .env table is empty"
    for key in keys:
        assert re.search(rf"^{key}=", example, re.M), f"{key} is in HANDOFF but not in .env.example"


# ------------------------------------------------------------------ the PRP list is not invented


def test_every_prp_handoff_lists_is_one_the_build_log_records() -> None:
    """Risk 10: section 2 is assembled from `docs/BUILD-LOG.md`, not from memory."""
    rows = _SHIPPED_ROW.findall(HANDOFF)
    assert len(rows) == 11, f"the shipped table has {len(rows)} rows, expected 00-10"

    for number, tag in rows:
        assert tag == f"prp-{number}", f"row {number} cites the tag {tag}"
        assert re.search(rf"\b{tag}\b", BUILD_LOG, re.I), f"{tag} is in HANDOFF but not in BUILD-LOG"


def test_the_build_log_records_no_prp_handoff_forgot() -> None:
    logged = {tag.lower() for tag in re.findall(r"\bprp-\d{2}\b", BUILD_LOG, re.I)}
    listed = {tag for _, tag in _SHIPPED_ROW.findall(HANDOFF)}
    assert logged <= listed, f"BUILD-LOG records PRPs HANDOFF never mentions: {sorted(logged - listed)}"


def test_the_vitalforge_branch_named_is_the_one_the_build_log_records() -> None:
    section = _section(HANDOFF, "## The VitalForge branch to review")
    assert "cadence/activity-endpoint" in section
    assert "cadence/activity-endpoint" in BUILD_LOG, "the branch name drifted from the build log"
    assert "../vitalforge" in section, "HANDOFF no longer says which checkout the branch is in"
    # D-005 is the whole reason the section exists; losing it makes the branch look forgotten.
    assert "D-005" in section


# ------------------------------------------------- the probe checklist still matches the contract

#: Each of the four Garmin-account-only questions, keyed on a phrase neither document can drop
#: without changing what it is asking. Contract §5 holds twelve items; these are the four that
#: need a real account, which is what HANDOFF says it is reproducing.
PROBES = (
    "get_activity_exercise_sets",
    "upload_activity",
    "get_activity_types",
    "get_last_activity",
)


@pytest.mark.parametrize("probe", PROBES)
def test_the_live_probe_checklist_matches_the_contract(probe: str) -> None:
    section = _section(HANDOFF, "### Live-probe checklist (from `docs/vitalforge-contract.md` §5)")
    assert probe in section, f"the probe checklist lost {probe}"
    assert probe in CONTRACT, f"{probe} is in HANDOFF but no longer in the contract"


def test_the_probe_checklist_counts_itself_honestly() -> None:
    """It says "Four things"; a fifth added without touching the sentence is the rot to catch."""
    section = _section(HANDOFF, "### Live-probe checklist (from `docs/vitalforge-contract.md` §5)")
    numbered = re.findall(r"^\d+\.\s+\*\*", section, re.M)
    assert len(numbered) == 4, f"the checklist has {len(numbered)} items but the prose says four"
    assert "Four things" in section


# ----------------------------------------------------------------- installing as an app is a PWA


def test_the_install_section_describes_a_pwa_and_invents_no_store() -> None:
    """D-117: Cadence ships as a PWA. A Play Store step would be a route that does not exist."""
    section = _section(HANDOFF, "## Installing as an app")
    assert "D-117" in section

    lowered = section.lower()
    for step in ("add to home screen", "service worker", "chrome"):
        assert step in lowered, f"the install steps no longer mention {step!r}"


def test_the_install_section_promises_no_app_store() -> None:
    section = _section(HANDOFF, "## Installing as an app").lower()
    for invented in ("play store", "google play", "app store", "f-droid"):
        assert invented not in section, f"the install section invents a {invented} step"
    # The TWA follow-up is allowed to be named, but only as out of scope.
    if "trusted web activity" in section or "twa" in section:
        assert "optional" in section or "out of scope" in section, "the TWA wrapper reads as shipped"


def test_the_known_limitations_name_the_three_the_prp_requires() -> None:
    """PRP-10 section 9 names these three by hand; a limitations list without them is decorative."""
    section = _section(HANDOFF, "## Known limitations").lower()
    assert "carry rows are tick-only" in section
    assert "not auto-scheduled" in section
    assert "readiness" in section and "null" in section
