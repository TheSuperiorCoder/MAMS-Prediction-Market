"""The event log and server log, Resend/SMTP email sending, and Turnstile's place in the risk score."""
import io
import json
import logging
import os
import smtplib
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_market import _fresh_db, _post, _user, csrf, signup, appmod  # noqa: E402
from test_security import client_at, fp, register, risk, seed_owner, uid  # noqa: E402
import eventlog  # noqa: E402
import security  # noqa: E402


@pytest.fixture(autouse=True)
def no_configured_owner(monkeypatch):
    monkeypatch.setattr(appmod, "OWNER_USERNAME", "")


@pytest.fixture(autouse=True)
def restore_db_path():
    old = appmod.DB_PATH
    yield
    appmod.DB_PATH = old


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(appmod, "send_login_code_email", lambda to, code: out.append((to, code)) or "stub")
    return out


def events(db, kind=None):
    sql, args = "SELECT * FROM security_events", ()
    if kind:
        sql, args = sql + " WHERE kind = ?", (kind,)
    return db.execute(sql + " ORDER BY id", args).fetchall()


def login(c, name, password):
    return c.post("/login", data={"csrf_token": csrf(c.get("/login")), "username": name, "password": password},
                  follow_redirects=True)


# ---------------------------------------------------------------- the event log

def test_signup_login_and_logout_are_logged(sent):
    db = _fresh_db()
    seed_owner(db)
    c = client_at("34.120.1.1")
    register(c, "newcomer")
    ev = events(db, "signup_ok")[0]
    assert ev["username"] == "newcomer" and ev["ip"] == "34.120.1.1" and ev["user_id"] and ev["request_id"]
    assert ev["device_id"] and "Chrome" in ev["ua"] and ev["path"] == "/register"
    assert len(events(db, "email_code_sent")) == 1
    db.execute("UPDATE users SET password_hash = ? WHERE username = 'newcomer'", (appmod.generate_password_hash("pw-1234"),))
    c2 = client_at("34.120.1.2")
    login(c2, "newcomer", "pw-1234")
    assert events(db, "login_ok")[0]["ip"] == "34.120.1.2"
    c2.post("/logout", data={"csrf_token": csrf(c2.get("/portfolio", follow_redirects=True))})
    assert len(events(db, "logout")) == 1


def test_failed_logins_lockouts_and_unknown_names_are_logged(sent):
    db = _fresh_db()
    signup(client_at("34.120.1.1"), "victim")
    c = client_at("34.120.1.9")
    login(c, "victim", "wrong")
    login(c, "nobodyhere", "wrong")
    assert events(db, "login_failed")[0]["username"] == "victim"
    assert events(db, "login_unknown_user")[0]["username"] == "nobodyhere"
    for _ in range(11):
        login(c, "victim", "wrong")
    assert len(events(db, "login_locked")) >= 1
    assert all(e["severity"] == "warn" for e in events(db, "login_failed"))


def test_a_password_pasted_into_the_username_box_is_never_logged():
    db = _fresh_db()
    c = client_at("34.120.1.9")
    login(c, "my secret password!! 123", "x")
    login(c, "hunter2hunter2hunter2hunter2hunter2", "x")           # looks like a username but is too long
    rows = events(db, "login_unknown_user")
    assert [r["username"] for r in rows] == ["(not a username)", "(not a username)"]
    blob = json.dumps([dict(r) for r in events(db)])
    assert "secret password" not in blob and "hunter2" not in blob


def test_no_passwords_codes_or_tokens_end_up_in_the_logs(sent, caplog):
    db = _fresh_db()
    seed_owner(db)
    c = client_at("34.120.1.1")
    with caplog.at_level(logging.DEBUG):
        register(c, "careful", password="Sup3r-secret-pw")
        code = sent[-1][1]
        c.post("/account/email", data={"csrf_token": csrf(c.get("/account/email")), "action": "verify", "code": "000000"})
        c.post("/account/email", data={"csrf_token": csrf(c.get("/account/email")), "action": "verify", "code": code})
    blob = json.dumps([dict(r) for r in events(db)]) + caplog.text
    assert "Sup3r-secret-pw" not in blob and code not in blob
    assert len(events(db, "email_code_wrong")) == 1 and len(events(db, "email_verified")) == 1


