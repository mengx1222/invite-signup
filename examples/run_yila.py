"""End-to-end example: one Yila registration, driven by a profile.

    python examples/run_yila.py --invite-code CODE

The invite code is read from the command line or the environment; it is never
stored in this repository.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.yila import build_profile  # noqa: E402
from invitesignup.runner import run_registration  # noqa: E402
from invitesignup.verify import verify_account  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--invite-code", default=os.environ.get("INVITE_CODE", ""))
    parser.add_argument("--output-dir", default="./output")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    if not args.invite_code:
        print("usage: python examples/run_yila.py --invite-code CODE")
        print("       (or set the INVITE_CODE environment variable)")
        return 2

    profile = build_profile(args.invite_code)
    result = run_registration(profile, output_dir=args.output_dir, headless=not args.headed)

    print(f"stage={result.stage} ok={result.ok} verified={result.verified}")
    for error in result.errors:
        print(f"  [err] {error}")
    if not result.ok:
        return 1

    print(f"  [acct] {result.email}")

    # Independent verification -- a fresh process would do this, but calling
    # the module directly already means a clean HTTP session with no cookies
    # inherited from the browser run.
    report = verify_account(
        profile,
        result.email,
        result.password,
        sign_in_endpoint=profile.sign_in_endpoint,
        session_endpoint=profile.endpoints.get("session", "/api/auth/get-session"),
        state_endpoints=profile.endpoints_state,
    )
    print(report.summary())
    return 0 if report.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
