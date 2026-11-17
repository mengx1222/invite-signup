"""Mailbox layer: receive a verification link or code.

Three real lessons are baked into this module, all of them learned the hard
way on a live site:

1. **Body fields are not always strings.**  mail.tm returns the ``html`` part
   as a *list* of fragments.  Joining them with ``"\\n".join(...)`` raises
   ``TypeError``; if that is swallowed by a bare ``except``, the body silently
   becomes empty and the flow dies with "no verification link arrived" --
   which looks exactly like the site blocking you.  ``flatten()`` handles it.

2. **Never swallow an exception into an empty result.**  Every message that
   arrives is dumped to disk, and every failure is recorded with its type.
   A silent empty string is the single most expensive failure mode in mail
   handling.

3. **Only accept payloads that arrived after the request.**  A stale code from
   an earlier message must never be mistaken for a fresh one, so polling is
   anchored on ``not_before`` timestamps.
"""

from __future__ import annotations

import json
import random
import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import requests

DEFAULT_TIMEOUT = 20


# --------------------------------------------------------------------------
# pure helpers (offline-testable)
# --------------------------------------------------------------------------

def flatten(value: Any) -> str:
    """Flatten a possibly-nested mail body field into a single string.

    Providers disagree: some return ``html`` as a string, some as a list of
    fragments, some as a dict.  Normalise them all here.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        parts = [flatten(item) for item in value]
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        parts = [flatten(item) for item in value.values()]
        return "\n".join(p for p in parts if p)
    return str(value)


def parse_timestamp(value: Any) -> float:
    """Normalise whatever a provider calls a timestamp into unix seconds.

    Providers disagree: mail.tm sends an ISO-8601 string
    ("2026-11-17T10:20:06.372Z"), others send milliseconds or seconds as a
    number or a numeric string.  Getting this wrong is silent and expensive:
    an unparsed timestamp becomes 0.0, so a "only accept mail newer than the
    request" filter stops filtering and a stale code from an earlier run gets
    mistaken for a fresh one.
    """
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e11 else number
    text = str(value).strip()
    if not text:
        return 0.0
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return parse_timestamp(float(text))
    normalised = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


LINK_RE = re.compile(r"https?://[^\s\"'<>\\,)]+")


def extract_links(text: str) -> List[str]:
    """Return every http(s) URL in a body, order preserved, de-duplicated."""
    seen: List[str] = []
    for raw in LINK_RE.findall(text or ""):
        cleaned = raw.rstrip(".,;:'\"")
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def extract_code(text: str, pattern: str = r"\b\d{4,8}\b") -> Optional[str]:
    """Pull the first code matching ``pattern`` out of a body."""
    m = re.search(pattern, text or "")
    return m.group(0) if m else None


def pick_link(links: Sequence[str], must_contain: Sequence[str] = ()) -> Optional[str]:
    """Pick the link most likely to be the verification link.

    ``must_contain`` is a list of substrings; a link containing any of them
    wins over a link containing none.
    """
    best: Optional[str] = None
    best_score = -1
    for link in links:
        lowered = link.lower()
        score = sum(1 for hint in must_contain if hint.lower() in lowered)
        if score > best_score:
            best, best_score = link, score
    if best is not None and best_score == 0 and must_contain:
        return None
    return best


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------

class MailboxError(RuntimeError):
    pass


class NoProviderAvailable(MailboxError):
    pass


@dataclass
class Message:
    id: str
    subject: str = ""
    body: str = ""
    received_at: float = 0.0


class Provider:
    """Base class: create a mailbox, list messages, read one message."""

    name = "base"

    def create(self) -> str:
        raise NotImplementedError

    def messages(self) -> List[Message]:
        raise NotImplementedError

    def body(self, message_id: str) -> str:
        raise NotImplementedError


class MailTmProvider(Provider):
    """mail.tm -- JSON REST, Bearer token auth, no signup required for reading."""

    name = "mail.tm"

    def __init__(self, proxy: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        self.base = "https://api.mail.tm"
        self.timeout = timeout
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self._token: Optional[str] = None
        self._address: Optional[str] = None

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def create(self) -> str:
        r = requests.get(f"{self.base}/domains", proxies=self.proxies, timeout=self.timeout)
        r.raise_for_status()
        members = r.json().get("hydra:member") or []
        if not members:
            raise MailboxError("mail.tm returned no domains")
        domain = members[0]["domain"]
        address = f"{_random_local_part()}@{domain}"
        password = _random_password()
        r = requests.post(
            f"{self.base}/accounts",
            json={"address": address, "password": password},
            proxies=self.proxies, timeout=self.timeout,
        )
        if r.status_code >= 300:
            raise MailboxError(f"mail.tm create failed: {r.status_code} {r.text[:120]}")
        r = requests.post(
            f"{self.base}/token",
            json={"address": address, "password": password},
            proxies=self.proxies, timeout=self.timeout,
        )
        if r.status_code >= 300:
            raise MailboxError(f"mail.tm token failed: {r.status_code} {r.text[:120]}")
        self._token = r.json()["token"]
        self._address = address
        return address

    def messages(self) -> List[Message]:
        r = requests.get(
            f"{self.base}/messages",
            headers=self._headers(), proxies=self.proxies, timeout=self.timeout,
        )
        r.raise_for_status()
        out: List[Message] = []
        for m in r.json().get("hydra:member") or []:
            out.append(Message(
                id=m.get("id"),
                subject=m.get("subject", "") or "",
                received_at=parse_timestamp(m.get("createdAt")),
            ))
        return out

    def body(self, message_id: str) -> str:
        r = requests.get(
            f"{self.base}/messages/{message_id}",
            headers=self._headers(), proxies=self.proxies, timeout=self.timeout,
        )
        r.raise_for_status()
        j = r.json()
        # NOTE: `html` is a LIST on mail.tm.  flatten() is what makes this work.
        return "\n".join(filter(None, [
            flatten(j.get("subject")),
            flatten(j.get("text")),
            flatten(j.get("html")),
            flatten(j.get("intro")),
        ]))


class GuerrillaMailProvider(Provider):
    """guerrillamail -- query-string API, session token, no signup."""

    name = "guerrillamail"

    def __init__(self, proxy: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        self.base = "https://api.guerrillamail.com/ajax.php"
        self.timeout = timeout
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self._sid: Optional[str] = None
        self._address: Optional[str] = None

    def create(self) -> str:
        r = requests.get(
            f"{self.base}?f=get_email_address&ip=127.0.0.1&agent=autoregister",
            proxies=self.proxies, timeout=self.timeout,
        )
        r.raise_for_status()
        j = r.json()
        self._sid = j.get("sid_token")
        self._address = j.get("email_addr")
        if not (self._sid and self._address):
            raise MailboxError("guerrillamail returned no address")
        return self._address

    def messages(self) -> List[Message]:
        out: List[Message] = []
        seq = 0
        seen = set()
        while seq < 25:
            r = requests.get(
                f"{self.base}?f=check_email&seq={seq}&sid_token={self._sid}",
                proxies=self.proxies, timeout=self.timeout,
            )
            r.raise_for_status()
            batch = r.json().get("list") or []
            if not batch:
                break
            advanced = False
            for m in batch:
                mid = m.get("mail_id")
                if mid in seen:
                    continue
                seen.add(mid)
                advanced = True
                out.append(Message(id=mid, subject=m.get("mail_subject", "") or ""))
            if not advanced:
                break
            seq += len(batch)
        return out

    def body(self, message_id: str) -> str:
        r = requests.get(
            f"{self.base}?f=fetch_email&email_id={message_id}&sid_token={self._sid}",
            proxies=self.proxies, timeout=self.timeout,
        )
        r.raise_for_status()
        j = r.json()
        raw = "\n".join(filter(None, [flatten(j.get("mail_subject")), flatten(j.get("mail_body"))]))
        return re.sub(r"<[^>]+>", " ", raw)


class TempMailIoProvider(Provider):
    """tempmail.io -- JSON REST, Bearer token."""

    name = "tempmail.io"

    def __init__(self, proxy: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        self.base = "https://api.internal.temp-mail.io/api/v3"
        self.timeout = timeout
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self._token: Optional[str] = None
        self._address: Optional[str] = None

    def create(self) -> str:
        r = requests.get(f"{self.base}/domains", proxies=self.proxies, timeout=self.timeout)
        r.raise_for_status()
        payload = r.json()
        if isinstance(payload, dict):
            domains = payload.get("domains") or []
            domain = domains[0]["name"] if domains else None
        elif isinstance(payload, list):
            first = payload[0] if payload else None
            domain = first["name"] if isinstance(first, dict) else first
        else:
            domain = None
        if not domain:
            raise MailboxError("tempmail.io returned no domain")
        r = requests.post(
            f"{self.base}/email/new",
            json={"name": _random_local_part(), "domain": domain},
            proxies=self.proxies, timeout=self.timeout,
        )
        if r.status_code >= 300:
            raise MailboxError(f"tempmail.io create failed: {r.status_code} {r.text[:120]}")
        j = r.json()
        self._token = j.get("token")
        self._address = j.get("email")
        if not self._address:
            raise MailboxError("tempmail.io returned no address")
        return self._address

    def messages(self) -> List[Message]:
        r = requests.get(
            f"{self.base}/email/{self._address}/messages",
            headers={"Authorization": f"Bearer {self._token}"},
            proxies=self.proxies, timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data = data.get("messages") or []
        return [
            Message(id=m.get("id"), subject=m.get("subject", "") or "",
                    body=flatten(m.get("body_text")) or flatten(m.get("body_html")))
            for m in data or []
        ]

    def body(self, message_id: str) -> str:
        for m in self.messages():
            if m.id == message_id:
                return m.body
        return ""


def _random_local_part(length: int = 12) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=length))


def _random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return "".join(random.choices(alphabet, k=length))


PROVIDERS = {
    "mail.tm": MailTmProvider,
    "tempmail.io": TempMailIoProvider,
    "guerrillamail": GuerrillaMailProvider,
}
DEFAULT_ORDER = ("mail.tm", "tempmail.io", "guerrillamail")


# --------------------------------------------------------------------------
# polling
# --------------------------------------------------------------------------

@dataclass
class PollResult:
    found: bool = False
    payload: str = ""
    message: Optional[Message] = None
    errors: List[str] = field(default_factory=list)
    dump_path: str = ""
    baseline: int = 0


class Mailbox:
    """A mailbox plus the polling logic that extracts a verification payload."""

    def __init__(
        self,
        provider: Optional[Provider] = None,
        dump_path: Optional[str | Path] = None,
        proxy: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.provider = provider
        self.dump_path = Path(dump_path) if dump_path else None
        self.proxy = proxy
        self.timeout = timeout
        self.address: Optional[str] = None
        self.errors: List[str] = []

    # ---------------- creation ----------------

    def create(self, order: Sequence[str] = DEFAULT_ORDER) -> str:
        """Create a mailbox, failing over between providers."""
        errors: List[str] = []
        for name in order:
            cls = PROVIDERS.get(name)
            if cls is None:
                continue
            try:
                self.provider = cls(proxy=self.proxy, timeout=self.timeout)
                self.address = self.provider.create()
                return self.address
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                self.provider = None
        raise NoProviderAvailable(
            "all mailbox providers failed:\n  " + "\n  ".join(errors)
        )

    # ---------------- dumping ----------------

    def _dump(self, message: Message, body: str) -> None:
        """Persist every message we see.  A silent empty body is the enemy."""
        if not self.dump_path:
            return
        try:
            self.dump_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.dump_path, "a", encoding="utf-8") as f:
                f.write(f"=== id={message.id} subject={message.subject!r}\n")
                f.write(f"--- body ({len(body or '')} chars) ---\n{(body or '')[:1500]}\n")
                f.write("--- links ---\n")
                for link in extract_links(body):
                    f.write(f"  {link}\n")
                f.write("\n")
        except Exception as exc:
            self.errors.append(f"dump failed: {type(exc).__name__}: {exc}")

    # ---------------- polling ----------------

    def current_ids(self) -> set:
        """Return the set of message ids that already exist.

        This is the baseline half of the staleness guard: capture it *before*
        triggering the mail, and only accept messages outside it.  A timestamp
        alone is not enough -- provider clocks drift, and a message sent in the
        same second as the request is ambiguous.
        """
        if self.provider is None:
            return set()
        try:
            return {m.id for m in self.provider.messages()}
        except Exception as exc:
            self.errors.append(f"baseline failed: {type(exc).__name__}: {exc}")
            return set()

    def poll(
        self,
        mode: str = "link",
        pattern: str = "",
        link_hints: Sequence[str] = ("verify", "token", "callback", "confirm"),
        timeout_secs: int = 120,
        not_before: Optional[float] = None,
        before_ids: Optional[set] = None,
        subject_hint: str = "",
    ) -> PollResult:
        """Poll until a verification payload shows up.

        ``mode`` is ``link`` or ``code``.  ``not_before`` is a unix timestamp;
        messages older than it are ignored so a stale code can never be
        mistaken for a fresh one.

        ``before_ids`` is a stronger guard: any message id present in it is
        skipped outright.  Pass the result of :meth:`current_ids` captured
        before the signup request.  Both guards are applied when both are
        given -- a message must be *new by id* and *newer by time*.
        """
        result = PollResult()
        if self.provider is None:
            result.errors.append("no mailbox provider")
            return result

        deadline = time.time() + timeout_secs
        interval = 3.0
        seen: set = set(before_ids or ())
        result.baseline = len(seen)

        while time.time() < deadline:
            try:
                messages = self.provider.messages()
            except Exception as exc:
                result.errors.append(f"{type(exc).__name__}: {exc}")
                # Rate limits are common on the free providers; back off
                # further than a plain retry rather than hammering the API.
                backoff = interval * 2 if "429" in str(exc) else interval
                time.sleep(backoff)
                interval = min(interval * 1.3, 15.0)
                continue

            for message in messages:
                if message.id in seen:
                    continue
                seen.add(message.id)
                try:
                    body = message.body or self.provider.body(message.id)
                except Exception as exc:
                    result.errors.append(f"body read failed for {message.id}: {type(exc).__name__}: {exc}")
                    body = ""
                self._dump(message, body)

                if not body:
                    continue
                if not_before and message.received_at and message.received_at < not_before:
                    continue
                if subject_hint and subject_hint.lower() not in (message.subject or "").lower():
                    continue

                if mode == "code":
                    code = extract_code(body, pattern or r"\b\d{4,8}\b")
                    if code:
                        result.found = True
                        result.payload = code
                        result.message = message
                        return result
                else:
                    links = extract_links(body)
                    chosen = pick_link(links, link_hints)
                    if chosen:
                        result.found = True
                        result.payload = chosen
                        result.message = message
                        return result

            time.sleep(interval)
            interval = min(interval * 1.3, 10.0)

        result.errors.extend(self.errors)
        result.dump_path = str(self.dump_path) if self.dump_path else ""
        return result