def test_email_failures_refusals_and_waits_are_logged(monkeypatch):
    db = _fresh_db()
    seed_owner(db)
    c = client_at("34.120.1.1")

    def boom(to, code):
        raise appmod.EmailError("SMTP login rejected: check the API key / password", permanent=True)
    monkeypatch.setattr(appmod, "send_login_code_email", boom)
    register(c, "unlucky")
    e = events(db, "email_send_failed")[0]
    assert e["severity"] == "alert" and "API key" in e["detail"] and "unlucky@example.org" in e["detail"]
    monkeypatch.setattr(appmod, "send_login_code_email", lambda to, code: "ok")
    post = lambda **d: c.post("/account/email", data={"csrf_token": csrf(c.get("/account/email")), **d})
    post(action="send", email="mailinator-user@mailinator.com")
    assert "mailinator" in events(db, "email_refused")[0]["detail"]
    appmod._hits.clear()                                          # forget the sign-up's own code request
    post(action="send", email="fine@example.org")
    post(action="send", email="fine@example.org")
    assert len(events(db, "email_code_wait")) == 1


def test_csrf_failures_and_admin_denials_are_logged():
    db = _fresh_db()
    seed_owner(db)
    bob = client_at("34.120.1.5")
    signup(bob, "bob")
    assert bob.post("/bonus", data={"csrf_token": "forged"}).status_code == 400
    assert events(db, "csrf_failed")[0]["detail"] == "POST bonus"
    assert bob.get("/admin").status_code == 403
    e = events(db, "admin_denied")[0]
    assert e["username"] == "bob" and e["path"] == "/admin"
    assert client_at("34.120.1.6").get("/admin/events").status_code in (302, 403)


def test_hold_and_risk_changes_are_logged(sent):
    db = _fresh_db()
    admin = client_at("34.120.1.1")
    signup(admin, "boss")                                       # the owner
    c = client_at("34.120.1.7")
    register(c, "mainacct", fp_json=fp(token="a" * 32))
    register(c, "mainacct2", fp_json=fp(token="a" * 32))
    held = events(db, "risk_held")[0]
    assert held["severity"] == "alert" and held["username"] == "mainacct2" and "risk" in held["detail"]
    db.execute("UPDATE users SET email_verified = 1, email = 'm2@example.org' WHERE username = 'mainacct2'")
    c.post("/bonus", data={"csrf_token": csrf(c.get("/account/email", follow_redirects=True))})
    assert events(db, "hold_blocked")[0]["username"] == "mainacct2"
    _post(admin, f"/admin/users/{uid(db, 'mainacct2')}/risk", action="approve")
    assert any("Approved an account by boss" in e["detail"] and e["username"] == "mainacct2"
               for e in events(db, "admin_action"))


def test_every_admin_log_entry_is_mirrored_into_the_event_log():
    db = _fresh_db()
    owner = client_at("34.120.1.1")
    signup(owner, "boss")
    bobid = _user(db, "bob")
    _post(owner, f"/admin/users/{bobid}/trading")
    e = events(db, "admin_action")[0]
    assert "Banned a user from trading by boss" in e["detail"] and e["username"] == "bob" and e["user_id"] == bobid
    _post(owner, "/admin/announcement", text="Hello")
    assert any("announcement" in x["detail"].lower() for x in events(db, "admin_action"))


def test_an_event_written_inside_a_transaction_is_queued_and_survives_a_rollback():
    db = _fresh_db()
    with appmod.app.test_request_context("/"):
        appmod.g.db = appmod.get_db()
        d = appmod.g.db
        d.execute("BEGIN IMMEDIATE")
        appmod.log_event("login_ok", username="queued", detail="inside a transaction")
        assert d.execute("SELECT COUNT(*) FROM security_events").fetchone()[0] == 0      # not written yet: no lock fight
        d.execute("ROLLBACK")
        appmod.flush_events(d)
        assert d.execute("SELECT username FROM security_events").fetchone()[0] == "queued"


def test_log_event_never_raises(monkeypatch):
    _fresh_db()
    monkeypatch.setattr(appmod, "get_db", lambda: 1 / 0)
    with appmod.app.test_request_context("/"):
        appmod.log_event("login_ok", username="x")              # must not raise
    appmod.log_event("not_a_known_kind")                         # outside any app context: only the server log
    with appmod.app.test_request_context("/"):
        appmod.log_event("login_ok", detail="x" * 100000, username="y" * 500)


