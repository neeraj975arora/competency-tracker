"""Pytest suite for the Competency Tracker (auth, CSRF, lockout, tracker, admin console, CLI).

Every test gets its own temporary SQLite database, so the real tracker.db is never touched.
Run with:  python -m pytest tests
"""

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest

import app as tracker


# ---------- fixtures and helpers ----------

@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "tracker-test.db")
    monkeypatch.setattr(tracker, "COOKIE_SECURE", False)
    tracker.init_db()
    tracker.app.config.update(TESTING=True)
    return tracker.app


@pytest.fixture
def cli(app_env):
    return app_env.test_cli_runner()


@pytest.fixture
def make_user(cli):
    """Create an account through the CLI and return its temporary password."""
    def _make(email, role="user", profile=None):
        args = ["create-user", email, "--role", role]
        if profile:
            args += ["--profile", profile]
        result = cli.invoke(args=args)
        assert result.exit_code == 0, result.output
        return re.search(r"shown once\): (\S+)", result.output).group(1)
    return _make


def csrf_of(client, path="/"):
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, f"no csrf token found on {path}"
    return match.group(1)


def login(client, email, password):
    return client.post("/login", data={"email": email, "password": password})


def first_login(client, email, temp, new="Fresh-Password-99"):
    """Sign in with a temporary password and set a permanent one. Returns the new password."""
    login(client, email, temp)
    token = csrf_of(client, "/account/password")
    resp = client.post("/account/password", data={
        "csrf": token, "current_password": temp,
        "new_password": new, "confirm_password": new,
    })
    assert resp.status_code == 302
    return new


def save_status(client, data):
    token = csrf_of(client, "/")
    return client.post("/save", data={"csrf": token, **data})


def default_profile_id():
    return tracker.query_one("SELECT id FROM profiles ORDER BY id LIMIT 1")["id"]


def topic_id(subdomain, topic):
    row = tracker.query_one(
        """SELECT t.id FROM topics t JOIN subdomains s ON s.id = t.subdomain_id
           WHERE s.name = ? AND t.name = ? ORDER BY t.id LIMIT 1""", (subdomain, topic))
    return row["id"]


def _token_for(email, tv, secret=None, hours=1):
    now = datetime.now(timezone.utc)
    claims = {"sub": email, "role": "user", "tv": tv, "csrf": "x",
              "iat": now, "exp": now + timedelta(hours=hours)}
    return jwt.encode(claims, secret or tracker.JWT_SECRET, algorithm="HS256")


@pytest.fixture
def alice(app_env, make_user):
    temp = make_user("alice@impressico.com")
    client = app_env.test_client()
    password = first_login(client, "alice@impressico.com", temp)
    return {"email": "alice@impressico.com", "password": password, "client": client}


@pytest.fixture
def bob(app_env, make_user):
    temp = make_user("bob@impressico.com")
    client = app_env.test_client()
    password = first_login(client, "bob@impressico.com", temp)
    return {"email": "bob@impressico.com", "password": password, "client": client}


@pytest.fixture
def admin(app_env, make_user):
    temp = make_user("admin@impressico.com", role="admin")
    client = app_env.test_client()
    password = first_login(client, "admin@impressico.com", temp)
    return {"email": "admin@impressico.com", "password": password, "client": client}


# ---------- email validation and profile tree ----------

@pytest.mark.parametrize("raw, expected", [
    ("Alice@Impressico.com", "alice@impressico.com"),
    ("  bob@impressico.com  ", "bob@impressico.com"),
    ("alice@gmail.com", None),
    ("alice@impressico.com.evil.com", None),
    ("alice@sub.impressico.com", None),
    ("not-an-email", None),
    ("", None),
    (None, None),
])
def test_normalise_email(raw, expected):
    assert tracker.normalise_email(raw) == expected


