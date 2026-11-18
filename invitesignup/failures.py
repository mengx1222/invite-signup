"""Failure taxonomy: decide whether a failure may be retried.

The rule this module encodes is the one that keeps a run from burning
accounts: **a transport failure before the site was asked to send mail may be
retried; anything after that must stop the batch.**  Once the request has
gone out the site has recorded an attempt, so blindly retrying means creating
duplicate accounts or burning invite slots.

The distinction only works if failures are classified instead of being
collapsed into a string.  ``docs/SOP.md`` states the policy; this module is
what makes it executable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Kind(str, Enum):
    """What went wrong."""

    TRANSPORT = "transport"        # DNS, connect, TLS, read timeout -- nothing was committed
    RATE_LIMIT = "rate_limit"      # 429 / explicit throttle
    MAIL_TIMEOUT = "mail_timeout"  # mail never arrived
    MAILBOX_REJECTED = "mailbox_rejected"  # the site refused the mailbox address
    SITE_REJECTED = "site_rejected"  # the site said no: exists, invalid invite, tos
    CAPTCHA = "captcha"            # a challenge appeared
    WALLET = "wallet"              # signup is a wallet signature / OAuth only
    VERIFICATION = "verification"  # account exists but could not be proven
    CONFIG = "config"              # profile is wrong or incomplete
    UNKNOWN = "unknown"


class Action(str, Enum):
    """What the caller is allowed to do next."""

    RETRY = "retry"          # safe: nothing was committed
    STOP = "stop"            # do not retry, do not substitute another account
    ABORT_BATCH = "abort"    # stop the whole batch, not just this item
    NEEDS_HUMAN = "needs_human"  # recoverable, but only by a person


@dataclass(frozen=True)
class Classification:
    kind: Kind
    action: Action
    reason: str

    @property
    def retryable(self) -> bool:
        return self.action is Action.RETRY

    @property
    def needs_human(self) -> bool:
        return self.action is Action.NEEDS_HUMAN

    def summary(self) -> str:
        return f"{self.kind.value}/{self.action.value}: {self.reason}"


# Substrings that identify each kind.  Order matters: the first match wins,
# so the more specific patterns come first.
_SIGNATURES = (
    (Kind.CAPTCHA, ("captcha", "turnstile", "hcaptcha", "recaptcha", "challenge is mounted")),
    (Kind.WALLET, ("wallet signature", "solana", "siwe", "no signup form",
                   "no email or password field")),
    (Kind.RATE_LIMIT, ("429", "too many requests", "rate limit")),
    (Kind.MAILBOX_REJECTED, (
        "disposable", "temporary email", "temp mail", "throwaway",
        "email domain is not allowed", "domain not allowed", "invalid domain",
        "email domain rejected", "domain rejected", "use a real email",
        "邮箱域名", "临时邮箱",
    )),
    (Kind.SITE_REJECTED, (
        "user_already_exists", "already exists", "already registered",
        "invalid invite", "invite code", "not authorized", "forbidden",
        "403", "terms", "tos", "already has an account", "already registered",
    )),
    (Kind.MAIL_TIMEOUT, ("no verification link", "no verification code", "no mail arrived")),
    (Kind.VERIFICATION, ("no session", "sign-in failed", "could not prove")),
    (Kind.CONFIG, ("no email selector", "no password selector", "missing_selectors",
                   "verification_mode_unknown", "missing_link_pattern")),
    (Kind.TRANSPORT, (
        "connection", "timeout", "timed out", "getaddrinfo", "dns",
        "ssl", "tls", "reset by peer", "network is unreachable",
    )),
)


def classify(error: str, *, mail_requested: bool = False) -> Classification:
    """Classify an error message and decide what may happen next.

    ``mail_requested`` is the phase flag: once the signup request has been
    sent, a transport error is no longer safe to retry, because the site may
    already have created the account.
    """
    text = (error or "").lower()

    for kind, needles in _SIGNATURES:
        if not any(needle in text for needle in needles):
            continue
        if kind is Kind.RATE_LIMIT:
            # A throttle is safe to retry while nothing was committed; after
            # the mail request it means the site is pushing back, so stop.
            return Classification(
                kind,
                Action.STOP if mail_requested else Action.RETRY,
                "rate limited" + (" after the signup request" if mail_requested else " before commit"),
            )
        if kind is Kind.TRANSPORT:
            if mail_requested:
                return Classification(
                    kind, Action.ABORT_BATCH,
                    "transport failure after the signup request: the account may exist",
                )
            return Classification(kind, Action.RETRY, "transport failure before commit")
        if kind is Kind.CAPTCHA:
            return Classification(kind, Action.STOP, "a challenge is present; not attempted")
        if kind is Kind.WALLET:
            # Nothing a form filler can do: the site wants a signature.
            return Classification(
                kind, Action.NEEDS_HUMAN,
                "this site signs up with a wallet or an OAuth account, not a form",
            )
        if kind is Kind.MAILBOX_REJECTED:
            # Recoverable, but not by retrying: the same address will be
            # refused again.  This is the one failure where the fix is a
            # different mailbox supplied by a person.
            return Classification(
                kind, Action.NEEDS_HUMAN,
                "the site refused this mailbox; supply a real inbox",
            )
        if kind is Kind.SITE_REJECTED:
            return Classification(kind, Action.ABORT_BATCH, "the site refused this attempt")
        if kind is Kind.MAIL_TIMEOUT:
            return Classification(
                kind, Action.STOP, "mail did not arrive; the account may already exist"
            )
        if kind is Kind.CONFIG:
            return Classification(kind, Action.STOP, "profile is incomplete; fix it first")
        return Classification(kind, Action.STOP, "verification failed")

    return Classification(Kind.UNKNOWN, Action.STOP, "unclassified failure; do not retry blindly")


def should_retry(error: str, *, mail_requested: bool = False) -> bool:
    """Convenience wrapper for callers that only need the yes/no."""
    return classify(error, mail_requested=mail_requested).retryable
