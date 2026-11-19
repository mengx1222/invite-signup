"""invite-signup -- a reusable pipeline for invite-gated registration flows.

    probe  -> discover a site's registration shape (read-only)
    run    -> create one account through an invite link
    verify -> prove the account exists, from a clean session

The pipeline refuses to run when a captcha is present or when the site's terms
forbid automation.  See docs/SOP.md for the full method and docs/LESSONS.md
for the failure modes that shaped it.
"""

def _read_version() -> str:
    """Read the version from installed metadata, falling back to the source.

    Keeping a literal here meant two places to update, and they had already
    drifted (0.1.0 here, 0.2.0 in pyproject.toml).  A version that disagrees
    with the package is worse than no version: it makes "which build is
    this?" unanswerable.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("invite-signup")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    try:
        from pathlib import Path
        import re as _re
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        m = _re.search(r'^version\s*=\s*"([^"]+)"', text, _re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "0.0.0+unknown"


__version__ = _read_version()

from .profile import SiteProfile
from .verdict import Verdict, decide

__all__ = ["SiteProfile", "Verdict", "decide", "__version__"]
