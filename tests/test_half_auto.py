"""Tests for --wait-link and the early mailbox-rejection check.

Both exist because the most common real-world snag is "the site will not
accept a disposable address".  Previously that surfaced as a two-minute wait
followed by "no verification link arrived", which says nothing about the
actual cause.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import invitesignup.mailbox  # noqa: E402
from invitesignup.mailbox import PROVIDERS, Mailbox, Provider  # noqa: E402
from invitesignup.probe import TrafficLog  # noqa: E402
from invitesignup.runner import _mailbox_was_rejected  # noqa: E402


def test_rejection_phrases_are_recognised():
    for text in (
        "Disposable email addresses are not allowed",
        "Please use a real email",
        "email domain is not allowed",
        "不支持临时邮箱",
    ):
        assert _mailbox_was_rejected(text) is True, text


def test_ordinary_error_text_is_not_a_rejection():
    for text in (
        "Invalid password",
        "This field is required",
        "",
    ):
        assert _mailbox_was_rejected(text) is False, text


def test_traffic_log_accepts_non_api_prefixes():
    # Hardcoding /api/ silently drops /v1/, /graphql and custom auth routes.
    log = TrafficLog()
    log.add("POST", "https://example.test/v1/signup")
    log.add("POST", "https://example.test/graphql")
    log.add("GET", "https://example.test/auth/session")
    assert "/v1/signup" in log.paths
    assert "/graphql" in log.paths
    assert "/auth/session" in log.paths


def test_traffic_log_prefixes_are_overridable():
    log = TrafficLog(prefixes=("/custom/",))
    log.add("GET", "https://example.test/custom/ping")
    log.add("GET", "https://example.test/api/ignored")
    assert log.paths == ["/custom/ping"]


def _fake_provider(name, *, restorable=True, messages=()):
    """A provider that behaves like a real one but talks to nothing."""

    class Fake(Provider):
        provider_name = name

        def __init__(self, proxy=None, timeout=20):
            self.address = ""

        def create(self):
            self.address = f"box@{name}.test"
            return self.address

        def credentials(self):
            return {"password": "pw-123"} if restorable else {}

        def reopen(self, creds):
            self.address = creds.get("address", "")
            return self.address

        def messages(self):
            return list(messages)

        def body(self, message_id):
            return "body"

    return Fake


def test_an_inbox_can_be_reopened_from_its_saved_credentials():
    """Storing an address without its password makes the inbox write-only.

    A site that later sends a password reset has nowhere readable to send it.
    """
    Fake = _fake_provider("restorable")
    PROVIDERS["restorable"] = Fake
    try:
        box = Mailbox()
        address = box.create(order=["restorable"])
        saved = box.export()

        assert saved["address"] == address
        assert saved["provider"] == "restorable"
        assert saved["password"] == "pw-123"

        again = Mailbox()
        assert again.reopen(saved) == address
    finally:
        PROVIDERS.pop("restorable", None)


def test_mail_tm_keeps_the_password_it_created_the_inbox_with():
    """The real provider, with the network stubbed out.

    The original bug was a local variable: `password = _random_password()`
    was used to create the account and then dropped when create() returned,
    so the address could never be opened again.  Testing a fake provider
    cannot catch that -- the password has to be thrown away in *this* class
    for the test to mean anything.
    """
    import types

    from invitesignup.mailbox import MailTmProvider

    seen = {}

    class Resp:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        return Resp({"hydra:member": [{"domain": "example.test"}]})

    def fake_post(url, json=None, **kw):
        if url.endswith("/accounts"):
            seen["created_password"] = json["password"]
            return Resp({"id": "1"})
        if url.endswith("/token"):
            seen["login_password"] = json["password"]
            return Resp({"token": "tok"})
        raise AssertionError(url)

    fake = types.SimpleNamespace(get=fake_get, post=fake_post)
    original = invitesignup.mailbox.requests
    invitesignup.mailbox.requests = fake
    try:
        provider = MailTmProvider()
        address = provider.create()
        creds = provider.credentials()
    finally:
        invitesignup.mailbox.requests = original

    assert address.endswith("@example.test")
    assert creds.get("password"), "the inbox password was discarded"
    assert creds["password"] == seen["created_password"]
    assert seen["login_password"] == seen["created_password"]


def test_a_provider_that_cannot_be_reopened_exports_nothing():
    """Returning a bare address would look restorable and fail later."""
    Fake = _fake_provider("one-shot", restorable=False)
    PROVIDERS["one-shot"] = Fake
    try:
        box = Mailbox()
        box.create(order=["one-shot"])
        assert box.export() == {}
    finally:
        PROVIDERS.pop("one-shot", None)


def test_reopening_an_unknown_provider_says_so():
    from invitesignup.mailbox import NoProviderAvailable

    try:
        Mailbox().reopen({"provider": "gone-forever", "address": "a@b.test"})
    except NoProviderAvailable as exc:
        assert "gone-forever" in str(exc)
    else:
        raise AssertionError("an unknown provider was accepted")


def test_an_operator_supplied_mailbox_exports_nothing():
    """When the address is the operator's own, there is nothing to store."""
    box = Mailbox()
    assert box.export() == {}


