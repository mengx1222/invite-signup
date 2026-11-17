"""Command line entry point.

    python -m invitesignup probe  <invite-url> [--out profile.json]
    python -m invitesignup run    --profile profile.json [--email ...] [--allow-multi]
    python -m invitesignup verify --profile profile.json --email ... --password ...
    python -m invitesignup ledger [--file output/ledger.jsonl]

``probe`` is read-only apart from a single existence check (see
``--no-advance``).  ``run`` creates one account.  ``verify`` proves an
account exists by signing in from a clean session.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import ledger as ledger_mod
from .profile import SiteProfile
from .probe import probe
from .runner import run_registration
from .verdict import decide
from .verify import verify_account


def _record_declined(output_dir: str, profile: SiteProfile, verdict) -> None:
    """Record a run the CLI refused to start.

    The gate in cmd_run() returns before run_registration() is ever called,
    so without this the ledger would not show that an attempt was made and
    declined.  That matters: the ledger is the only place the quota and
    dedupe counts come from, and a silent gap makes "we never tried" and "we
    tried and stopped" indistinguishable.
    """
    try:
        book = ledger_mod.Ledger.open(Path(output_dir) / "ledger.jsonl")
        book.append({
            "site": profile.site,
            "invite_code": profile.invite_code,
            "dedupe_key": ledger_mod.dedupe_key(profile.site, profile.invite_code),
            "verdict": verdict.state,
            "outcome": "declined",
            "stage": "gate-no-yes",
            "email": "",
            "verified": False,
            "reward_credits": profile.reward_credits,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration_s": 0,
            "last_error": "; ".join(verdict.reasons[:3]),
            "notes": verdict.human_step,
        })
    except Exception:
        # Recording the decline must never break the CLI's exit path.
        pass


def cmd_probe(args: argparse.Namespace) -> int:
    result = probe(
        args.invite_url,
        headless=not args.headed,
        locale=args.locale,
        advance=not args.no_advance,
        tos_forbids_automation=args.tos_blocks,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if args.out:
        path = result.profile.save(args.out)
        print(f"[+] profile written to {path}")
    if result.verdict:
        print(f"[verdict] {result.verdict.summary()}")
        if result.verdict.human_step:
            print(f"[human ] {result.verdict.human_step}")
        if result.verdict.requires_confirmation and not args.yes:
            # The verdict allows the run but wants a human to have read the
            # caveats.  Printing them is not enough -- the flag exists so the
            # decision is explicit rather than implied by "the probe exited 0".
            print("[gate  ] this profile needs a human ack before running:")
            for reason in result.verdict.reasons:
                print(f"         - {reason}")
            print("         re-run `run` with --yes once you have read the above")
    for warning in result.warnings:
        print(f"[warn ] {warning}")
    return 0 if (result.verdict and result.verdict.allowed) else 2


def cmd_run(args: argparse.Namespace) -> int:
    profile = SiteProfile.load(args.profile)
    verdict = decide(profile, tos_forbids_automation=args.tos_blocks)
    if verdict.requires_confirmation and not args.yes:
        print(f"[verdict] {verdict.summary()}")
        print("[gate  ] this profile needs an explicit ack; pass --yes to continue")
        for reason in verdict.reasons:
            print(f"         - {reason}")
        if verdict.human_step:
            print(f"[human ] {verdict.human_step}")
        _record_declined(args.output_dir, profile, verdict)
        return 3
    if args.tos_blocks:
        verdict = decide(profile, tos_forbids_automation=True)
        if not verdict.allowed:
            print(f"[verdict] {verdict.summary()}")
            return 2
    result = run_registration(
        profile,
        output_dir=args.output_dir,
        email=args.email,
        password=args.password,
        allow_multi=args.allow_multi,
        force=args.force,
        headless=not args.headed,
        tos_forbids_automation=args.tos_blocks,
        poll_timeout=args.poll_timeout,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    print(f"[stage] {result.stage}  ok={result.ok}  verified={result.verified}")
    for error in result.errors:
        print(f"[err  ] {error}")
    if result.ok:
        print(f"[acct ] {result.email}")
        print(f"[note ] credentials are also in {Path(args.output_dir) / 'accounts.jsonl'}")
    return 0 if result.ok else 1


def cmd_verify(args: argparse.Namespace) -> int:
    profile = SiteProfile.load(args.profile)
    report = verify_account(
        profile,
        args.email,
        args.password,
        sign_in_endpoint=profile.sign_in_endpoint,
        session_endpoint=profile.endpoints.get("session", "/api/auth/get-session"),
        state_endpoints=profile.endpoints_state,
    )
    print(report.summary())
    return 0 if report.verified else 1


def cmd_ledger(args: argparse.Namespace) -> int:
    book = ledger_mod.Ledger.open(args.file)
    for row in book.read():
        print(f"{row.get('started_at','')}  {row.get('outcome',''):8} {row.get('stage',''):24} "
              f"{row.get('email','')}  {row.get('last_error','')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invite-signup", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="discover how a site registers (read-only)")
    p.add_argument("invite_url")
    p.add_argument("--out", help="write the profile JSON here")
    p.add_argument("--locale", default="en-US")
    p.add_argument("--headed", action="store_true", help="show the browser")
    p.add_argument("--no-advance", action="store_true",
                   help="do not perform the existence check that reveals step-2 fields")
    p.add_argument("--tos-blocks", action="store_true",
                   help="declare that the site's terms forbid automation")
    p.add_argument("--yes", action="store_true",
                   help="acknowledge the verdict's caveats without being prompted")
    p.set_defaults(func=cmd_probe)

    r = sub.add_parser("run", help="create one account using an invite code")
    r.add_argument("--profile", required=True)
    r.add_argument("--email", help="use this mailbox instead of a temporary one")
    r.add_argument("--password")
    r.add_argument("--output-dir", default="./output")
    r.add_argument("--allow-multi", action="store_true",
                   help="permit running past the site's own referral quota")
    r.add_argument("--force", action="store_true", help="ignore the dedupe ledger")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--poll-timeout", type=int, default=120)
    r.add_argument("--tos-blocks", action="store_true")
    r.add_argument("--yes", action="store_true",
                   help="acknowledge the verdict's caveats and proceed")
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("verify", help="prove an account exists (independent sign-in)")
    v.add_argument("--profile", required=True)
    v.add_argument("--email", required=True)
    v.add_argument("--password", required=True)
    v.set_defaults(func=cmd_verify)

    l = sub.add_parser("ledger", help="show the run ledger")
    l.add_argument("--file", default="./output/ledger.jsonl")
    l.set_defaults(func=cmd_ledger)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
