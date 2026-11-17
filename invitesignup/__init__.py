"""invite-signup -- a reusable pipeline for invite-gated registration flows.

    probe  -> discover a site's registration shape (read-only)
    run    -> create one account through an invite link
    verify -> prove the account exists, from a clean session

The pipeline refuses to run when a captcha is present or when the site's terms
forbid automation.  See docs/SOP.md for the full method and docs/LESSONS.md
for the failure modes that shaped it.
"""

__version__ = "0.1.0"

from .profile import SiteProfile
from .verdict import Verdict, decide

__all__ = ["SiteProfile", "Verdict", "decide", "__version__"]
