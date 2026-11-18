"""Command line entry point.

    python -m invitesignup probe  <invite-url> [--out profile.json]
    python -m invitesignup run    --profile profile.json [--email ...] [--allow-multi]
    python -m invitesignup verify --profile profile.json --email ... --password ...
    python -m invitesignup ledger [--file output/ledger.jsonl]
    python -m invitesignup inbox  --email <address> [--file output/accounts.jsonl]
    python -m invitesignup inbox-check --email ... --password <app password>

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
from typing import Optional

from . import ledger as ledger_mod
from .profile import SiteProfile
from .probe import probe
from .runner import run_registration
from .verdict import decide
from .verify import verify_account


def _use_utf8_console() -> None:
    """Make Chinese mail subjects readable on a Windows console.

    cmd.exe and PowerShell default to the legacy GBK code page here, so a
    perfectly decoded subject like "您的 Google 账号已成功恢复" prints as
    replacement characters.  That looks exactly like a decoding bug in the
    mailbox layer, which is where time then gets wasted.  The data was never
    wrong -- only the terminal was.
    """
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


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
    inferred = [k for k, v in (result.profile.confidence or {}).items() if v != "measured"]
    if inferred:
        print(f"[conf  ] inferred, not measured: {', '.join(inferred)}")
    review = result.profile.needs_review()
    if review:
        print("[review] confirm before trusting this profile:")
        for item in review:
            print(f"         - {item}")
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
        inbox_password=args.inbox_password,
        allow_multi=args.allow_multi,
        force=args.force,
        headless=not args.headed,
        tos_forbids_automation=args.tos_blocks,
        poll_timeout=args.poll_timeout,
        link_prompt=_paste_link_prompt if args.wait_link else None,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    print(f"[stage] {result.stage}  ok={result.ok}  verified={result.verified}")
    for error in result.errors:
        print(f"[err  ] {error}")
    if result.ok:
        print(f"[acct ] {result.email}")
        print(f"[note ] credentials are also in {Path(args.output_dir) / 'accounts.jsonl'}")
    return 0 if result.ok else 1


def _paste_link_prompt() -> Optional[str]:
    """Ask the operator to paste the verification link.

    Used by --wait-link: the account has been created and the mail has been
    sent, but it went to an inbox this tool cannot read.  Everything before
    and after this one step stays automated.
    """
    print()
    print("=" * 68)
    print("  the verification mail was sent to your own inbox.")
    print("  open it, copy the verification link, and paste it below.")
    print("=" * 68)
    try:
        link = input("[?] verification link (empty to abort): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return link or None


def cmd_verify(args: argparse.Namespace) -> int:
    profile = SiteProfile.load(args.profile)
    report = verify_account(
        profile,
        args.email,
        args.password,
        sign_in_endpoint=profile.sign_in_endpoint,
        session_endpoint=profile.endpoints.get("session", ""),
        state_endpoints=profile.endpoints_state,
    )
    print(report.summary())
    return 0 if report.verified else 1


def cmd_inbox_check(args: argparse.Namespace) -> int:
    """Prove an IMAP inbox works before a run depends on it.

    Connecting is the only honest test: an address that resolves but cannot
    authenticate looks identical to a working one until the first real run.
    """
    from .mailbox import ImapProvider, MailboxError

    provider = ImapProvider()
    try:
        provider.configure(
            args.email, args.password,
            host=args.host or "", port=int(args.port or 0),
            folder=args.folder or "",
        )
        provider.create()
    except MailboxError as exc:
        print(f"[-] {exc}")
        return 1
    except Exception as exc:
        print(f"[-] {type(exc).__name__}: {exc}")
        return 1

    print(f"[ok] connected to {provider.host}:{provider.port} as {provider.address}")
    try:
        messages = provider.messages(limit=args.limit)
    except Exception as exc:
        print(f"[!] connected but listing failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"[ok] {len(messages)} message(s) visible in {provider.folder}")
    for m in messages[-5:]:
        print(f"     {m.subject or '(no subject)'}   uid={m.id}")
    provider.close()
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    """Reopen a temporary inbox recorded when the account was created.

    The credentials exist so that mail arriving *after* signup -- a password
    reset, a security notice -- is still readable.  Without a way to use them
    they would be decoration, so this command is the proof they work.
    """
    from .mailbox import Mailbox

    rows = []
    path = Path(args.file)
    if not path.exists():
        print(f"[-] no account file at {path}")
        return 1
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue

    wanted = (args.email or "").strip().lower()
    match = None
    for row in reversed(rows):
        if (row.get("email") or "").lower() == wanted:
            match = row
            break
    if match is None:
        print(f"[-] no account recorded for {args.email}")
        return 1

    saved = match.get("inbox") or {}
    if not saved:
        print(f"[!] {match.get('email')} has no stored inbox credentials.")
        print("    Either the mailbox was yours already, or it was created")
        print("    before credentials were recorded. Nothing to reopen.")
        return 1

    box = Mailbox()
    try:
        box.reopen(saved)
    except Exception as exc:
        print(f"[-] could not reopen: {type(exc).__name__}: {exc}")
        return 1

    messages = box.provider.messages() if box.provider else []
    print(f"[inbox] {box.address}  ({saved.get('provider','')})")
    if not messages:
        print("        no messages")
        return 0
    for m in messages:
        print(f"  {m.subject or '(no subject)'}   id={m.id}")
    if args.show:
        for m in messages:
            print()
            print("=" * 60)
            print(m.subject)
            print("=" * 60)
            print((box.provider.body(m.id) if box.provider else "")[:4000])
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    book = ledger_mod.Ledger.open(args.file)
    for row in book.read():
        print(f"{row.get('started_at','')}  {row.get('outcome',''):8} {row.get('stage',''):24} "
              f"{row.get('email','')}  {row.get('last_error','')}")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Start the local web UI."""
    try:
        from .web import serve
    except ImportError as exc:
        print(f"[-] the web UI needs Flask: {exc}")
        print("    pip install flask")
        return 1
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"[!] binding {args.host} exposes this tool on the network.")
        print("    it holds account credentials; prefer 127.0.0.1")
    serve(host=args.host, port=args.port, output_dir=args.output_dir)
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
    r.add_argument("--inbox-password",
                   help="app password for --email, so verification is read over IMAP "
                        "instead of waiting for a pasted link")
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
    r.add_argument("--wait-link", action="store_true",
                   help="stop after sending mail and let you paste the verification link")
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("verify", help="prove an account exists (independent sign-in)")
    v.add_argument("--profile", required=True)
    v.add_argument("--email", required=True)
    v.add_argument("--password", required=True)
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("inbox-check",
                       help="prove an IMAP inbox works before relying on it")
    c.add_argument("--email", required=True)
    c.add_argument("--password", required=True,
                   help="app-specific password, not the account password")
    c.add_argument("--host", help="override the preset IMAP host")
    c.add_argument("--port", type=int, default=0)
    c.add_argument("--folder", default="")
    c.add_argument("--limit", type=int, default=10)
    c.set_defaults(func=cmd_inbox_check)

    i = sub.add_parser("inbox", help="reopen a temporary inbox used for signup")
    i.add_argument("--email", required=True, help="the address the account was created with")
    i.add_argument("--file", default="./output/accounts.jsonl")
    i.add_argument("--show", action="store_true", help="print message bodies")
    i.set_defaults(func=cmd_inbox)

    l = sub.add_parser("ledger", help="show the run ledger")
    l.add_argument("--file", default="./output/ledger.jsonl")
    l.set_defaults(func=cmd_ledger)

    w = sub.add_parser("web", help="start the local web UI")
    w.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: local only)")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--output-dir", default="./output")
    w.set_defaults(func=cmd_web)

    return parser


def main(argv=None) -> int:
    _use_utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