def test_unhandled_errors_get_a_reference_that_matches_the_event(monkeypatch):
    db = _fresh_db()
    monkeypatch.setitem(appmod.app.config, "TESTING", False)
    monkeypatch.setitem(appmod.app.config, "PROPAGATE_EXCEPTIONS", False)
    monkeypatch.setitem(appmod.app.view_functions, "leaderboard", lambda: 1 / 0)
    r = client_at("34.120.1.1").get("/leaderboard")
    assert r.status_code == 500
    ref = r.headers["X-Request-ID"]
    assert f"<code>{ref}</code>" in r.get_data(as_text=True)
    e = events(db, "server_error")[0]
    assert e["request_id"] == ref and "ZeroDivisionError" in e["detail"] and e["severity"] == "alert"


def test_slow_requests_are_logged(monkeypatch):
    db = _fresh_db()
    monkeypatch.setattr(appmod, "SLOW_REQUEST", -1)
    client_at("34.120.1.1").get("/leaderboard")
    assert "GET /leaderboard took" in events(db, "slow_request")[0]["detail"]


def test_old_events_are_pruned():
    db = _fresh_db()
    old = time.time() - (security.SIGNAL_RETENTION_DAYS + 1) * 86400
    db.execute("INSERT INTO security_events (created_at, kind) VALUES (?, 'login_ok')", (old,))
    db.execute("INSERT INTO security_events (created_at, kind) VALUES (?, 'login_ok')", (time.time(),))
    security.prune(db)
    assert db.execute("SELECT COUNT(*) FROM security_events").fetchone()[0] == 1


def test_every_catalog_entry_has_a_valid_severity():
    assert all(sev in eventlog.SEVERITIES and label for sev, label in eventlog.EVENTS.values())


# ---------------------------------------------------------------- the Events page

def test_events_page_filters_searches_and_paginates(sent):
    db = _fresh_db()
    owner = client_at("34.120.1.1")
    signup(owner, "boss")
    c = client_at("34.120.1.9")
    login(c, "ghost", "x")
    register(client_at("34.120.1.2"), "newbie")
    page = owner.get("/admin/events").get_data(as_text=True)
    assert "Signed up" in page and "unknown username" in page and "alert" in page.lower()
    assert "Log-in for an unknown username" in owner.get("/admin/events?kind=login_unknown_user").get_data(as_text=True)
    assert "newbie" not in owner.get("/admin/events?kind=login_unknown_user").get_data(as_text=True)
    assert "ghost" in owner.get("/admin/events?q=ghost").get_data(as_text=True)
    assert "34.120.1.2" in owner.get("/admin/events?q=34.120.1").get_data(as_text=True)
    assert "No events match" in owner.get("/admin/events?q=zzzzzz").get_data(as_text=True)
    assert "No events match" in owner.get("/admin/events?severity=alert").get_data(as_text=True)
    for i in range(130):
        db.execute("INSERT INTO security_events (created_at, kind, detail) VALUES (?, 'login_ok', ?)", (time.time(), f"row{i}"))
    p1 = owner.get("/admin/events").get_data(as_text=True)
    p3 = owner.get("/admin/events?page=3").get_data(as_text=True)
    assert "Older →" in p1 and "Page 3 of" in p3 and owner.get("/admin/events?page=bad").status_code == 200


def test_events_about_admins_are_only_shown_to_the_owner():
    db = _fresh_db()
    owner = client_at("34.120.1.1")
    signup(owner, "boss")
    deputy = client_at("34.120.1.3")
    signup(deputy, "deputy")
    db.execute("UPDATE users SET is_admin = 1 WHERE username = 'deputy'")
    db.execute("UPDATE users SET password_hash = ? WHERE username = 'boss'", (appmod.generate_password_hash("pw-1234"),))
    login(client_at("34.120.1.77"), "boss", "pw-1234")                # an event about the owner
    assert "34.120.1.77" in owner.get("/admin/events").get_data(as_text=True)
    assert "34.120.1.77" not in deputy.get("/admin/events").get_data(as_text=True)


def test_user_page_shows_that_users_events(sent):
    db = _fresh_db()
    owner = client_at("34.120.1.1")
    signup(owner, "boss")
    register(client_at("34.120.1.2"), "target")
    page = owner.get(f"/admin/users/{uid(db, 'target')}").get_data(as_text=True)
    assert "Event log" in page and "Signed up" in page and "Confirmation code emailed" in page


# ---------------------------------------------------------------- server log file

