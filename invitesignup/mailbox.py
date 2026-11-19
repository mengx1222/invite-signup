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

import imaplib
import json
import random
import re
import socket
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
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
    """Base class: create a mailbox, list messages, read one message.

    ``credentials`` and ``reopen`` exist because a temporary address that
    cannot be opened again is a dead end: the site may later send a password
    reset or a security notice to an inbox nobody can read.  A provider that
    genuinely cannot be reopened returns nothing from ``credentials`` rather
    than handing back something that merely looks restorable.
    """

    name = "base"

    def create(self) -> str:
        raise NotImplementedError

    def credentials(self) -> Dict[str, str]:
        """What is needed to open this same inbox again, minus the address."""
        return {}

    def reopen(self, credentials: Dict[str, str]) -> str:
        """Attach to an existing inbox and return its address."""
        raise NotImplementedError(
            f"{self.provider_name} cannot reopen an existing inbox"
        )

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
        self._password: str = ""

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
        # Kept on the instance, not in a local: the password is the only way
        # back into this inbox, and throwing it away made the address
        # write-only -- the account could never be recovered.
        self._password = _random_password()
        r = requests.post(
            f"{self.base}/accounts",
            json={"address": address, "password": self._password},
            proxies=self.proxies, timeout=self.timeout,
        )
        if r.status_code >= 300:
            raise MailboxError(f"mail.tm create failed: {r.status_code} {r.text[:120]}")
        self._address = address
        self._login()
        return address

    def _login(self) -> None:
        r = requests.post(
            f"{self.base}/token",
            json={"address": self._address, "password": self._password},
            proxies=self.proxies, timeout=self.timeout,
        )
        if r.status_code >= 300:
            raise MailboxError(f"mail.tm token failed: {r.status_code} {r.text[:120]}")
        self._token = r.json()["token"]

    def credentials(self) -> Dict[str, str]:
        return {"password": getattr(self, "_password", "")}

    def reopen(self, credentials: Dict[str, str]) -> str:
        address = credentials.get("address", "")
        password = credentials.get("password", "")
        if not (address and password):
            raise MailboxError("mail.tm needs both an address and its password")
        self._address = address
        self._password = password
        self._login()
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

    def credentials(self) -> Dict[str, str]:
        return {"token": self._sid or ""}

    def reopen(self, credentials: Dict[str, str]) -> str:
        address = credentials.get("address", "")
        sid = credentials.get("token", "")
        if not (address and sid):
            raise MailboxError("guerrillamail needs both an address and a session token")
        self._address = address
        self._sid = sid
        return address

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
            # guerrillamail's `seq` is the last mail_id already seen, not a
            # running count.  Incrementing by the batch size walks past gaps
            # in the id sequence and skips messages -- or stops early.
            last_id = batch[-1].get("mail_id")
            if last_id is None:
                break
            seq = last_id
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

    def credentials(self) -> Dict[str, str]:
        return {"token": self._token or ""}

    def reopen(self, credentials: Dict[str, str]) -> str:
        address = credentials.get("address", "")
        token = credentials.get("token", "")
        if not (address and token):
            raise MailboxError("tempmail.io needs both an address and a token")
        self._address = address
        self._token = token
        return address

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