def test_a_profile_can_record_which_provider_this_site_accepts():
    """A site that refuses one domain every time should not cost a failed run
    on every attempt.  The preference is data on the profile, so re-running
    is enough -- no code change, no new flag to remember.
    """
    from invitesignup.profile import SiteProfile

    p = SiteProfile(site="example.test", mailbox_prefer=["pref-b"])
    assert p.to_dict()["mailbox_prefer"] == ["pref-b"]

    restored = SiteProfile.from_dict(p.to_dict())
    assert restored.mailbox_prefer == ["pref-b"]

    # A profile written before this field existed must still load.
    old = SiteProfile(site="legacy.test").to_dict()
    old.pop("mailbox_prefer")
    assert SiteProfile.from_dict(old).mailbox_prefer == []


def test_imap_host_is_chosen_from_the_address_domain():
    from invitesignup.mailbox import ImapProvider

    p = ImapProvider()
    p.configure("someone@qq.com", "pw")
    assert (p.host, p.port) == ("imap.qq.com", 993)

    p2 = ImapProvider()
    p2.configure("someone@gmail.com", "pw")
    assert p2.host == "imap.gmail.com"


def test_an_unknown_imap_domain_is_refused_rather_than_guessed():
    from invitesignup.mailbox import ImapProvider, MailboxError

    try:
        ImapProvider().configure("someone@unknown-provider.test", "pw")
    except MailboxError as exc:
        assert "no IMAP host known" in str(exc)
    else:
        raise AssertionError("an unknown domain was silently accepted")


def test_an_explicit_imap_host_wins_over_the_preset_table():
    from invitesignup.mailbox import ImapProvider

    p = ImapProvider()
    p.configure("someone@qq.com", "pw", host="mail.example.test", port=143)
    assert (p.host, p.port) == ("mail.example.test", 143)


def test_gbk_encoded_mail_is_decoded_not_mangled():
    """QQ Mail sends GBK.  Decoding as UTF-8 gives mojibake, and a mangled
    body still contains a link-shaped string, so the damage shows up later as
    "no verification link arrived" rather than as an encoding error.
    """
    from invitesignup.mailbox import ImapProvider

    original = "验证码是 123456，请点击 https://example.test/verify?token=abc"
    raw = original.encode("gbk")
    assert ImapProvider._decode_bytes(raw, "gbk") == original
    # Even when the declared charset is wrong, the CJK fallback must catch it.
    assert ImapProvider._decode_bytes(raw, "utf-8") == original


def test_undecodable_bytes_do_not_raise():
    from invitesignup.mailbox import ImapProvider

    text = ImapProvider._decode_bytes(b"\xff\xfe\x00\x01")
    assert isinstance(text, str)


def test_imap_credentials_round_trip():
    from invitesignup.mailbox import ImapProvider

    p = ImapProvider()
    p.configure("someone@qq.com", "app-password")
    saved = p.credentials()
    assert saved["password"] == "app-password"
    assert saved["host"] == "imap.qq.com"
    assert saved["port"] == "993"


def test_imap_reopen_needs_both_address_and_password():
    from invitesignup.mailbox import ImapProvider, MailboxError

    for creds in ({"address": "a@qq.com"}, {"password": "x"}):
        try:
            ImapProvider().reopen(creds)
        except MailboxError:
            pass
        else:
            raise AssertionError(f"{creds} was accepted")