def test_default_profile_is_seeded(app_env):
    tree = tracker.profile_tree(default_profile_id())
    assert [d["name"] for d in tree] == ["A. Technical Skills", "B. Analytical Skills", "C. Communication"]
    assert [d["color"] for d in tree] == ["blue", "orange", "teal"]
    topics = [t for d in tree for s in d["subdomains"] for t in s["topics"]]
    assert len(topics) == 23
    assert len(tracker.profile_topic_ids(default_profile_id())) == 23


def test_init_db_does_not_reseed_existing_profiles(app_env):
    tracker.init_db()
    tracker.init_db()
    count = tracker.query_one("SELECT COUNT(*) AS n FROM profiles")["n"]
    assert count == 1


def test_database_upgrade_from_first_version(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE users (
            email TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
            active INTEGER NOT NULL DEFAULT 1, must_change_password INTEGER NOT NULL DEFAULT 1,
            token_version INTEGER NOT NULL DEFAULT 0, failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        INSERT INTO users (email, password_hash, role, created_at)
            VALUES ('old.user@impressico.com', 'x', 'user', '2026-01-01');
        CREATE TABLE ratings (
            person TEXT NOT NULL, subject_key TEXT NOT NULL, status TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, PRIMARY KEY (person, subject_key));
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(tracker, "DB_PATH", db)
    tracker.init_db()

    user = tracker.query_one("SELECT profile_id FROM users WHERE email = 'old.user@impressico.com'")
    assert user["profile_id"] is not None
    cols = {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(ratings)")}
    assert "topic_id" in cols and "subject_key" not in cols


# ---------- anonymous access and sign-in page ----------

def test_anonymous_user_is_sent_to_login(app_env):
    resp = app_env.test_client().get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


def test_login_page_renders(app_env):
    resp = app_env.test_client().get("/login")
    assert resp.status_code == 200
    assert b"Sign in" in resp.data


def test_security_headers_present(app_env):
    resp = app_env.test_client().get("/login")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["Cache-Control"] == "no-store"


# ---------- login ----------

def test_login_wrong_password_gives_generic_error(app_env, make_user):
    make_user("alice@impressico.com")
    resp = login(app_env.test_client(), "alice@impressico.com", "wrong-password")
    assert resp.status_code == 401
    assert b"Invalid email or password." in resp.data


def test_unknown_email_gives_same_generic_error(app_env):
    resp = login(app_env.test_client(), "ghost@impressico.com", "whatever")
    assert resp.status_code == 401
    assert b"Invalid email or password." in resp.data


def test_outside_domain_is_rejected(app_env):
    assert login(app_env.test_client(), "me@gmail.com", "whatever").status_code == 401


def test_first_login_is_forced_to_change_password(app_env, make_user):
    temp = make_user("carol@impressico.com")
    client = app_env.test_client()
    resp = login(client, "carol@impressico.com", temp)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/account/password")
    assert client.get("/").headers["Location"].endswith("/account/password")


def test_login_cookie_is_httponly_and_samesite(app_env, make_user):
    temp = make_user("carol@impressico.com")
    cookie = login(app_env.test_client(), "carol@impressico.com", temp).headers["Set-Cookie"]
    assert "access_token=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Secure" not in cookie


def test_secure_flag_when_cookie_secure_enabled(app_env, make_user, monkeypatch):
    monkeypatch.setattr(tracker, "COOKIE_SECURE", True)
    temp = make_user("carol@impressico.com")
    resp = login(app_env.test_client(), "carol@impressico.com", temp)
    assert "Secure" in resp.headers["Set-Cookie"]


# ---------- lockout ----------

def test_five_wrong_passwords_lock_the_account(app_env, make_user):
    temp = make_user("dave@impressico.com")
    client = app_env.test_client()
    statuses = [login(client, "dave@impressico.com", "wrong").status_code for _ in range(5)]
    assert statuses == [401] * 5
    assert login(client, "dave@impressico.com", temp).status_code == 429


def test_lock_expires_after_the_window(app_env, make_user):
    temp = make_user("dave@impressico.com")
    client = app_env.test_client()
    for _ in range(5):
        login(client, "dave@impressico.com", "wrong")
    tracker.execute("UPDATE users SET locked_until = ? WHERE email = ?",
                    (time.time() - 1, "dave@impressico.com"))
    assert login(client, "dave@impressico.com", temp).status_code == 302


def test_successful_login_resets_failed_count(app_env, make_user):
    temp = make_user("dave@impressico.com")
    client = app_env.test_client()
    for _ in range(4):
        login(client, "dave@impressico.com", "wrong")
    login(client, "dave@impressico.com", temp)
    row = tracker.query_one("SELECT failed_attempts FROM users WHERE email = ?", ("dave@impressico.com",))
    assert row["failed_attempts"] == 0


def test_reset_password_unlocks_and_forces_change(app_env, make_user, cli):
    make_user("dave@impressico.com")
    client = app_env.test_client()
    for _ in range(5):
        login(client, "dave@impressico.com", "wrong")
    result = cli.invoke(args=["reset-password", "dave@impressico.com"])
    new_temp = re.search(r"shown once\): (\S+)", result.output).group(1)
    resp = login(client, "dave@impressico.com", new_temp)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/account/password")


# ---------- password change ----------

def test_change_password_requires_csrf(app_env, make_user):
    temp = make_user("erin@impressico.com")
    client = app_env.test_client()
    login(client, "erin@impressico.com", temp)
    resp = client.post("/account/password", data={
        "csrf": "forged", "current_password": temp,
        "new_password": "Fresh-Password-99", "confirm_password": "Fresh-Password-99",
    })
    assert resp.status_code == 400


@pytest.mark.parametrize("current_ok, new, confirm, message", [
    (False, "Fresh-Password-99", "Fresh-Password-99", b"Current password is incorrect."),
    (True, "short", "short", b"at least 10 characters"),
    (True, "Fresh-Password-99", "Other-Password-99", b"do not match"),
])
def test_change_password_validation(app_env, make_user, current_ok, new, confirm, message):
    temp = make_user("erin@impressico.com")
    client = app_env.test_client()
    login(client, "erin@impressico.com", temp)
    token = csrf_of(client, "/account/password")
    current = temp if current_ok else "not-the-password"
    resp = client.post("/account/password", data={
        "csrf": token, "current_password": current, "new_password": new, "confirm_password": confirm,
    })
    assert resp.status_code == 400
    assert message in resp.data


def test_cannot_reuse_current_password(app_env, make_user):
    temp = make_user("erin@impressico.com")
    client = app_env.test_client()
    login(client, "erin@impressico.com", temp)
    token = csrf_of(client, "/account/password")
    resp = client.post("/account/password", data={
        "csrf": token, "current_password": temp, "new_password": temp, "confirm_password": temp,
    })
    assert resp.status_code == 400
    assert b"different from the current one" in resp.data


def test_password_change_clears_forced_flag_and_old_session(app_env, make_user):
    temp = make_user("erin@impressico.com")
    client = app_env.test_client()
    first_login(client, "erin@impressico.com", temp)
    row = tracker.query_one("SELECT must_change_password FROM users WHERE email = ?", ("erin@impressico.com",))
    assert row["must_change_password"] == 0
    assert client.get("/").status_code == 200
    old_client = app_env.test_client()
    old_client.set_cookie("access_token", _token_for("erin@impressico.com", tv=0))
    assert old_client.get("/").status_code == 302


# ---------- tracker: saving and viewing ----------

def test_new_user_without_profile_sees_message(app_env, make_user, cli):
    temp = make_user("noprof@impressico.com", role="admin")  # admins may have no profile
    client = app_env.test_client()
    first_login(client, "noprof@impressico.com", temp)
    page = client.get("/").get_data(as_text=True)
    assert "No competency profile is assigned to you yet." in page
    token = csrf_of(client, "/account/password")
    assert client.post("/save", data={"csrf": token}).status_code == 400


def test_save_requires_csrf(alice):
    resp = alice["client"].post("/save", data={f"status_{topic_id('Python', 'Lists')}": "Weak"})
    assert resp.status_code == 400


def test_save_and_reload_own_status(alice):
    tid = topic_id("Python", "Lists")
    resp = save_status(alice["client"], {f"status_{tid}": "Weak", f"notes_{tid}": "Need slicing practice"})
    assert resp.status_code == 302
    page = alice["client"].get("/").get_data(as_text=True)
    assert "Need slicing practice" in page
    assert f'value="Weak" selected' in page


def test_invalid_status_is_stored_as_not_rated(alice):
    tid = topic_id("Python", "Lists")
    save_status(alice["client"], {f"status_{tid}": "Expert"})
    page = alice["client"].get("/").get_data(as_text=True)
    assert "Expert" not in page


def test_weak_areas_list_only_weak_and_learning(alice):
    save_status(alice["client"], {
        f"status_{topic_id('Python', 'Lists')}": "Weak",
        f"status_{topic_id('PySpark', 'Joins')}": "Learning",
        f"status_{topic_id('SQL Server', 'Joins')}": "Confident",
    })
    page = alice["client"].get("/").get_data(as_text=True)
    weak_section = page.split("Weak areas")[1].split("</section>")[0]
    assert "Python: Lists" in weak_section
    assert "PySpark: Joins" in weak_section
    assert "SQL Server: Joins" not in weak_section
    assert "Weak areas (2)" in page


def test_notes_are_trimmed_to_300_characters(alice):
    tid = topic_id("Python", "Lists")
    save_status(alice["client"], {f"notes_{tid}": "x" * 500})
    page = alice["client"].get("/").get_data(as_text=True)
    assert "x" * 300 in page
    assert "x" * 301 not in page


def test_employees_cannot_see_each_other(alice, bob):
    tid = topic_id("Python", "Lists")
    save_status(alice["client"], {f"notes_{tid}": "Alice private note"})
    assert "Alice private note" not in bob["client"].get("/").get_data(as_text=True)
    assert "Alice private note" not in bob["client"].get(
        "/?person=alice@impressico.com").get_data(as_text=True)


def test_saving_only_writes_the_signed_in_user(alice, bob):
    tid = topic_id("Python", "Lists")
    save_status(bob["client"], {f"status_{tid}": "Confident"})
    row = tracker.query_one("SELECT person FROM ratings WHERE topic_id = ?", (tid,))
    assert row["person"] == "bob@impressico.com"


# ---------- admin: access control ----------

def test_normal_user_cannot_reach_admin_pages(alice):
    client = alice["client"]
    assert client.get("/admin").status_code == 403
    assert client.get("/admin/profiles").status_code == 403
    assert client.get(f"/admin/profiles/{default_profile_id()}").status_code == 403


def test_admin_nav_only_for_admins(alice, admin):
    assert "Admin</a>" not in alice["client"].get("/").get_data(as_text=True)
    assert "Admin</a>" in admin["client"].get("/").get_data(as_text=True)


def test_admin_post_requires_csrf(admin):
    resp = admin["client"].post("/admin/users", data={"csrf": "forged", "email": "x@impressico.com",
                                                      "role": "user", "profile_id": ""})
    assert resp.status_code == 400


# ---------- admin: users ----------

def add_user(client, email, role="user", profile_id=None):
    data = {"csrf": csrf_of(client, "/admin"), "email": email, "role": role,
            "profile_id": "" if profile_id is None else str(profile_id)}
    return client.post("/admin/users", data=data)


def test_admin_creates_user_and_shows_password_once(admin, app_env):
    resp = add_user(admin["client"], "new.person@impressico.com", profile_id=default_profile_id())
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "Account created" in page
    temp = re.search(r'class="secret">([^<]+)<', page).group(1)

    # The password is not kept anywhere on the next page load.
    assert temp not in admin["client"].get("/admin").get_data(as_text=True)

    new_client = app_env.test_client()
    resp = login(new_client, "new.person@impressico.com", temp)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/account/password")


def test_admin_add_user_validation(admin, alice):
    client = admin["client"]
    resp = add_user(client, "x@gmail.com", profile_id=default_profile_id())
    assert resp.status_code == 400 and b"must end with @impressico.com" in resp.data
    resp = add_user(client, "alice@impressico.com", profile_id=default_profile_id())
    assert resp.status_code == 400 and b"already exists" in resp.data
    resp = add_user(client, "noprofile@impressico.com", role="user", profile_id=None)
    assert resp.status_code == 400 and b"Choose a competency profile" in resp.data


def test_admin_can_create_admin_without_profile(admin):
    resp = add_user(admin["client"], "second.admin@impressico.com", role="admin")
    assert resp.status_code == 200
    row = tracker.query_one("SELECT role, profile_id FROM users WHERE email = ?",
                            ("second.admin@impressico.com",))
    assert row["role"] == "admin" and row["profile_id"] is None


def test_admin_reset_password_shows_new_password_and_signs_user_out(alice, admin):
    client = admin["client"]
    resp = client.post("/admin/users/action", data={
        "csrf": csrf_of(client, "/admin"), "email": "alice@impressico.com", "action": "reset"})
    assert resp.status_code == 200
    assert "Password reset" in resp.get_data(as_text=True)
    assert alice["client"].get("/").headers["Location"].endswith("/login")


def test_admin_cannot_disable_self(admin):
    client = admin["client"]
    resp = client.post("/admin/users/action", data={
        "csrf": csrf_of(client, "/admin"), "email": "admin@impressico.com", "action": "disable"})
    assert resp.status_code == 400
    assert b"cannot disable your own account" in resp.data


def test_admin_disable_and_enable_user(alice, admin, app_env):
    client = admin["client"]
    token = csrf_of(client, "/admin")
    client.post("/admin/users/action", data={"csrf": token, "email": "alice@impressico.com", "action": "disable"})
    assert alice["client"].get("/").headers["Location"].endswith("/login")
    client.post("/admin/users/action", data={"csrf": token, "email": "alice@impressico.com", "action": "enable"})
    assert login(app_env.test_client(), "alice@impressico.com", alice["password"]).status_code == 302


def test_admin_moves_user_to_another_profile(alice, admin):
    client = admin["client"]
    pid2 = _make_profile(client, "Second Profile")
    client.post("/admin/users/action", data={
        "csrf": csrf_of(client, "/admin"), "email": "alice@impressico.com",
        "action": "profile", "profile_id": str(pid2)})
    page = alice["client"].get("/").get_data(as_text=True)
    assert "Profile: Second Profile" in page
    assert "Python: Lists" not in page


def test_user_cannot_be_left_without_profile(alice, admin):
    client = admin["client"]
    resp = client.post("/admin/users/action", data={
        "csrf": csrf_of(client, "/admin"), "email": "alice@impressico.com",
        "action": "profile", "profile_id": ""})
    assert resp.status_code == 400
    assert b"Users need a competency profile" in resp.data


# ---------- admin: profile editor ----------

def _make_profile(client, name):
    token = csrf_of(client, "/admin/profiles")
    resp = client.post("/admin/profiles", data={"csrf": token, "name": name})
    assert resp.status_code == 302
    return tracker.query_one("SELECT id FROM profiles WHERE name = ?", (name,))["id"]


def test_create_profile_and_reject_duplicate_name(admin):
    client = admin["client"]
    pid = _make_profile(client, "Data Engineer Coaching")
    assert tracker.query_one("SELECT name FROM profiles WHERE id = ?", (pid,))["name"] == "Data Engineer Coaching"
    token = csrf_of(client, "/admin/profiles")
    resp = client.post("/admin/profiles", data={"csrf": token, "name": "Data Engineer Coaching"})
    assert resp.status_code == 400
    assert b"already exists" in resp.data


def test_add_domain_with_colour(admin):
    client = admin["client"]
    pid = _make_profile(client, "Colour Test")
    token = csrf_of(client, f"/admin/profiles/{pid}")
    client.post(f"/admin/profiles/{pid}/domains", data={"csrf": token, "name": "Coding", "color": "purple"})
    page = client.get(f"/admin/profiles/{pid}").get_data(as_text=True)
    assert 'class="area purple"' in page
    assert "Coding" in page


def test_domain_rejects_unknown_colour(admin):
    client = admin["client"]
    pid = _make_profile(client, "Colour Check")
    token = csrf_of(client, f"/admin/profiles/{pid}")
    resp = client.post(f"/admin/profiles/{pid}/domains",
                       data={"csrf": token, "name": "Coding", "color": "neon-pink"})
    assert resp.status_code == 400


def test_build_subdomains_and_topics(admin):
    client = admin["client"]
    pid = _make_profile(client, "Tree Test")
    token = csrf_of(client, f"/admin/profiles/{pid}")
    client.post(f"/admin/profiles/{pid}/domains", data={"csrf": token, "name": "Data", "color": "green"})
    did = tracker.query_one("SELECT id FROM domains WHERE name = 'Data' AND profile_id = ?", (pid,))["id"]
    client.post(f"/admin/domains/{did}/subdomains", data={"csrf": csrf_of(client, f"/admin/profiles/{pid}"),
                                                         "name": "SQL"})
    sid = tracker.query_one("SELECT id FROM subdomains WHERE domain_id = ?", (did,))["id"]
    client.post(f"/admin/subdomains/{sid}/topics", data={
        "csrf": csrf_of(client, f"/admin/profiles/{pid}"),
        "topics": "Joins\n  Views \n\nJoins\nIndexes"})
    names = [r["name"] for r in tracker.query_all("SELECT name FROM topics WHERE subdomain_id = ? ORDER BY position", (sid,))]
    assert names == ["Joins", "Views", "Indexes"]  # blanks and duplicates dropped


def test_deleting_topic_removes_its_ratings(alice, admin):
    tid = topic_id("Communication", "Professional technical explanations")
    save_status(alice["client"], {f"status_{tid}": "Weak"})
    client = admin["client"]
    client.post(f"/admin/topics/{tid}/delete", data={"csrf": csrf_of(client, f"/admin/profiles/{default_profile_id()}")})
    assert tracker.query_one("SELECT 1 FROM topics WHERE id = ?", (tid,)) is None
    assert tracker.query_one("SELECT 1 FROM ratings WHERE topic_id = ?", (tid,)) is None


def test_deleting_domain_cascades(admin):
    client = admin["client"]
    pid = default_profile_id()
    did = tracker.query_one("SELECT id FROM domains WHERE name = 'C. Communication'")["id"]
    client.post(f"/admin/domains/{did}/delete", data={"csrf": csrf_of(client, f"/admin/profiles/{pid}")})
    assert tracker.query_one("SELECT COUNT(*) AS n FROM topics")["n"] == 23 - 1


def test_cannot_delete_profile_with_users(alice, admin):
    client = admin["client"]
    resp = client.post(f"/admin/profiles/{default_profile_id()}/delete",
                       data={"csrf": csrf_of(client, "/admin/profiles")})
    assert resp.status_code == 400
    assert b"Move users to another profile" in resp.data


def test_empty_profile_can_be_deleted(admin):
    client = admin["client"]
    pid = _make_profile(client, "Temporary")
    resp = client.post(f"/admin/profiles/{pid}/delete", data={"csrf": csrf_of(client, "/admin/profiles")})
    assert resp.status_code == 302
    assert tracker.query_one("SELECT 1 FROM profiles WHERE id = ?", (pid,)) is None


def test_admin_can_view_employee_read_only(alice, admin):
    tid = topic_id("Python", "Lists")
    save_status(alice["client"], {f"notes_{tid}": "Alice note for coach"})
    page = admin["client"].get("/?person=alice@impressico.com").get_data(as_text=True)
    assert "Viewing alice@impressico.com (read-only)" in page
    assert "Alice note for coach" in page
    assert "Save my status" not in page


def test_admin_unknown_person_falls_back_to_self(admin):
    page = admin["client"].get("/?person=nobody@impressico.com").get_data(as_text=True)
    assert "Viewing nobody@impressico.com" not in page
    assert "Signed in as <strong>admin@impressico.com</strong>" in page


def test_non_admin_cannot_view_others(alice):
    page = alice["client"].get("/?person=admin@impressico.com").get_data(as_text=True)
    assert "(read-only)" not in page


# ---------- logout, tampering and revocation ----------

def test_logout_clears_session(alice):
    client = alice["client"]
    assert client.post("/logout").status_code == 302
    assert client.get("/").headers["Location"].endswith("/login")


def test_logout_needs_post(alice):
    assert alice["client"].get("/logout").status_code == 405


def test_tampered_cookie_is_rejected(app_env):
    client = app_env.test_client()
    client.set_cookie("access_token", "garbage.token.value")
    assert client.get("/").headers["Location"].endswith("/login")


def test_token_signed_with_other_secret_is_rejected(alice, app_env):
    user = tracker.query_one("SELECT token_version FROM users WHERE email = ?", (alice["email"],))
    forged = _token_for(alice["email"], tv=user["token_version"], secret="attacker-secret-value-0123456789abcdef")
    client = app_env.test_client()
    client.set_cookie("access_token", forged)
    assert client.get("/").headers["Location"].endswith("/login")


def test_expired_token_is_rejected(alice, app_env):
    user = tracker.query_one("SELECT token_version FROM users WHERE email = ?", (alice["email"],))
    expired = _token_for(alice["email"], tv=user["token_version"], hours=-1)
    client = app_env.test_client()
    client.set_cookie("access_token", expired)
    assert client.get("/").headers["Location"].endswith("/login")


def test_disabled_user_loses_existing_session(alice, cli, app_env):
    assert alice["client"].get("/").status_code == 200
    cli.invoke(args=["disable-user", alice["email"]])
    assert alice["client"].get("/").headers["Location"].endswith("/login")
    assert login(app_env.test_client(), alice["email"], alice["password"]).status_code == 401


def test_enable_user_restores_sign_in(alice, cli, app_env):
    cli.invoke(args=["disable-user", alice["email"]])
    cli.invoke(args=["enable-user", alice["email"]])
    assert login(app_env.test_client(), alice["email"], alice["password"]).status_code == 302


def test_reset_password_signs_out_existing_session(alice, cli):
    cli.invoke(args=["reset-password", alice["email"]])
    assert alice["client"].get("/").headers["Location"].endswith("/login")


# ---------- CLI commands ----------

def test_create_user_rejects_wrong_domain(cli):
    result = cli.invoke(args=["create-user", "x@gmail.com"])
    assert result.exit_code != 0
    assert "must end with @impressico.com" in result.output


def test_create_user_rejects_duplicates(make_user, cli):
    make_user("frank@impressico.com")
    result = cli.invoke(args=["create-user", "frank@impressico.com"])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_create_user_defaults_to_first_profile(make_user):
    make_user("frank@impressico.com")
    row = tracker.query_one("SELECT role, must_change_password, profile_id FROM users WHERE email = ?",
                            ("frank@impressico.com",))
    assert row["role"] == "user"
    assert row["must_change_password"] == 1
    assert row["profile_id"] == default_profile_id()


def test_create_user_with_named_profile(make_user):
    tracker.execute("INSERT INTO profiles (name, created_at) VALUES ('Named', '2026-01-01')")
    named = tracker.query_one("SELECT id FROM profiles WHERE name = 'Named'")["id"]
    make_user("gina@impressico.com", profile="Named")
    assert tracker.query_one("SELECT profile_id FROM users WHERE email = ?",
                             ("gina@impressico.com",))["profile_id"] == named


def test_create_user_admin_role(make_user):
    make_user("boss@impressico.com", role="admin")
    assert tracker.query_one("SELECT role FROM users WHERE email = ?", ("boss@impressico.com",))["role"] == "admin"


def test_passwords_are_stored_hashed(make_user):
    temp = make_user("frank@impressico.com")
    row = tracker.query_one("SELECT password_hash FROM users WHERE email = ?", ("frank@impressico.com",))
    assert temp not in row["password_hash"]
    assert row["password_hash"].startswith(("scrypt:", "pbkdf2:"))


def test_list_users_shows_accounts(make_user, cli):
    make_user("frank@impressico.com")
    make_user("gina@impressico.com", role="admin")
    out = cli.invoke(args=["list-users"]).output
    assert "frank@impressico.com" in out
    assert "gina@impressico.com" in out
    assert "Claude Automation Analyst" in out


def test_disable_unknown_user_fails(cli):
    result = cli.invoke(args=["disable-user", "ghost@impressico.com"])
    assert result.exit_code != 0
    assert "No user" in result.output


# ---------- profile import (JSON) ----------

GOOD_SPEC = {
    "name": "Imported Profile",
    "domains": [
        {"name": "Coding", "color": "purple", "subdomains": [
            {"name": "Python", "topics": ["Lists", "Dictionaries"]},
            {"name": "SQL", "topics": ["Joins"]},
        ]},
        {"name": "Talk", "color": "rose", "subdomains": [
            {"name": "Writing", "topics": []},
        ]},
    ],
}


def _write_json(tmp_path, spec, name="profile.json"):
    path = tmp_path / name
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


def test_import_profile_from_json(cli, app_env, tmp_path):
    result = cli.invoke(args=["import-profile", _write_json(tmp_path, GOOD_SPEC)])
    assert result.exit_code == 0, result.output
    assert "3 topics" in result.output
    pid = tracker.query_one("SELECT id FROM profiles WHERE name = 'Imported Profile'")["id"]
    tree = tracker.profile_tree(pid)
    assert [d["color"] for d in tree] == ["purple", "rose"]
    assert len(tracker.profile_topic_ids(pid)) == 3


def test_import_profile_rejects_bad_colour(cli, app_env, tmp_path):
    bad = json.loads(json.dumps(GOOD_SPEC))
    bad["domains"][0]["color"] = "neon"
    result = cli.invoke(args=["import-profile", _write_json(tmp_path, bad)])
    assert result.exit_code != 0
    assert "colour must be one of" in result.output


def test_import_profile_rejects_duplicate_name(cli, app_env, tmp_path):
    path = _write_json(tmp_path, GOOD_SPEC)
    assert cli.invoke(args=["import-profile", path]).exit_code == 0
    result = cli.invoke(args=["import-profile", path])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_import_profile_rejects_invalid_json(cli, app_env, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    result = cli.invoke(args=["import-profile", str(path)])
    assert result.exit_code != 0
    assert "Invalid JSON" in result.output


def test_sample_profile_file_imports(cli, app_env):
    sample = tracker.BASE_DIR / "sample_profile.json"
    result = cli.invoke(args=["import-profile", str(sample)])
    assert result.exit_code == 0, result.output
    assert "Data Engineer Coaching" in tracker.query_one(
        "SELECT name FROM profiles WHERE name = 'Data Engineer Coaching'")["name"]


def test_manage_py_entry_point_lists_commands():
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "manage.py", "--help"], capture_output=True,
                            text=True, cwd=tracker.BASE_DIR, timeout=120)
    assert result.returncode == 0, result.stderr
    for command in ["create-user", "import-profile", "reset-password", "list-users"]:
        assert command in result.stdout


# ---------- default profile ----------

def test_new_users_default_to_data_engineer_profile(app_env, cli, make_user, admin):
    result = cli.invoke(args=["import-profile", str(tracker.BASE_DIR / "sample_profile.json")])
    assert result.exit_code == 0, result.output
    de_id = tracker.query_one("SELECT id FROM profiles WHERE name = 'Data Engineer Coaching'")["id"]

    make_user("default.check@impressico.com")
    row = tracker.query_one("SELECT profile_id FROM users WHERE email = ?", ("default.check@impressico.com",))
    assert row["profile_id"] == de_id

    page = admin["client"].get("/admin").get_data(as_text=True)
    assert f'value="{de_id}" selected' in page
