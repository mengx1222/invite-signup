"""Fail if the package version disagrees with pyproject.toml.

Two places to update meant they had already drifted once (0.1.0 vs 0.2.0).
CI checks the invariant instead of trusting the next editor to remember.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import invitesignup  # noqa: E402

declared = re.search(
    r'^version\s*=\s*"([^"]+)"',
    (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    re.M,
).group(1)

if invitesignup.__version__ != declared:
    print(f"version mismatch: __version__={invitesignup.__version__} pyproject={declared}")
    sys.exit(1)

print(f"version ok: {declared}")