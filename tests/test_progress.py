"""Tests for progress accounting and failure codes.

A progress bar is only useful if it moves monotonically, never overshoots,
and does not silently report 0% for a stage it does not know about.  The
failure codes exist so a report can be grepped; they must stay stable.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup import progress  # noqa: E402


def test_every_plan_starts_at_zero_and_increases():
    for kind in ("probe", "run", "verify"):
        plan = progress.plan(kind)
        assert plan, kind
        assert plan[0]["percent"] == 0
        percents = [step["percent"] for step in plan]
        assert percents == sorted(percents), kind
        assert len(set(percents)) == len(percents), f"{kind} has duplicate percentages"


def test_percent_never_reaches_one_hundred_before_the_last_stage():
    # A bar that reads 100% while work remains is a lie.
    for kind in ("probe", "run", "verify"):
        stages = progress.STAGE_SETS[kind]
        for stage, _, _ in stages[:-1]:
            assert progress.percent(kind, stage) < 100, (kind, stage)


def test_unknown_stage_returns_zero_rather_than_guessing():
    # A bar that jumps on an unrecognised stage is worse than one that stays
    # put: the reader would believe progress that is not happening.
    assert progress.percent("run", "some-new-stage") == 0
    assert progress.percent("nonsense", "email") == 0


def test_labels_cover_terminal_stages():
    for stage in (
        "blocked-by-verdict", "blocked-by-captcha", "blocked-by-quota",
        "skipped-duplicate", "mailbox-failed", "mailbox-rejected",
    ):
        label = progress.describe("run", stage)
        assert label and label != stage, f"{stage} has no human label"


def test_every_failure_kind_has_a_code_and_a_hint():
    from invitesignup.failures import Kind

    for kind in Kind:
        code = progress.failure_code(kind.value)
        assert code.startswith("E"), kind
        assert progress.failure_hint(kind.value), f"{kind} has no hint"


def test_codes_are_unique_per_kind():
    from invitesignup.failures import Kind

    codes = [progress.failure_code(k.value) for k in Kind]
    assert len(codes) == len(set(codes)), "two failure kinds share a code"


def test_unknown_kind_falls_back_to_a_generic_code():
    assert progress.failure_code("never-heard-of-it") == "E900"
    assert progress.failure_hint("never-heard-of-it")


def test_completion_is_representable_as_no_current_stage():
    """The UI marks every step done when the job finishes.

    That is expressed as "no stage is current" -- an empty stage name, which
    the checklist resolves to index -1.  A finished job that kept the last
    real stage as "current" showed 100% next to an unticked final step.
    """
    plan = progress.plan("probe")
    assert plan, "probe plan must not be empty"

    # Nothing in the plan matches "", so a lookup yields -1: all done.
    assert all(step["stage"] != "" for step in plan)


def test_last_stage_starts_below_one_hundred():
    # 100% must mean "finished", not "started the final step".
    for kind in ("probe", "run", "verify"):
        plan = progress.plan(kind)
        assert plan[-1]["percent"] < 100, kind