def test_imap_refuses_to_pretend_a_proxy_works():
    from invitesignup.mailbox import ImapProvider, MailboxError

    p = ImapProvider(proxy="http://127.0.0.1:7890")
    p.configure("someone@qq.com", "pw")
    try:
        p._connect()
    except MailboxError as exc:
        assert "proxy" in str(exc).lower()
    else:
        raise AssertionError("a proxy was silently ignored")


def test_a_mime_body_yields_its_text_and_links():
    from email.message import EmailMessage

    from invitesignup.mailbox import _imap_body_text, extract_links

    msg = EmailMessage()
    msg["Subject"] = "Verify"
    msg.set_content("Please confirm: https://example.test/verify?token=abc")
    text = _imap_body_text(msg)
    assert "https://example.test/verify?token=abc" in extract_links(text)


def test_an_imap_inbox_can_be_polled_for_a_link(monkeypatch):
    """The end the IMAP provider exists for: read a verification link from a
    mailbox the operator owns, with no human in the loop.

    imaplib is stubbed, so this exercises the real fetch/decode/extract path
    without a network or an account.
    """
    from invitesignup.mailbox import ImapProvider, Mailbox

    link = "https://example.test/verify?token=abc123"
    raw = (
        b"Subject: Verify your email\r\n"
        b"Date: Mon, 18 Nov 2026 10:00:00 +0800\r\n"
        b"Content-Type: text/plain; charset=gbk\r\n\r\n"
        + ("请点击验证: " + link).encode("gbk")
    )

    class FakeConn:
        def login(self, address, password):
            assert password == "app-pw"
            return ("OK", [b""])

        def select(self, folder):
            return ("OK", [b"1"])

        def uid(self, command, *args):
            if command == "SEARCH":
                return ("OK", [b"7"])
            if command == "FETCH":
                if "HEADER.FIELDS" in str(args[-1]):
                    header = (
                        b"Subject: Verify your email\r\n"
                        b"Date: Mon, 18 Nov 2026 10:00:00 +0800\r\n\r\n"
                    )
                    return ("OK", [(b"1", header)])
                return ("OK", [(b"1", raw)])
            return ("OK", [b""])

        def close(self):
            return ("OK", [])

        def logout(self):
            return ("BYE", [])

    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda *a, **k: FakeConn())

    provider = ImapProvider()
    provider.configure("me@qq.com", "app-pw")
    box = Mailbox(provider=provider)
    box.address = provider.create()

    polled = box.poll(mode="link", timeout_secs=5, not_before=0, before_ids=set())
    assert polled.found, polled.errors
    assert "token=abc123" in polled.payload


def test_the_cli_switches_the_console_to_utf8():
    """Windows defaults to GBK here, so a correctly decoded Chinese subject
    prints as replacement characters -- which looks like a decoding bug in the
    mailbox layer and sends the investigation to the wrong place.
    """
    from invitesignup import cli

    calls = []
    import sys as _sys

    class FakeOut:
        encoding = "gbk"

        def reconfigure(self, **kw):
            calls.append(kw.get("encoding"))

    original = _sys.stdout
    _sys.stdout = FakeOut()
    try:
        cli._use_utf8_console()
    finally:
        _sys.stdout = original
    assert "utf-8" in calls


def test_an_operator_owned_mailbox_never_exports_credentials():
    """A temp inbox is ours to reopen; someone's real inbox is not.

    This was a real defect: a run with --email/--inbox-password wrote the QQ
    app password into output/accounts.jsonl in clear.  The rule now lives in
    Mailbox.export(), so a caller cannot forget it -- and this test targets
    that method directly rather than a code path that might not run.
    """
    Fake = _fake_provider("ownable")
    PROVIDERS["ownable"] = Fake
    try:
        ours = Mailbox()
        ours.create(order=["ownable"])
        assert ours.export().get("password"), "our own inbox should be restorable"

        theirs = Mailbox(operator_owned=True)
        theirs.create(order=["ownable"])
        assert theirs.export() == {}, "an operator inbox must not be exported"
    finally:
        PROVIDERS.pop("ownable", None)


def test_the_runner_marks_an_operator_inbox_as_theirs():
    """The flag is worthless if the caller forgets to pass it."""
    import inspect

    from invitesignup import runner

    source = inspect.getsource(runner._attempt)
    marker = "if inbox_password:"
    assert marker in source
    branch = source.split(marker, 1)[1]
    assert "operator_owned=True" in branch