def test_logging_is_set_up_once_and_writes_a_rotating_file(tmp_path):
    lg = logging.getLogger("test-eventlog")
    eventlog.configure(lg, str(tmp_path / "logs"), "INFO")
    eventlog.configure(lg, str(tmp_path / "logs"), "INFO")
    assert len(lg.handlers) == 2                                # stderr + file, not stacked on the second call
    lg.warning("hello from the test")
    for h in lg.handlers:
        h.flush()
    text = (tmp_path / "logs" / "mams.log").read_text()
    assert "hello from the test" in text and "[-]" in text
    if os.name != "nt":
        assert os.stat(tmp_path / "logs" / "mams.log").st_mode & 0o777 == 0o600
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in lg.handlers)


def test_unwritable_log_directory_falls_back_to_stderr(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    lg = logging.getLogger("test-eventlog-2")
    eventlog.configure(lg, str(blocker / "logs"))               # can't create a directory under a file
    assert len(lg.handlers) == 1


def test_log_lines_carry_the_request_id(sent, caplog):
    _fresh_db()
    with caplog.at_level(logging.INFO):
        r = client_at("34.120.1.1").get("/login")
        c = client_at("34.120.1.1")
        login(c, "ghost", "x")
    assert any(getattr(rec, "request_id", "-") not in ("-", "") for rec in caplog.records if "login_unknown_user" in rec.getMessage()) \
        or "event=login_unknown_user" in caplog.text


# ---------------------------------------------------------------- email: Resend over SMTP

class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what happened."""
    log = []
    fail = []                        # exceptions to raise on successive send_message calls

    def __init__(self, host, port, timeout=None, context=None):
        self.kind = "ssl" if context is not None else "plain"
        FakeSMTP.log.append(("connect", self.kind, host, port, timeout))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        FakeSMTP.log.append(("ehlo",))

    def starttls(self, context=None):
        FakeSMTP.log.append(("starttls",))

    def login(self, user, password):
        FakeSMTP.log.append(("login", user, password))

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise FakeSMTP.fail.pop(0)
        FakeSMTP.log.append(("send", msg["To"], msg["From"], msg["Subject"], msg.get_body(("plain",)).get_content(),
                             msg.get_body(("html",)).get_content() if msg.get_body(("html",)) else None))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.log, FakeSMTP.fail = [], []
    monkeypatch.setitem(appmod.app.config, "TESTING", False)
    monkeypatch.setattr(appmod.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(appmod.smtplib, "SMTP_SSL", lambda h, p, timeout=None, context=None: FakeSMTP(h, p, timeout, context))
    for k in list(os.environ):
        if k.startswith("MAMS_SMTP") or k in ("MAMS_RESEND_API_KEY", "MAMS_EMAIL_BACKEND"):
            monkeypatch.delenv(k)
    return FakeSMTP


def test_resend_backend_uses_resends_smtp_service_with_the_api_key(smtp, monkeypatch):
    monkeypatch.setenv("MAMS_EMAIL_BACKEND", "resend")
    monkeypatch.setenv("MAMS_RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("MAMS_SMTP_FROM", "MAMS Markets <noreply@mail.example.org>")
    with appmod.app.app_context():
        appmod.send_login_code_email("kid@example.org", "123456")
    log = smtp.log
    assert log[0] == ("connect", "plain", "smtp.resend.com", 587, 15.0)
    assert ("starttls",) in log and ("login", "resend", "re_test_key") in log
    sent = [x for x in log if x[0] == "send"][0]
    assert sent[1] == "kid@example.org" and "noreply@mail.example.org" in sent[2]
    assert "123456" in sent[4] and "123456" in sent[5] and "<html" not in sent[4]


def test_resend_over_implicit_tls_port_465(smtp, monkeypatch):
    monkeypatch.setenv("MAMS_EMAIL_BACKEND", "resend")
    monkeypatch.setenv("MAMS_RESEND_API_KEY", "re_k")
    monkeypatch.setenv("MAMS_SMTP_FROM", "noreply@mail.example.org")
    monkeypatch.setenv("MAMS_SMTP_PORT", "465")
    with appmod.app.app_context():
        appmod.send_login_code_email("kid@example.org", "654321")
    assert smtp.log[0][:4] == ("connect", "ssl", "smtp.resend.com", 465)
    assert ("starttls",) not in smtp.log                         # already encrypted from the first byte


def test_resend_without_a_key_or_sender_fails_clearly(smtp, monkeypatch):
    monkeypatch.setenv("MAMS_EMAIL_BACKEND", "resend")
    monkeypatch.setenv("MAMS_SMTP_FROM", "noreply@mail.example.org")
    with appmod.app.app_context(), pytest.raises(appmod.EmailError, match="MAMS_RESEND_API_KEY"):
        appmod.send_email("a@b.org", "s", "t")
    monkeypatch.setenv("MAMS_RESEND_API_KEY", "re_k")
    monkeypatch.delenv("MAMS_SMTP_FROM")
    with appmod.app.app_context(), pytest.raises(appmod.EmailError, match="MAMS_SMTP_FROM"):
        appmod.send_email("a@b.org", "s", "t")
    assert not smtp.log                                          # never even connected


def test_plain_smtp_still_works_as_before(smtp, monkeypatch):
    monkeypatch.setenv("MAMS_SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("MAMS_SMTP_USERNAME", "me@example.org")
    monkeypatch.setenv("MAMS_SMTP_PASSWORD", "pw")
    with appmod.app.app_context():
        appmod.send_login_code_email("kid@example.org", "111111")
    assert smtp.log[0][:4] == ("connect", "plain", "smtp.example.org", 587)
    assert ("login", "me@example.org", "pw") in smtp.log
    assert [x for x in smtp.log if x[0] == "send"][0][2] == "me@example.org"


def test_a_dropped_connection_is_retried_once(smtp, monkeypatch):
    monkeypatch.setenv("MAMS_EMAIL_BACKEND", "resend")
    monkeypatch.setenv("MAMS_RESEND_API_KEY", "re_k")
    monkeypatch.setenv("MAMS_SMTP_FROM", "noreply@mail.example.org")
    smtp.fail = [smtplib.SMTPServerDisconnected("gone")]
    with appmod.app.app_context():
        how = appmod.send_login_code_email("kid@example.org", "222222")
    assert "2nd try" in how and len([x for x in smtp.log if x[0] == "connect"]) == 2


@pytest.mark.parametrize("exc, text, permanent", [
    (smtplib.SMTPAuthenticationError(535, b"bad"), "API key", True),
    (smtplib.SMTPSenderRefused(550, b"no", "x@y"), "verify the From domain", True),
    (smtplib.SMTPRecipientsRefused({"a@b": (550, b"no")}), "recipient address refused", True),
    (smtplib.SMTPResponseException(451, "try later"), "451", False),
    (socket.timeout("slow"), "timed out", False),
    (socket.gaierror("no host"), "MAMS_SMTP_HOST", True),
    (ConnectionResetError("reset"), "dropped", False),
])
def test_smtp_errors_become_plain_reasons_without_secrets(smtp, monkeypatch, exc, text, permanent):
    monkeypatch.setenv("MAMS_EMAIL_BACKEND", "resend")
    monkeypatch.setenv("MAMS_RESEND_API_KEY", "re_SECRETKEY")
    monkeypatch.setenv("MAMS_SMTP_FROM", "noreply@mail.example.org")
    smtp.fail = [exc, exc]
    with appmod.app.app_context(), pytest.raises(appmod.EmailError) as info:
        appmod.send_email("kid@example.org", "s", "t")
    assert text in info.value.reason and info.value.permanent is permanent
    assert "SECRETKEY" not in info.value.reason
    attempts = len([x for x in smtp.log if x[0] == "connect"])
    assert attempts == (1 if permanent else 2)                  # permanent failures aren't retried


def test_a_failed_send_tells_the_user_something_generic_and_logs_the_reason(smtp, monkeypatch):
    db = _fresh_db()
    seed_owner(db)
    monkeypatch.setenv("MAMS_EMAIL_BACKEND", "resend")
    monkeypatch.setenv("MAMS_RESEND_API_KEY", "re_SECRETKEY")
    monkeypatch.setenv("MAMS_SMTP_FROM", "noreply@mail.example.org")
    smtp.fail = [smtplib.SMTPAuthenticationError(535, b"bad")]
    page = register(client_at("34.120.1.1"), "kid").get_data(as_text=True)
    assert "couldn&#39;t send an email" in page and "SECRETKEY" not in page and "535" not in page
    e = events(db, "email_send_failed")[0]
    assert "API key" in e["detail"] and "SECRETKEY" not in e["detail"]


def test_console_backend_never_connects_to_anything(smtp, monkeypatch, caplog):
    monkeypatch.setenv("MAMS_EMAIL_BACKEND", "console")
    with appmod.app.app_context(), caplog.at_level(logging.WARNING):
        assert appmod.send_email("a@b.org", "Hello", "body text") == "console"
    assert not smtp.log and "body text" in caplog.text


# ---------------------------------------------------------------- Turnstile and the risk score

def _turnstile(monkeypatch, behaviour):
    monkeypatch.setattr(appmod, "TURNSTILE_SITE_KEY", "site")
    monkeypatch.setattr(appmod, "TURNSTILE_SECRET", "secret")

    def fake(req, timeout):
        if behaviour == "down":
            raise OSError("unreachable")
        good = b"response=good" in req.data
        body = {"success": good} if good else {"success": False, "error-codes": ["invalid-input-response"]}
        return io.BytesIO(json.dumps(body).encode())
    monkeypatch.setattr(appmod.urllib.request, "urlopen", fake)


def _signup_with_captcha(db, name, token, ip="34.120.1.1"):
    c = client_at(ip)
    return c, c.post("/register", data={"csrf_token": csrf(c.get("/register")), "username": name,
                                        "email": f"{name}@example.org", "password": "secret1", "confirm": "secret1",
                                        "fp": fp(canvas=name), "cf-turnstile-response": token}, follow_redirects=True)


def test_turnstile_check_outcomes(monkeypatch):
    _fresh_db()
    with appmod.app.app_context():
        assert appmod.turnstile_check("", "1.2.3.4") == "off"                   # not configured
        _turnstile(monkeypatch, "up")
        assert appmod.turnstile_check("", "1.2.3.4") == "missing"
        assert appmod.turnstile_check("bad", "1.2.3.4") == "failed"
        assert appmod.turnstile_check("good", "1.2.3.4") == "ok"
        _turnstile(monkeypatch, "down")
        assert appmod.turnstile_check("good", "1.2.3.4") == "unreachable"
        assert not appmod.turnstile_passed("good", "1.2.3.4")                  # outage: rejected
        _turnstile(monkeypatch, "up")
        assert appmod.turnstile_passed("good", "1.2.3.4") and not appmod.turnstile_passed("", "1.2.3.4")
        assert not appmod.turnstile_passed("bad", "1.2.3.4")


def test_failed_human_check_is_logged_and_makes_no_account(monkeypatch, sent):
    db = _fresh_db()
    seed_owner(db)
    _turnstile(monkeypatch, "up")
    _, page = _signup_with_captcha(db, "robot", "bad")
    assert "verify you" in page.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) FROM users WHERE username = 'robot'").fetchone()[0] == 0
    e = events(db, "signup_captcha_failed")[0]
    assert e["detail"] == "failed" and e["username"] == "robot"
    _signup_with_captcha(db, "robot2", "")
    assert events(db, "signup_captcha_failed")[1]["detail"] == "missing"


def test_passing_the_human_check_is_recorded_and_costs_no_risk(monkeypatch, sent):
    db = _fresh_db()
    seed_owner(db)
    _turnstile(monkeypatch, "up")
    _signup_with_captcha(db, "human", "good")
    assert "human check: ok" in events(db, "signup_ok")[0]["detail"]
    assert not any(k == "captcha" for _, k, _ in risk(db, "human")["reasons"])
    assert not events(db, "captcha_unreachable")


def test_cloudflare_outage_rejects_signup_and_logs_it(monkeypatch, sent):
    db = _fresh_db()
    owner = client_at("34.120.1.1")
    signup(owner, "boss")
    _turnstile(monkeypatch, "down")
    _, page = _signup_with_captcha(db, "unverified", "tok", ip="34.120.1.2")
    assert "couldn" in page.get_data(as_text=True) and "Try again in a minute" in page.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) FROM users WHERE username = 'unverified'").fetchone()[0] == 0
    e = events(db, "captcha_unreachable")[0]
    assert e["severity"] == "warn" and "rejected" in e["detail"]
    assert not sent


def test_register_page_still_loads_both_scripts_when_turnstile_is_on(monkeypatch):
    _fresh_db()
    monkeypatch.setattr(appmod, "TURNSTILE_SITE_KEY", "site")
    monkeypatch.setattr(appmod, "TURNSTILE_SECRET", "secret")
    html = client_at("34.120.1.1").get("/register").get_data(as_text=True)
    assert "fp.js" in html and "turnstile/v0/api.js" in html and 'data-sitekey="site"' in html and "data-fp" in html