class MailCxProvider(Provider):
    """mail.cx -- pick any address, no signup, mail buffers on arrival.

    This is the first provider that does not *create* anything: the address
    is the mailbox.  Mail starts buffering the moment the SMTP gateway
    accepts it, so "creating a mailbox" and "choosing a name" are the same
    action.  That is what makes a pool practical here -- an address costs a
    string, not an API call.

    Verified by hand before this class existed: an arbitrary address under
    uqu.me received real mail and its body was readable without a token.

    Two limits worth knowing rather than discovering later:

    * Free tier keeps mail for **one hour**.  Enough for a verification
      flow; not a mailbox for later password resets.
    * ``GET /v1/inbox/<addr>`` long-polls for up to 25 s and returns 204 when
      nothing arrived.  Treating 204 as "empty inbox" instead of "keep
      waiting" would make every run look like a mail timeout.
    """

    name = "mail.cx"
    BASE = "https://mail.cx/v1"
    # From GET /v1/config (system_domains), which is public.
    DOMAINS = ("uqu.me", "ddker.com", "9k3r.com")

    def __init__(self, proxy: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self._address = ""
        self._since: Optional[str] = None

    # ---------------- creation ----------------

    def create(self) -> str:
        """Choose an address.  Nothing is registered; the name is the inbox."""
        domain = self.DOMAINS[0]
        self._address = f"{_random_local_part(10)}@{domain}"
        return self._address

    def credentials(self) -> Dict[str, str]:
        # The address alone is enough to read it again, so there is no secret
        # to store -- but the export still has to say so explicitly rather
        # than leave a future caller guessing.
        return {}

    def reopen(self, credentials: Dict[str, str]) -> str:
        address = credentials.get("address", "")
        if not address:
            raise MailboxError("mail.cx needs an address to reopen")
        self._address = address
        return address

    # ---------------- reading ----------------

    def messages(self) -> List[Message]:
        out: List[Message] = []
        for item in self._emails():
            # ``preview_text`` is a truncated teaser -- 180 chars in the real
            # message that exposed this, against a 794-char body.  Putting it
            # in ``body`` made poll() believe the body was already read, so it
            # never fetched the full message and extracted a link cut off
            # mid-token.  Leave body empty; poll() fetches the real thing.
            out.append(Message(
                id=item.get("id", ""),
                subject=item.get("subject", "") or "",
                body="",
                received_at=parse_timestamp(item.get("created_at")),
            ))
        return out

    def _emails(self) -> List[Dict[str, Any]]:
        if not self._address:
            return []
        url = f"{self.BASE}/inbox/{self._address}"
        if self._since:
            url += f"?since={self._since}"
        r = requests.get(url, proxies=self.proxies, timeout=self.timeout + 30)
        if r.status_code == 204:
            # Long-poll elapsed with nothing new.  Not an error, and not an
            # empty inbox -- the caller should simply ask again.
            return []
        if r.status_code >= 300:
            raise MailboxError(f"mail.cx inbox failed: {r.status_code} {r.text[:120]}")
        payload = r.json() or {}
        self._since = payload.get("next_since") or self._since
        return payload.get("emails") or []

    def body(self, message_id: str) -> str:
        r = requests.get(
            f"{self.BASE}/email/{message_id}",
            proxies=self.proxies, timeout=self.timeout,
        )
        if r.status_code >= 300:
            raise MailboxError(f"mail.cx email failed: {r.status_code}")
        data = r.json() or {}
        return flatten(data.get("text_body")) or flatten(data.get("html_body"))


class ImapProvider(Provider):
    """A real mailbox you own, read over IMAP.

    This is the answer to "temporary inboxes keep getting refused": a site
    blocks a *domain*, so the durable fix is a domain or inbox you control,
    not more addresses on a blocklisted one.

    It does not create anything.  ``create`` only checks that the credentials
    work and returns the address, because a mailbox you own already exists --
    pretending to create it would be the kind of fake capability this project
    keeps having to remove.

    Two details that matter in practice:

    * QQ Mail (and several Chinese providers) send GBK-encoded headers and
      bodies.  Decoding with UTF-8 alone yields mojibake, and a verification
      link inside a mangled body still *looks* present, so the failure shows
      up later as "no link arrived".  ``_decode_bytes`` tries the declared
      charset, then GBK, then falls back with replacement.
    * Search by UID and only fetch headers first; a full-body fetch of a
      mailbox with thousands of messages is slow enough to look hung.
    """

    name = "imap"

    # host -> (imap host, port); the address's domain picks the entry.
    PRESETS = {
        "qq.com": ("imap.qq.com", 993),
        "foxmail.com": ("imap.qq.com", 993),
        "163.com": ("imap.163.com", 993),
        "126.com": ("imap.126.com", 993),
        "gmail.com": ("imap.gmail.com", 993),
        "outlook.com": ("outlook.office365.com", 993),
        "hotmail.com": ("outlook.office365.com", 993),
    }

    def __init__(self, proxy: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        # IMAP over a proxy would need a tunnel; rather than silently ignoring
        # the setting, say so when someone tries.
        self.timeout = timeout
        self.proxy = proxy
        self.host = ""
        self.port = 993
        self.address = ""
        self.password = ""
        self.folder = "INBOX"
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    # ---------------- connection ----------------

    def _connect(self) -> imaplib.IMAP4_SSL:
        if self._conn is not None:
            return self._conn
        if not self.host:
            raise MailboxError("imap provider has no host; call configure() first")
        if self.proxy:
            raise MailboxError(
                "IMAP cannot use an HTTP proxy directly; "
                "unset the proxy for this provider or run without one"
            )
        try:
            conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        except (OSError, socket.error) as exc:
            raise MailboxError(f"cannot reach {self.host}:{self.port}: {exc}")
        try:
            conn.login(self.address, self.password)
        except imaplib.IMAP4.error as exc:
            # The usual cause is using the account password instead of the
            # app-specific one, so say that rather than echoing "AUTH failed".
            raise MailboxError(
                f"IMAP login failed for {self.address}: {exc}. "
                "Most providers need an app-specific password here, not the "
                "account password."
            )
        conn.select(self.folder)
        self._conn = conn
        return conn

    def configure(self, address: str, password: str,
                  host: str = "", port: int = 0, folder: str = "") -> None:
        self.address = address
        self.password = password
        if folder:
            self.folder = folder
        if host:
            self.host, self.port = host, port or 993
            return
        domain = address.rsplit("@", 1)[-1].lower()
        preset = self.PRESETS.get(domain)
        if not preset:
            raise MailboxError(
                f"no IMAP host known for {domain!r}; pass host= and port= explicitly"
            )
        self.host, self.port = preset

    def create(self) -> str:
        """Verify the credentials work and return the address.

        Nothing is created: this mailbox already belongs to the operator.
        """
        self._connect()
        return self.address

    def credentials(self) -> Dict[str, str]:
        if not (self.address and self.password):
            return {}
        return {
            "password": self.password,
            "host": self.host,
            "port": str(self.port),
            "folder": self.folder,
        }

    def reopen(self, credentials: Dict[str, str]) -> str:
        self.address = credentials.get("address", "")
        self.password = credentials.get("password", "")
        if not (self.address and self.password):
            raise MailboxError("imap needs both an address and a password")
        self.host = credentials.get("host", "") or self.host
        self.port = int(credentials.get("port") or self.port or 993)
        self.folder = credentials.get("folder", "") or self.folder
        if not self.host:
            domain = self.address.rsplit("@", 1)[-1].lower()
            preset = self.PRESETS.get(domain)
            if not preset:
                raise MailboxError(f"no IMAP host known for {domain!r}")
            self.host, self.port = preset
        self._connect()
        return self.address

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:
            pass
        try:
            self._conn.logout()
        except Exception:
            pass
        self._conn = None

    # ---------------- reading ----------------

    @staticmethod
    def _decode_bytes(raw: bytes, declared: str = "") -> str:
        """Decode header/body bytes, tolerating GBK and other CJK charsets."""
        charsets = []
        if declared:
            charsets.append(declared)
        charsets += ["utf-8", "gb18030", "big5"]
        for charset in charsets:
            try:
                return raw.decode(charset)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _decode_header(value: str) -> str:
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    def _uid_list(self, limit: int) -> List[str]:
        conn = self._connect()
        status, data = conn.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise MailboxError(f"IMAP search failed: {status}")
        uids = (data[0] or b"").split()
        # Newest last in IMAP; take the tail so a huge mailbox stays cheap.
        return [u.decode() for u in uids[-limit:]]

    def messages(self, limit: int = 30) -> List[Message]:
        conn = self._connect()
        out: List[Message] = []
        for uid in self._uid_list(limit):
            status, data = conn.uid(
                "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE FROM)])"
            )
            if status != "OK" or not data or not isinstance(data[0], tuple):
                continue
            header = message_from_bytes(data[0][1])
            subject = self._decode_header(header.get("Subject", ""))
            date = header.get("Date", "")
            out.append(Message(
                id=uid,
                subject=subject,
                received_at=parse_timestamp(_to_imap_time(date)),
            ))
        return out

    def body(self, message_id: str) -> str:
        conn = self._connect()
        status, data = conn.uid("FETCH", str(message_id), "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            return ""
        msg = message_from_bytes(data[0][1])
        return _imap_body_text(msg)


def _to_imap_time(raw: str) -> str:
    """Convert an RFC 2822 Date header into something parse_timestamp reads."""
    from email.utils import parsedate_to_datetime

    if not raw:
        return ""
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return ""
    if parsed is None:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _imap_body_text(msg) -> str:
    """Plain-text view of a MIME message, preferring text/plain."""
    parts: List[str] = []
    plain: List[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = ImapProvider._decode_bytes(payload, part.get_content_charset() or "")
            if ctype == "text/plain":
                plain.append(text)
            else:
                parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            plain.append(
                ImapProvider._decode_bytes(payload, msg.get_content_charset() or "")
            )
    text = "\n".join(plain) or "\n".join(parts)
    if "<" in text and ">" in text and not plain:
        # Verification links survive tag-stripping; keep the href targets too.
        text = re.sub(r"<[^>]+>", " ", text)
    return text


def _random_local_part(length: int = 12) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=length))


def _random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return "".join(random.choices(alphabet, k=length))


PROVIDERS: Dict[str, type] = {}


def register_provider(name: str):
    """Decorator that adds a provider class to the registry.

    The built-in providers register themselves below.  A caller with a
    private inbox (IMAP, a corporate relay) can add one without touching this
    module: subclass :class:`Provider` and decorate it.
    """
    def decorator(cls):
        PROVIDERS[name] = cls
        cls.provider_name = name
        return cls
    return decorator


register_provider("mail.cx")(MailCxProvider)
register_provider("imap")(ImapProvider)
register_provider("mail.tm")(MailTmProvider)
register_provider("tempmail.io")(TempMailIoProvider)
register_provider("guerrillamail")(GuerrillaMailProvider)

# mail.cx leads because choosing a name is the whole creation step: no
# signup, no token, no per-mailbox request, so a run costs nothing to get an
# address.  The rest stay as fallbacks -- if mail.cx is down or a site
# blacklists its domains, the run should still have somewhere to go rather
# than failing on one provider.
#
# imap is deliberately absent: it needs credentials the operator supplies, so
# it can never be "auto-created" and must be requested by name.
DEFAULT_ORDER = ("mail.cx", "mail.tm", "tempmail.io", "guerrillamail")


def available_providers() -> List[str]:
    """Registered provider names, default order first."""
    ordered = [n for n in DEFAULT_ORDER if n in PROVIDERS]
    ordered += sorted(n for n in PROVIDERS if n not in ordered)
    return ordered


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
        operator_owned: bool = False,
    ):
        # True when the mailbox belongs to the operator rather than to us.
        # The distinction decides whether credentials may be written to disk:
        # a temporary inbox is ours to reopen, an app password for someone's
        # real inbox is not.  Keeping that rule here rather than in the caller
        # means a future caller cannot forget it -- which is exactly how the
        # QQ app password ended up in a plaintext file.
        self.operator_owned = operator_owned
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

    def create_preferring(self, preferred: Sequence[str], fallback: Sequence[str] = DEFAULT_ORDER) -> str:
        """Create a mailbox, trying the site's preferred providers first.

        Some sites always refuse a particular provider's domains.  Recording
        that per site (``profile.mailbox_prefer``) turns every run from "fail
        on mail.tm, then succeed on the second try" into "succeed on the
        first try".
        """
        order = list(dict.fromkeys([p for p in preferred if p in PROVIDERS]
                                   + [p for p in fallback if p in PROVIDERS]))
        return self.create(order)

    # ---------------- reopen ----------------

    def export(self) -> Dict[str, str]:
        """Everything needed to open this inbox again later.

        Empty when the provider cannot be reopened, or when the mailbox is
        the operator's own.  An address with no credentials is deliberately
        *not* returned as if it were restorable.
        """
        if self.operator_owned:
            return {}
        if self.provider is None or not self.address:
            return {}
        try:
            creds = self.provider.credentials() or {}
        except Exception:
            return {}
        if not any(creds.values()):
            return {}
        return {
            "provider": getattr(self.provider, "provider_name", self.provider.name),
            "address": self.address,
            **{k: v for k, v in creds.items() if v},
        }

    def reopen(self, saved: Dict[str, str]) -> str:
        """Attach to an inbox described by :meth:`export`."""
        name = saved.get("provider", "")
        cls = PROVIDERS.get(name)
        if cls is None:
            raise NoProviderAvailable(f"unknown mailbox provider: {name!r}")
        self.provider = cls(proxy=self.proxy, timeout=self.timeout)
        self.address = self.provider.reopen(dict(saved))
        return self.address

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
        on_tick: Optional[Callable[[int, int], None]] = None,
    ) -> PollResult:
        """Poll until a verification payload shows up.

        ``mode`` is ``link`` or ``code``.  ``not_before`` is a unix timestamp;
        messages older than it are ignored so a stale code can never be
        mistaken for a fresh one.

        ``before_ids`` is a stronger guard: any message id present in it is
        skipped outright.  Pass the result of :meth:`current_ids` captured
        before the signup request.  Both guards are applied when both are
        given -- a message must be *new by id* and *newer by time*.

        ``on_tick(elapsed_seconds, remaining_seconds)`` is called about once a
        second while waiting.  Waiting for mail is the longest silent stretch
        in the whole pipeline; without this a UI has nothing to show for two
        minutes.
        """
        result = PollResult()
        unmatched: List = []
        if self.provider is None:
            result.errors.append("no mailbox provider")
            return result

        deadline = time.time() + timeout_secs
        interval = 3.0
        seen: set = set(before_ids or ())
        result.baseline = len(seen)
        last_tick = 0.0

        while time.time() < deadline:
            if on_tick is not None:
                now = time.time()
                if now - last_tick >= 1.0:
                    last_tick = now
                    try:
                        on_tick(int(now - (deadline - timeout_secs)),
                                max(0, int(deadline - now)))
                    except Exception:
                        pass
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
                    # Mail arrived but nothing matched the hints.  Remember it
                    # so a timeout can say so: "no mail arrived" and "mail
                    # arrived but the link did not look like a verification
                    # link" need completely different fixes, and reporting the
                    # first when the second is true sends the reader to the
                    # wrong place.
                    if links:
                        unmatched.append((message, links))

            time.sleep(interval)
            interval = min(interval * 1.3, 10.0)

        if unmatched:
            message, links = unmatched[-1]
            result.errors.append(
                f"mail arrived ({message.subject!r}) but none of its "
                f"{len(links)} link(s) matched the expected hints "
                f"{list(link_hints)}; first link was {links[0][:120]}"
            )
        result.errors.extend(self.errors)
        result.dump_path = str(self.dump_path) if self.dump_path else ""
        return result