def test_create_preferring_puts_the_preferred_provider_first():
    seen = []

    class Fake(Provider):
        def __init__(self, proxy=None, timeout=20):
            pass

        def create(self):
            seen.append(self.provider_name)
            return f"x@{self.provider_name}.test"

        def messages(self):
            return []

        def body(self, message_id):
            return ""

    for name in ("pref-a", "pref-b"):
        PROVIDERS[name] = type(name, (Fake,), {"provider_name": name})
    try:
        box = Mailbox()
        address = box.create_preferring(preferred=["pref-b", "pref-a"])
        assert address.endswith("@pref-b.test")
        assert seen == ["pref-b"]
    finally:
        for name in ("pref-a", "pref-b"):
            PROVIDERS.pop(name, None)


def test_a_crash_still_writes_a_ledger_row(monkeypatch, tmp_path):
    """An exception used to escape before the row was written.

    The ledger exists so "we never tried" and "we tried and it broke" are
    distinguishable; a crashed run that leaves no row defeats exactly that.
    """
    import json

    from invitesignup import runner
    from invitesignup.profile import SiteProfile

    def boom(*a, **k):
        raise RuntimeError("exploded before any stage ran")

    monkeypatch.setattr(runner, "_attempt", boom)

    profile = SiteProfile(site="example.test", base_url="https://example.test",
                          invite_code="C", invite_valid=True, captcha="none")
    result = runner.run_registration(
        profile, output_dir=str(tmp_path), force=True,
    )

    assert result.stage == "crashed"
    assert any("exploded" in e for e in result.errors)
    rows = [
        json.loads(line)
        for line in (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows, "a crashed run left no trace at all"
    assert rows[-1]["stage"] == "crashed"


def test_a_sign_in_redirect_is_read_as_already_registered():
    """The site says "this address exists" by redirecting to sign-in.

    Missing that produced a timeout on a password field that will never
    appear, which the failure table then called a retryable transport error --
    so a batch would retry forever against an account that already exists.
    """
    from invitesignup.runner import _landed_on_sign_in

    class Page:
        def __init__(self, url):
            self.url = url

        def locator(self, sel):
            class L:
                def count(self):
                    return 0
            return L()

    profile = type("P", (), {"sign_in_endpoint": "", "selectors": {}})()

    assert _landed_on_sign_in(Page("https://yila.ai/sign-in?email=x"), profile)
    assert _landed_on_sign_in(Page("https://site.test/login"), profile)
    assert not _landed_on_sign_in(Page("https://site.test/invite/CODE"), profile)


def test_already_registered_aborts_the_batch_instead_of_retrying():
    from invitesignup.runner import RunResult, _classify_outcome

    # The error text deliberately avoids every signature word, so this can
    # only pass via the stage check and not via text matching.
    result = RunResult(stage="already-registered", errors=["redirected"])
    _classify_outcome(result)
    assert result.failure_kind == "site_rejected"
    assert result.failure_action == "abort"


def test_mail_cx_choosing_an_address_is_the_whole_creation():
    """mail.cx has no signup step: the name *is* the mailbox.

    That is what makes it usable as a pool -- an address costs a string
    rather than an API call, so a run does not burn a quota unit to get one.
    """
    from invitesignup.mailbox import MailCxProvider

    p = MailCxProvider()
    address = p.create()
    assert address.endswith("@uqu.me")
    assert len(address.split("@")[0]) >= 2
    # No secret exists, so nothing should look like a stored credential.
    assert p.credentials() == {}


def test_mail_cx_treats_204_as_no_mail_yet_not_as_a_failure():
    """The endpoint long-polls for 25 s and answers 204 when nothing arrived.

    Reading 204 as an empty inbox would turn every wait into "no mail", which
    is indistinguishable from the site never sending anything.
    """
    import types

    import invitesignup.mailbox as mb

    class Resp:
        status_code = 204
        text = ""

        def json(self):
            # A real 204 has an empty body, so json() raises.  Returning {}
            # here would hide the very failure this test is about.
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    fake = types.SimpleNamespace(get=lambda *a, **k: Resp())
    original = mb.requests
    mb.requests = fake
    try:
        p = mb.MailCxProvider()
        p.create()
        assert p.messages() == []
    finally:
        mb.requests = original


def test_mail_cx_reads_a_body_and_follows_the_cursor():
    import types

    import invitesignup.mailbox as mb

    seen = {}

    class Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self._p = payload
            self.text = ""

        def json(self):
            return self._p

    def fake_get(url, **kw):
        seen.setdefault("urls", []).append(url)
        if "/email/" in url:
            return Resp(200, {"text_body": "code 123456", "html_body": ""})
        return Resp(200, {
            "emails": [{"id": "e1", "subject": "Verify",
                        "preview_text": "hello",
                        "created_at": "2026-11-19T00:00:00Z"}],
            "next_since": "999",
        })

    original = mb.requests
    mb.requests = types.SimpleNamespace(get=fake_get)
    try:
        p = mb.MailCxProvider()
        p.create()
        msgs = p.messages()
        assert [m.id for m in msgs] == ["e1"]
        assert p.body("e1") == "code 123456"
        # The second inbox call must carry the cursor, or every poll would
        # return the same mail and the "is it new?" guard would be bypassed.
        p.messages()
        assert any("since=999" in u for u in seen["urls"])
    finally:
        mb.requests = original


def test_a_preview_is_never_mistaken_for_the_body():
    """The truncation bug that broke a real signup.

    mail.cx returns ``preview_text`` (180 chars in the real case) alongside a
    794-char body.  Putting the preview into Message.body made poll() think
    the body had already been read, so it extracted a verification link cut
    off mid-token -- the flow reached "no session" with no error anywhere.
    """
    import types

    import invitesignup.mailbox as mb

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"emails": [{"id": "e1", "subject": "Verify",
                                "preview_text": "truncated https://x.test/verify?tok",
                                "created_at": "2026-11-19T00:00:00Z"}]}

    original = mb.requests
    mb.requests = types.SimpleNamespace(get=lambda *a, **k: Resp())
    try:
        p = mb.MailCxProvider()
        p.create()
        msgs = p.messages()
        assert msgs[0].body == "", "the preview must not masquerade as the body"
    finally:
        mb.requests = original


def test_a_duplicate_skip_is_not_reported_as_an_unknown_failure():
    """Nothing went wrong when the dedupe guard fires.

    It used to come out as `unknown` / `stop` with the note "unclassified
    failure; do not retry blindly", which reads as an incident and would make
    a batch driver stop on a perfectly healthy skip.
    """
    from invitesignup.runner import RunResult, _classify_outcome

    result = RunResult(stage="skipped-duplicate",
                       errors=["dedupe: already completed"])
    _classify_outcome(result)
    assert result.failure_kind == ""
    assert result.failure_action == ""
    assert "nothing was attempted" in result.notes


def test_a_query_token_never_reaches_the_shared_diagnosis_block():
    """The API trace is copied into a "send this to me" block.

    A verification request carries a live token in its query string, so the
    trace must keep the path (the useful part) and drop the credential.
    """
    from invitesignup.ledger import scrub_url

    url = ("https://yila.ai/api/auth/verify-email"
           "?token=eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6ImEifQ.sig&callbackURL=%2Fresearch")
    scrubbed = scrub_url(url)
    assert "verify-email" in scrubbed
    assert "eyJhbGciOiJIUzI1NiJ9" not in scrubbed
    assert "<redacted>" in scrubbed


def test_scrub_rules_pair_pattern_with_replacement():
    """The rules used to be two parallel tuples indexed by position, so
    inserting a new pattern shifted every replacement onto the wrong one."""
    from invitesignup.ledger import _VALUE_RULES

    for rule in _VALUE_RULES:
        assert len(rule) == 2, "each rule must carry its own replacement"
        pattern, replacement = rule
        assert hasattr(pattern, "sub") and isinstance(replacement, str)


def test_an_invite_gate_failure_is_config_not_a_site_refusal():
    """"no invite code was found" means "this may not be an invite site".

    It used to match the "invite code" needle and come out as
    site_rejected/abort -- "the site refused you, stop the batch" -- which is
    both the wrong cause and the wrong next action.
    """
    from invitesignup.failures import Action, Kind, classify

    gate = classify("no invite code was found on this page")
    assert gate.kind is Kind.CONFIG
    assert gate.action is Action.STOP

    real = classify("invite code is invalid")
    assert real.kind is Kind.SITE_REJECTED
    assert real.action is Action.ABORT_BATCH


def test_the_mailbox_rejection_vocabulary_has_one_home():
    """Two copies had already drifted apart; runner.py now re-exports."""
    from invitesignup import failures, runner

    assert runner.MAILBOX_REJECT_HINTS is failures.MAILBOX_REJECT_HINTS


def test_the_ledger_records_the_classification_it_promises():
    """_classify_outcome says the decision is visible in the ledger.

    It was not: only prose was written, so a batch driver had to re-derive
    the classification from text and could disagree with what actually
    happened.
    """
    import inspect

    from invitesignup import runner

    source = inspect.getsource(runner._persist)
    assert '"failure_kind"' in source
    assert '"failure_action"' in source


def test_generated_passwords_satisfy_the_complexity_rules():
    """Behavioural check: whatever the generator uses, every password it
    emits must carry one of each required class and be long enough."""
    import string

    from invitesignup.runner import make_password

    seen = set()
    for _ in range(200):
        pw = make_password()
        assert len(pw) == 16
        assert any(c.islower() for c in pw)
        assert any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert any(c in "!@#$%&*_-=+" for c in pw)
        assert all(c in string.ascii_letters + string.digits + "!@#$%&*_-=+" for c in pw)
        seen.add(pw)
    # A generator that returned a constant would pass the checks above.
    assert len(seen) == 200, "passwords are not varying"


def test_the_version_matches_pyproject():
    import re
    from pathlib import Path

    import invitesignup

    text = (Path(invitesignup.__file__).resolve().parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    declared = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
    assert invitesignup.__version__ == declared


def test_an_old_profile_with_unknown_nested_keys_still_loads():
    """The outer dataclass filtered unknown keys and the nested ones did not,
    so a profile written by an older schema raised TypeError at load time."""
    from invitesignup.profile import SiteProfile

    data = SiteProfile(site="example.test", base_url="https://example.test").to_dict()
    data["attribution"]["expires"] = "2020-01-01"
    data["verification"]["legacy_field"] = 1
    restored = SiteProfile.from_dict(data)
    assert restored.site == "example.test"


def test_guerrillamail_paging_uses_the_last_id_not_a_count():
    """`seq` is the last mail_id seen, not a running total.

    Adding the batch size walks past gaps in the id sequence and skips
    messages, which reads as "no verification link arrived".
    """
    import inspect

    from invitesignup import mailbox

    source = inspect.getsource(mailbox.GuerrillaMailProvider.messages)
    assert "seq += len(batch)" not in source
    assert 'last_id = batch[-1].get("mail_id")' in source


def test_an_otp_field_is_found_without_a_label():
    """`infer_steps` asks infer_selectors for an "otp" key.

    That key was only produced when a <label> pointed at the field, so a code
    input labelled purely by its attributes made infer_steps report an otp
    step while infer_selectors returned no otp selector -- the two disagreed
    about the same page.
    """
    from invitesignup.probe import DomInfo, infer_selectors, infer_steps

    by_id = DomInfo(inputs=[{"type": "email", "id": "email"},
                            {"type": "text", "id": "otp-code"}], url="https://x.test/v")
    assert infer_selectors(by_id)["otp"] == "#otp-code"
    assert "otp" in infer_steps(by_id)

    by_name = DomInfo(inputs=[{"type": "email", "id": "email"},
                              {"type": "text", "name": "code"}], url="https://x.test/v")
    assert infer_selectors(by_name)["otp"] == 'input[name="code"]'
    assert "otp" in infer_steps(by_name)


def test_a_page_without_a_code_field_gets_no_otp_selector():
    from invitesignup.probe import DomInfo, infer_selectors, infer_steps

    plain = DomInfo(inputs=[{"type": "email", "id": "email"}], url="https://x.test/v")
    assert "otp" not in infer_selectors(plain)
    assert "otp" not in infer_steps(plain)


def test_a_code_site_with_no_otp_selector_is_blocked_not_attempted():
    """The verdict said automatable while the runner had nowhere to type.

    The runner then skipped the entry step silently and reported "no session
    after the flow" -- blaming the site for a gap in the profile.
    """
    from invitesignup.profile import SiteProfile
    from invitesignup.verdict import BLOCKED_FACTS, decide

    p = SiteProfile(site="x.test", base_url="https://x.test",
                    invite_code="C", invite_valid=True, captcha="none")
    p.verification.mode = "code"
    p.verification.pattern = r"\b\d{6}\b"
    p.selectors = {"email": "input[type=email]", "submit": "button[type=submit]"}

    v = decide(p)
    assert v.state == BLOCKED_FACTS
    assert any("otp" in b for b in v.blockers)
    assert not v.allowed

    p.selectors["otp"] = "input[name=code]"
    assert decide(p).allowed


def test_a_code_profile_without_an_otp_field_stops_before_the_browser(monkeypatch):
    """The real guarantee: the run stops with a clear reason and no side effect.

    The verdict blocks this profile at the top of _attempt, so the check
    inside the code branch is defence in depth rather than the thing that
    fires.  What matters, and what this asserts, is that the run never reaches
    the browser and the error names the missing field instead of blaming the
    site with "no session after the flow".
    """
    import pathlib
    import sys as _sys
    import tempfile
    import types as _types

    from invitesignup import runner
    from invitesignup.profile import SiteProfile

    touched = []

    class StubPage:
        url = "https://x.test/verify"

        def wait_for_timeout(self, ms):
            pass

        def screenshot(self, path=None):
            pass

        def locator(self, *a, **k):
            touched.append(a)
            raise AssertionError("locator touched")

    class StubBrowser:
        def new_context(self, **kw):
            touched.append("context")
            return _types.SimpleNamespace(new_page=lambda: StubPage())

        def close(self):
            pass

    class StubPW:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_api = _types.ModuleType("playwright.sync_api")
    fake_api.sync_playwright = lambda: StubPW()
    monkeypatch.setitem(_sys.modules, "playwright.sync_api", fake_api)
    monkeypatch.setattr(runner, "launch_browser", lambda *a, **k: StubBrowser())

    profile = SiteProfile(site="x.test", base_url="https://x.test",
                          invite_code="C", invite_valid=True, captcha="none")
    profile.verification.mode = "code"
    profile.verification.pattern = r"\b\d{6}\b"
    profile.selectors = {"email": "input[type=email]", "submit": "button"}

    with tempfile.TemporaryDirectory() as tmp:
        result = runner._attempt(
            profile, out=pathlib.Path(tmp), key="k", book=None, email=None,
            password=None, inbox_password=None, allow_multi=False, force=True,
            headless=True, tos_forbids_automation=False, poll_timeout=5,
        )

    assert result.ok is False
    assert any("otp" in e.lower() for e in result.errors), result.errors
    assert not any("no session" in e for e in result.errors), result.errors
    # Nothing was launched: the decision happened before any side effect.
    assert touched == [], touched


def test_verification_rejects_a_session_for_a_different_account():
    """A session proves *someone* signed in, not that it was this account.

    Without this check a reused password, a shared mailbox, or a site that
    falls back to another account would all be reported as verified -- which
    defeats the one guarantee this module exists for.
    """
    from invitesignup.verify import _identity_matches

    ok, detail = _identity_matches({"email": "target@x.test"}, "target@x.test")
    assert ok, detail

    ok, detail = _identity_matches({"email": "Target@X.Test"}, "target@x.test")
    assert ok, "comparison must be case-insensitive"

    ok, detail = _identity_matches({"email": "other@x.test"}, "target@x.test")
    assert not ok
    assert "other@x.test" in detail


def test_verification_refuses_to_guess_identity_it_cannot_see():
    """No email in the session means it cannot be compared -- say so rather
    than silently counting as a pass."""
    from invitesignup.verify import _identity_matches

    ok, detail = _identity_matches({"id": "123"}, "target@x.test")
    assert not ok
    assert "cannot be matched" in detail

    ok, detail = _identity_matches(None, "target@x.test")
    assert not ok


def test_a_mismatched_account_makes_the_report_unverified(monkeypatch):
    """End to end through verify_account with HTTP stubbed out."""
    from invitesignup import verify
    from invitesignup.profile import SiteProfile

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"user": {"email": "someone-else@x.test", "emailVerified": True}}

    class Session:
        headers = {}

        def post(self, *a, **k):
            return Resp()

        def get(self, *a, **k):
            return Resp()

    monkeypatch.setattr(verify.requests, "Session", lambda: Session())

    profile = SiteProfile(site="x.test", base_url="https://x.test")
    report = verify.verify_account(
        profile, "target@x.test", "pw",
        sign_in_endpoint="/api/auth/sign-in/email",
        session_endpoint="/api/auth/get-session",
    )

    assert report.verified is False, "a session for another account must not verify"
    assert report.reason == "signed in as a different account"
    assert any(c["name"] == "identity" and not c["ok"] for c in report.checks)
