import os
import re
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MAMS_DB"] = os.path.join(tempfile.mkdtemp(), "test_profile.db")

import app as appmod  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    old_db = appmod.DB_PATH
    appmod.DB_PATH = str(tmp_path / "profile.db")
    monkeypatch.setattr(appmod, "OWNER_USERNAME", "")
    appmod.init_db()
    appmod.app.config["TESTING"] = True
    try:
        yield appmod.app.test_client()
    finally:
        appmod.DB_PATH = old_db


def csrf(resp):
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', resp.get_data(as_text=True)).group(1)


def make_user(name, password="secret1"):
    db = appmod.connect()
    cur = db.execute(
        "INSERT INTO users (username, password_hash, balance, is_admin, created_at, email, email_verified) "
        "VALUES (?, ?, ?, 0, ?, ?, 1)",
        (name, appmod.generate_password_hash(password), 1000.0, appmod.now_str(), f"{name}@example.com"),
    )
    uid = cur.lastrowid
    db.commit()
    db.close()
    return uid


def login_as(client, uid):
    with client.session_transaction() as sess:
        sess.permanent = True
        sess["uid"] = uid


def test_profile_menu_and_name_email_password_changes(client):
    alice = make_user("alice")
    make_user("bob")
    login_as(client, alice)

    # Menu shows Profile instead of Change email.
    home = client.get("/").get_data(as_text=True)
    assert "Profile</a>" in home
    assert "Change email</a>" not in home

    # /profile renders Name, Email, and Password sections.
    rp = client.get("/profile")
    assert rp.status_code == 200
    html = rp.get_data(as_text=True)
    assert "<h1>Profile</h1>" in html
    assert "Save name" in html
    assert "Send verification code" in html
    assert "Change password" in html
    tok = csrf(rp)

    # Rejects taken name.
    r_dup = client.post("/account/username", data={"csrf_token": tok, "username": "Bob"}, follow_redirects=True)
    assert "already taken" in r_dup.get_data(as_text=True)

    # Rejects invalid name.
    r_bad = client.post("/account/username", data={"csrf_token": tok, "username": "a"}, follow_redirects=True)
    assert "3–20 characters" in r_bad.get_data(as_text=True)

    # Renames cleanly.
    r_ok = client.post("/account/username", data={"csrf_token": tok, "username": "alice_new"}, follow_redirects=True)
    assert "Your name is now alice_new." in r_ok.get_data(as_text=True)
    db = appmod.connect()
    assert db.execute("SELECT username FROM users WHERE id = ?", (alice,)).fetchone()["username"] == "alice_new"
    db.close()

    # Changes password from Profile and redirects back to Profile.
    r_pw = client.post("/account/password", data={
        "csrf_token": tok, "current": "secret1", "new": "newpass123", "confirm": "newpass123",
        "next": "/account/email#password",
    }, follow_redirects=False)
    assert r_pw.status_code == 302
    assert r_pw.headers["Location"].endswith("/account/email#password")
    db = appmod.connect()
    pw_hash = db.execute("SELECT password_hash FROM users WHERE id = ?", (alice,)).fetchone()["password_hash"]
    assert appmod.check_password_hash(pw_hash, "newpass123")
    db.close()
