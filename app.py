"""Competency Tracker - Flask app with JWT login, an admin console and editable competency profiles.

Structure: profile -> domain (coloured) -> sub-domain -> topic.
Each user is assigned one profile and records a status for every topic in it.
Admins create accounts, assign profiles and edit profiles in the browser.
"""

import hmac
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import click
import jwt
from flask import Flask, abort, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("TRACKER_DB", BASE_DIR / "tracker.db"))  # /data/tracker.db in Docker
SECRET_FILE = BASE_DIR / ".jwt_secret"

# Settings (can be overridden with environment variables)
ALLOWED_DOMAIN = os.environ.get("ALLOWED_EMAIL_DOMAIN", "impressico.com").strip().lower()
TOKEN_HOURS = int(os.environ.get("TOKEN_HOURS", "8"))
# Set COOKIE_SECURE=1 when the site is served over https (e.g. a Cloudflare or ngrok link).
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15
MIN_PASSWORD_LENGTH = 10
COOKIE_NAME = "access_token"
EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")

# Status options. "Weak" and "Learning" count as weak areas.
STATUSES = ["Not rated", "Weak", "Learning", "Practising", "Confident"]
WEAK_STATUSES = {"Weak", "Learning"}

# Domain colours offered in the admin editor: (text colour, light background).
COLOR_CHOICES = {
    "blue": ("#2b6cb0", "#dce9f8"),
    "orange": ("#c4601a", "#fde3d3"),
    "teal": ("#1d8a78", "#d9efea"),
    "purple": ("#6b4fbb", "#e7e0f8"),
    "green": ("#2e7d32", "#dcefdc"),
    "rose": ("#b83262", "#f8dce6"),
}

# Created on first run. Admins can edit it or add new profiles in the browser.
DEFAULT_PROFILE = {
    "name": "Claude Automation Analyst",
    "domains": [
        {"name": "A. Technical Skills", "color": "blue", "subdomains": [
            {"name": "Python", "topics": ["Lists", "Tuples", "Dictionaries", "Functions",
                                          "File I/O", "Serialization"]},
            {"name": "SQL Server", "topics": ["Fundamentals", "Commands", "Keys", "Joins",
                                              "Views", "Query writing"]},
            {"name": "PySpark", "topics": ["Fundamentals", "RDD", "DataFrames", "Joins", "Cache",
                                           "Persistence", "Performance tuning"]},
        ]},
        {"name": "B. Analytical Skills", "color": "orange", "subdomains": [
            {"name": "Analytical skills", "topics": ["Problem-solving", "Debugging", "Edge cases"]},
        ]},
        {"name": "C. Communication", "color": "teal", "subdomains": [
            {"name": "Communication", "topics": ["Professional technical explanations"]},
        ]},
    ],
}

app = Flask(__name__)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def clean_text(raw, max_len=80):
    text = re.sub(r"\s+", " ", (raw or "")).strip()
    return text[:max_len] or None


def load_jwt_secret():
    """Use JWT_SECRET from the environment, or a random secret saved beside app.py."""
    value = os.environ.get("JWT_SECRET")
    if value:
        return value
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return SECRET_FILE.read_text(encoding="utf-8").strip()


JWT_SECRET = load_jwt_secret()
DUMMY_HASH = generate_password_hash("not-a-real-password")  # keeps timing similar for unknown emails


# ---------- database ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def query_one(sql, params=()):
    conn = get_db()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def query_all(sql, params=()):
    conn = get_db()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def execute(sql, params=()):
    conn = get_db()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domains (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    color      TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS subdomains (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    position  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subdomain_id INTEGER NOT NULL REFERENCES subdomains(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    position     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
    email                TEXT PRIMARY KEY,
    password_hash        TEXT NOT NULL,
    role                 TEXT NOT NULL DEFAULT 'user',
    active               INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 1,
    token_version        INTEGER NOT NULL DEFAULT 0,
    failed_attempts      INTEGER NOT NULL DEFAULT 0,
    locked_until         REAL NOT NULL DEFAULT 0,
    profile_id           INTEGER REFERENCES profiles(id) ON DELETE SET NULL,
    created_at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ratings (
    person     TEXT NOT NULL,
    topic_id   INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    status     TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (person, topic_id)
);
"""


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _insert_profile_tree(conn, spec):
    pid = conn.execute("INSERT INTO profiles (name, created_at) VALUES (?, ?)",
                       (spec["name"], now_iso())).lastrowid
    for dpos, dom in enumerate(spec["domains"]):
        did = conn.execute(
            "INSERT INTO domains (profile_id, name, color, position) VALUES (?, ?, ?, ?)",
            (pid, dom["name"], dom["color"], dpos)).lastrowid
        for spos, sub in enumerate(dom["subdomains"]):
            sid = conn.execute("INSERT INTO subdomains (domain_id, name, position) VALUES (?, ?, ?)",
                               (did, sub["name"], spos)).lastrowid
            for tpos, topic in enumerate(sub["topics"]):
                conn.execute("INSERT INTO topics (subdomain_id, name, position) VALUES (?, ?, ?)",
                             (sid, topic, tpos))
    return pid


def init_db():
    conn = get_db()
    try:
        # Upgrade databases created by the earlier versions.
        if "topic_id" not in _columns(conn, "ratings") and _columns(conn, "ratings"):
            conn.execute("DROP TABLE ratings")  # old name-based ratings
        migrated = False
        if _columns(conn, "users") and "profile_id" not in _columns(conn, "users"):
            conn.execute("ALTER TABLE users ADD COLUMN profile_id INTEGER REFERENCES profiles(id) ON DELETE SET NULL")
            migrated = True
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0:
            _insert_profile_tree(conn, DEFAULT_PROFILE)
        if migrated:
            default_id = conn.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()[0]
            conn.execute("UPDATE users SET profile_id = ? WHERE profile_id IS NULL AND role = 'user'",
                         (default_id,))
        conn.commit()
    finally:
        conn.close()


def load_ratings(person):
    rows = query_all("SELECT topic_id, status, notes FROM ratings WHERE person = ?", (person,))
    return {row["topic_id"]: {"status": row["status"], "notes": row["notes"]} for row in rows}


def save_ratings(person, rows):
    """rows: iterable of (topic_id, status, notes)."""
    conn = get_db()
    try:
        for topic_id, status, notes in rows:
            conn.execute(
                """
                INSERT INTO ratings (person, topic_id, status, notes, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(person, topic_id) DO UPDATE SET
                    status = excluded.status,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (person, topic_id, status, notes, now_iso()),
            )
        conn.commit()
    finally:
        conn.close()


def profile_tree(profile_id):
    """Return domains -> sub-domains -> topics for one profile as plain dicts."""
    conn = get_db()
    try:
        domains = [dict(r) for r in conn.execute(
            "SELECT id, name, color FROM domains WHERE profile_id = ? ORDER BY position, id",
            (profile_id,))]
        for dom in domains:
            dom["subdomains"] = [dict(r) for r in conn.execute(
                "SELECT id, name FROM subdomains WHERE domain_id = ? ORDER BY position, id",
                (dom["id"],))]
            for sub in dom["subdomains"]:
                sub["topics"] = [dict(r) for r in conn.execute(
                    "SELECT id, name FROM topics WHERE subdomain_id = ? ORDER BY position, id",
                    (sub["id"],))]
        return domains
    finally:
        conn.close()


def profile_topic_ids(profile_id):
    rows = query_all(
        """SELECT t.id FROM topics t
           JOIN subdomains s ON s.id = t.subdomain_id
           JOIN domains d ON d.id = s.domain_id
           WHERE d.profile_id = ?""", (profile_id,))
    return [r["id"] for r in rows]


def next_position(table, parent_col, parent_id):
    row = query_one(f"SELECT COALESCE(MAX(position), -1) + 1 AS nxt FROM {table} WHERE {parent_col} = ?",
                    (parent_id,))
    return row["nxt"]


def build_view(person, profile_id):
    """Build the tracker view for one person and profile, plus summary counts and weak areas."""
    ratings = load_ratings(person)
    counts = {status: 0 for status in STATUSES}
    weak = []
    areas = []
    for dom in profile_tree(profile_id):
        subs = []
        for sub in dom["subdomains"]:
            topics = []
            for topic in sub["topics"]:
                saved = ratings.get(topic["id"], {})
                status = saved.get("status", "Not rated")
                topics.append({"id": topic["id"], "name": topic["name"], "status": status,
                               "notes": saved.get("notes", "")})
                counts[status] += 1
                if status in WEAK_STATUSES:
                    weak.append({"name": f"{sub['name']}: {topic['name']}", "status": status,
                                 "area": dom["name"]})
            subs.append({"name": sub["name"], "topics": topics})
        areas.append({"name": dom["name"], "color": dom["color"], "subs": subs})
    return areas, counts, weak


# ---------- helpers ----------

def normalise_email(raw):
    """Return a lower-case office email, or None if it is malformed or outside the domain."""
    email = (raw or "").strip().lower()[:120]
    if not EMAIL_RE.match(email) or not email.endswith("@" + ALLOWED_DOMAIN):
        return None
    return email


def parse_profile_id(raw):
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if query_one("SELECT id FROM profiles WHERE id = ?", (pid,)) else None


def _temp_password():
    return secrets.token_urlsafe(12)


def _row_or_404(sql, params):
    row = query_one(sql, params)
    if row is None:
        abort(404)
    return row


# ---------- JWT and auth ----------

def issue_token(user):
    now = datetime.now(timezone.utc)
    claims = {
        "sub": user["email"],
        "role": user["role"],
        "tv": user["token_version"],          # bump to revoke all older tokens
        "csrf": secrets.token_urlsafe(24),    # checked against every form post
        "iat": now,
        "exp": now + timedelta(hours=TOKEN_HOURS),
        "jti": secrets.token_urlsafe(12),
    }
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def set_auth_cookie(response, token):
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=TOKEN_HOURS * 3600,
        httponly=True,          # not readable from JavaScript
        secure=COOKIE_SECURE,
        samesite="Lax",
        path="/",
    )
    return response


def load_auth():
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        claims = jwt.decode(
            token, JWT_SECRET, algorithms=["HS256"], options={"require": ["exp", "iat", "sub"]}
        )
    except jwt.PyJWTError:
        return None
    user = query_one("SELECT * FROM users WHERE email = ?", (claims["sub"],))
    if user is None or not user["active"] or user["token_version"] != claims.get("tv"):
        return None
    return {"user": user, "claims": claims}


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        auth = load_auth()
        if auth is None:
            return redirect(url_for("login"))
        g.auth = auth
        if auth["user"]["must_change_password"] and request.endpoint not in {"change_password", "logout"}:
            return redirect(url_for("change_password"))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @login_required
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.auth["user"]["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def require_csrf():
    if not hmac.compare_digest(request.form.get("csrf", ""), g.auth["claims"]["csrf"]):
        abort(400, "Form expired. Please reload the page and try again.")


@app.context_processor
def inject_globals():
    auth = getattr(g, "auth", None)
    return {
        "signed_in": auth is not None,
        "is_admin_user": bool(auth and auth["user"]["role"] == "admin"),
        "csrf_token": auth["claims"]["csrf"] if auth else "",
        "allowed_domain": ALLOWED_DOMAIN,
    }


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------- sign-in and password ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error=None, email="")

    raw_email = request.form.get("email", "")
    password = request.form.get("password", "")
    email = normalise_email(raw_email)
    user = query_one("SELECT * FROM users WHERE email = ?", (email,)) if email else None

    generic_error = "Invalid email or password."
    if user is None or not user["active"]:
        check_password_hash(DUMMY_HASH, password)
        return render_template("login.html", error=generic_error, email=raw_email), 401

    now = time.time()
    if user["locked_until"] > now:
        return render_template(
            "login.html", error="Too many failed attempts. Try again in a few minutes.", email=raw_email
        ), 429

    if not check_password_hash(user["password_hash"], password):
        failed = user["failed_attempts"] + 1
        locked_until = 0
        if failed >= MAX_FAILED_ATTEMPTS:
            locked_until = now + LOCK_MINUTES * 60
            failed = 0
        execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE email = ?",
                (failed, locked_until, email))
        return render_template("login.html", error=generic_error, email=raw_email), 401

    execute("UPDATE users SET failed_attempts = 0, locked_until = 0 WHERE email = ?", (email,))
    target = "change_password" if user["must_change_password"] else "index"
    response = redirect(url_for(target))
    return set_auth_cookie(response, issue_token(user))


@app.post("/logout")
def logout():
    response = redirect(url_for("login"))
    response.delete_cookie(COOKIE_NAME, path="/", secure=COOKIE_SECURE, httponly=True, samesite="Lax")
    return response


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    error = None
    user = g.auth["user"]
    if request.method == "POST":
        require_csrf()
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if not check_password_hash(user["password_hash"], current):
            error = "Current password is incorrect."
        elif len(new) < MIN_PASSWORD_LENGTH:
            error = f"New password must be at least {MIN_PASSWORD_LENGTH} characters."
        elif new != confirm:
            error = "The new passwords do not match."
        elif new == current:
            error = "Choose a password different from the current one."

        if error is None:
            execute(
                """UPDATE users SET password_hash = ?, must_change_password = 0,
                   token_version = token_version + 1 WHERE email = ?""",
                (generate_password_hash(new), user["email"]),
            )
            fresh = query_one("SELECT * FROM users WHERE email = ?", (user["email"],))
            response = redirect(url_for("index", changed=1))
            return set_auth_cookie(response, issue_token(fresh))

        return render_template("password.html", error=error,
                               forced=user["must_change_password"]), 400

    return render_template("password.html", error=None, forced=user["must_change_password"])


# ---------- tracker ----------

@app.route("/")
@login_required
def index():
    me_user = g.auth["user"]
    me = me_user["email"]
    is_admin = me_user["role"] == "admin"

    viewing_user = me_user
    if is_admin and request.args.get("person"):
        target = normalise_email(request.args.get("person"))
        found = query_one("SELECT * FROM users WHERE email = ?", (target,)) if target else None
        if found is not None:
            viewing_user = found
    viewing = viewing_user["email"]
    editable = viewing == me

    profile_name = None
    areas, weak = [], []
    counts = {status: 0 for status in STATUSES}
    if viewing_user["profile_id"] is not None:
        profile_name = _row_or_404("SELECT name FROM profiles WHERE id = ?",
                                   (viewing_user["profile_id"],))["name"]
        areas, counts, weak = build_view(viewing, viewing_user["profile_id"])

    return render_template(
        "index.html",
        me=me,
        viewing=viewing,
        editable=editable,
        profile_name=profile_name,
        areas=areas,
        statuses=STATUSES,
        counts=counts,
        weak=weak,
        saved=request.args.get("saved") == "1",
        changed=request.args.get("changed") == "1",
    )


@app.post("/save")
@login_required
def save():
    require_csrf()
    user = g.auth["user"]
    if user["profile_id"] is None:
        abort(400, "No competency profile is assigned to you.")
    rows = []
    for topic_id in profile_topic_ids(user["profile_id"]):
        status = request.form.get(f"status_{topic_id}", "Not rated")
        if status not in STATUSES:
            status = "Not rated"
        notes = request.form.get(f"notes_{topic_id}", "").strip()[:300]
        rows.append((topic_id, status, notes))
    save_ratings(user["email"], rows)  # people can only save their own status
    return redirect(url_for("index", saved=1))


# ---------- admin: users ----------

def render_admin(issued=None, message=None, error=None, status=200):
    users = query_all(
        """SELECT u.email, u.role, u.active, u.must_change_password, u.profile_id,
                  p.name AS profile_name
           FROM users u LEFT JOIN profiles p ON p.id = u.profile_id
           ORDER BY u.email""")
    profiles = query_all("SELECT id, name FROM profiles ORDER BY name")
    return render_template("admin.html", users=users, profiles=profiles, issued=issued,
                           message=message, error=error, me=g.auth["user"]["email"]), status


@app.route("/admin")
@admin_required
def admin_home():
    return render_admin()


@app.post("/admin/users")
@admin_required
def admin_add_user():
    require_csrf()
    email = normalise_email(request.form.get("email"))
    role = request.form.get("role", "user")
    if role not in ("user", "admin"):
        role = "user"
    profile_id = parse_profile_id(request.form.get("profile_id"))

    if email is None:
        return render_admin(error=f"Email must end with @{ALLOWED_DOMAIN}.", status=400)
    if query_one("SELECT 1 FROM users WHERE email = ?", (email,)):
        return render_admin(error="That user already exists.", status=400)
    if role == "user" and profile_id is None:
        return render_admin(error="Choose a competency profile for this user.", status=400)

    temp = _temp_password()
    execute(
        """INSERT INTO users (email, password_hash, role, profile_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (email, generate_password_hash(temp), role, profile_id, now_iso()),
    )
    issued = {"title": "Account created", "email": email, "password": temp}
    return render_admin(issued=issued)


@app.post("/admin/users/action")
@admin_required
def admin_user_action():
    require_csrf()
    me = g.auth["user"]["email"]
    email = normalise_email(request.form.get("email"))
    user = query_one("SELECT * FROM users WHERE email = ?", (email,)) if email else None
    if user is None:
        return render_admin(error="No such user.", status=404)

    action = request.form.get("action", "")
    if action == "profile":
        if request.form.get("profile_id", "") == "":
            pid = None
        else:
            pid = parse_profile_id(request.form.get("profile_id"))
            if pid is None:
                return render_admin(error="Unknown profile.", status=400)
        if user["role"] == "user" and pid is None:
            return render_admin(error="Users need a competency profile.", status=400)
        execute("UPDATE users SET profile_id = ? WHERE email = ?", (pid, email))
        return render_admin(message=f"Profile updated for {email}.")

    if action == "reset":
        temp = _temp_password()
        execute(
            """UPDATE users SET password_hash = ?, must_change_password = 1,
               token_version = token_version + 1, failed_attempts = 0, locked_until = 0
               WHERE email = ?""",
            (generate_password_hash(temp), email),
        )
        issued = {"title": "Password reset", "email": email, "password": temp}
        return render_admin(issued=issued)

    if action in ("disable", "enable"):
        if action == "disable" and email == me:
            return render_admin(error="You cannot disable your own account.", status=400)
        active = 1 if action == "enable" else 0
        execute("UPDATE users SET active = ?, token_version = token_version + 1 WHERE email = ?",
                (active, email))
        return render_admin(message=f"{email} is now {'enabled' if active else 'disabled'}.")

    abort(400)


# ---------- admin: profiles ----------

def render_profiles(error=None, message=None, status=200):
    profiles = query_all(
        """SELECT p.id, p.name,
                  (SELECT COUNT(*) FROM domains d WHERE d.profile_id = p.id) AS domain_count,
                  (SELECT COUNT(*) FROM users u WHERE u.profile_id = p.id) AS user_count
           FROM profiles p ORDER BY p.name""")
    return render_template("admin_profiles.html", profiles=profiles, error=error,
                           message=message), status


def render_profile(pid, error=None, status=200):
    profile = _row_or_404("SELECT id, name FROM profiles WHERE id = ?", (pid,))
    return render_template("admin_profile.html", profile=profile, tree=profile_tree(pid),
                           colors=list(COLOR_CHOICES), error=error), status


@app.get("/admin/profiles")
@admin_required
def admin_profiles():
    return render_profiles()


@app.post("/admin/profiles")
@admin_required
def admin_create_profile():
    require_csrf()
    name = clean_text(request.form.get("name"))
    if not name:
        return render_profiles(error="Enter a profile name.", status=400)
    try:
        conn = get_db()
        try:
            pid = conn.execute("INSERT INTO profiles (name, created_at) VALUES (?, ?)",
                               (name, now_iso())).lastrowid
            conn.commit()
        finally:
            conn.close()
    except sqlite3.IntegrityError:
        return render_profiles(error="A profile with that name already exists.", status=400)
    return redirect(url_for("admin_profile", pid=pid))


@app.get("/admin/profiles/<int:pid>")
@admin_required
def admin_profile(pid):
    return render_profile(pid)


@app.post("/admin/profiles/<int:pid>/delete")
@admin_required
def admin_delete_profile(pid):
    require_csrf()
    _row_or_404("SELECT id FROM profiles WHERE id = ?", (pid,))
    if query_one("SELECT 1 FROM users WHERE profile_id = ?", (pid,)):
        return render_profiles(error="Move users to another profile before deleting this one.",
                               status=400)
    execute("DELETE FROM profiles WHERE id = ?", (pid,))
    return redirect(url_for("admin_profiles"))


@app.post("/admin/profiles/<int:pid>/domains")
@admin_required
def admin_add_domain(pid):
    require_csrf()
    _row_or_404("SELECT id FROM profiles WHERE id = ?", (pid,))
    name = clean_text(request.form.get("name"))
    color = request.form.get("color", "")
    if not name or color not in COLOR_CHOICES:
        return render_profile(pid, error="Enter a domain name and choose a colour.", status=400)
    execute("INSERT INTO domains (profile_id, name, color, position) VALUES (?, ?, ?, ?)",
            (pid, name, color, next_position("domains", "profile_id", pid)))
    return redirect(url_for("admin_profile", pid=pid))


@app.post("/admin/domains/<int:did>/delete")
@admin_required
def admin_delete_domain(did):
    require_csrf()
    row = _row_or_404("SELECT profile_id FROM domains WHERE id = ?", (did,))
    execute("DELETE FROM domains WHERE id = ?", (did,))
    return redirect(url_for("admin_profile", pid=row["profile_id"]))


@app.post("/admin/domains/<int:did>/subdomains")
@admin_required
def admin_add_subdomain(did):
    require_csrf()
    row = _row_or_404("SELECT profile_id FROM domains WHERE id = ?", (did,))
    name = clean_text(request.form.get("name"))
    if not name:
        return render_profile(row["profile_id"], error="Enter a sub-domain name.", status=400)
    execute("INSERT INTO subdomains (domain_id, name, position) VALUES (?, ?, ?)",
            (did, name, next_position("subdomains", "domain_id", did)))
    return redirect(url_for("admin_profile", pid=row["profile_id"]))


@app.post("/admin/subdomains/<int:sid>/delete")
@admin_required
def admin_delete_subdomain(sid):
    require_csrf()
    row = _row_or_404(
        """SELECT d.profile_id FROM subdomains s JOIN domains d ON d.id = s.domain_id
           WHERE s.id = ?""", (sid,))
    execute("DELETE FROM subdomains WHERE id = ?", (sid,))
    return redirect(url_for("admin_profile", pid=row["profile_id"]))


@app.post("/admin/subdomains/<int:sid>/topics")
@admin_required
def admin_add_topics(sid):
    require_csrf()
    row = _row_or_404(
        """SELECT d.profile_id FROM subdomains s JOIN domains d ON d.id = s.domain_id
           WHERE s.id = ?""", (sid,))
    names = []
    for line in (request.form.get("topics", "") or "").splitlines():
        name = clean_text(line, max_len=120)
        if name and name not in names:
            names.append(name)
    names = names[:40]
    if not names:
        return render_profile(row["profile_id"], error="Enter at least one topic.", status=400)
    existing = {r["name"] for r in query_all("SELECT name FROM topics WHERE subdomain_id = ?", (sid,))}
    position = next_position("topics", "subdomain_id", sid)
    conn = get_db()
    try:
        for name in names:
            if name in existing:
                continue
            conn.execute("INSERT INTO topics (subdomain_id, name, position) VALUES (?, ?, ?)",
                         (sid, name, position))
            position += 1
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin_profile", pid=row["profile_id"]))


@app.post("/admin/topics/<int:tid>/delete")
@admin_required
def admin_delete_topic(tid):
    require_csrf()
    row = _row_or_404(
        """SELECT d.profile_id FROM topics t
           JOIN subdomains s ON s.id = t.subdomain_id
           JOIN domains d ON d.id = s.domain_id
           WHERE t.id = ?""", (tid,))
    execute("DELETE FROM topics WHERE id = ?", (tid,))
    return redirect(url_for("admin_profile", pid=row["profile_id"]))


# ---------- admin command-line tools (run from the project folder) ----------

def _get_user_or_fail(email):
    addr = normalise_email(email)
    if addr is None:
        raise click.ClickException(f"Email must end with @{ALLOWED_DOMAIN}.")
    user = query_one("SELECT * FROM users WHERE email = ?", (addr,))
    if user is None:
        raise click.ClickException(f"No user {addr}.")
    return addr


@app.cli.command("create-user")
@click.argument("email")
@click.option("--role", type=click.Choice(["user", "admin"]), default="user", show_default=True)
@click.option("--profile", "profile_name", default=None,
              help="Profile name. Defaults to the first profile.")
def create_user(email, role, profile_name):
    """Create a login for an office email and print a one-time temporary password."""
    addr = normalise_email(email)
    if addr is None:
        raise click.ClickException(f"Email must end with @{ALLOWED_DOMAIN}.")
    if query_one("SELECT 1 FROM users WHERE email = ?", (addr,)):
        raise click.ClickException("User already exists. Use reset-password instead.")
    if profile_name:
        prof = query_one("SELECT id FROM profiles WHERE name = ?", (profile_name,))
        if prof is None:
            raise click.ClickException(f"No profile named '{profile_name}'.")
    elif role == "user":
        prof = query_one("SELECT id FROM profiles ORDER BY id LIMIT 1")
    else:
        prof = None  # admins do not need a profile unless one is named
    if prof is None and role == "user":
        raise click.ClickException("No competency profile exists. Create one in the admin console first.")
    temp = _temp_password()
    execute(
        "INSERT INTO users (email, password_hash, role, profile_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (addr, generate_password_hash(temp), role, prof["id"] if prof else None, now_iso()),
    )
    click.echo(f"Created {addr} ({role}).")
    click.echo(f"Temporary password (shown once): {temp}")
    click.echo("The user must change it at first sign-in.")


@app.cli.command("reset-password")
@click.argument("email")
def reset_password(email):
    """Issue a new temporary password, unlock the account and sign the user out everywhere."""
    addr = _get_user_or_fail(email)
    temp = _temp_password()
    execute(
        """UPDATE users SET password_hash = ?, must_change_password = 1, token_version = token_version + 1,
           failed_attempts = 0, locked_until = 0 WHERE email = ?""",
        (generate_password_hash(temp), addr),
    )
    click.echo(f"Temporary password for {addr} (shown once): {temp}")


def _set_active(email, active):
    addr = _get_user_or_fail(email)
    execute("UPDATE users SET active = ?, token_version = token_version + 1 WHERE email = ?",
            (1 if active else 0, addr))
    click.echo(f"{addr} is now {'enabled' if active else 'disabled'}.")


@app.cli.command("disable-user")
@click.argument("email")
def disable_user(email):
    """Block sign-in and end existing sessions for this user."""
    _set_active(email, False)


@app.cli.command("enable-user")
@click.argument("email")
def enable_user(email):
    """Allow a disabled user to sign in again."""
    _set_active(email, True)


def validate_profile_spec(spec):
    """Return an error message for a bad profile spec, or None if it is valid."""
    if not isinstance(spec, dict):
        return "The file must contain a JSON object."
    if not clean_text(spec.get("name")):
        return "The profile needs a name."
    domains = spec.get("domains")
    if not isinstance(domains, list) or not domains:
        return "The profile needs at least one domain."
    for dom in domains:
        if not isinstance(dom, dict) or not clean_text(dom.get("name")):
            return "Every domain needs a name."
        if dom.get("color") not in COLOR_CHOICES:
            return f"Domain '{dom.get('name')}': colour must be one of {', '.join(COLOR_CHOICES)}."
        subs = dom.get("subdomains", [])
        if not isinstance(subs, list):
            return f"Domain '{dom.get('name')}': subdomains must be a list."
        for sub in subs:
            if not isinstance(sub, dict) or not clean_text(sub.get("name")):
                return f"Domain '{dom.get('name')}': every sub-domain needs a name."
            topics = sub.get("topics", [])
            if not isinstance(topics, list) or not all(isinstance(t, str) and t.strip() for t in topics):
                return f"Sub-domain '{sub.get('name')}': topics must be a list of non-empty text."
    return None


@app.cli.command("import-profile")
@click.argument("json_file", type=click.Path(exists=True, dir_okay=False))
def import_profile(json_file):
    """Create a competency profile from a JSON file (see sample_profile.json)."""
    import json

    try:
        spec = json.loads(Path(json_file).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid JSON: {exc}")
    error = validate_profile_spec(spec)
    if error:
        raise click.ClickException(error)
    name = clean_text(spec["name"])
    if query_one("SELECT 1 FROM profiles WHERE name = ?", (name,)):
        raise click.ClickException(f"A profile named '{name}' already exists.")

    # Normalise names before inserting.
    for dom in spec["domains"]:
        dom["name"] = clean_text(dom["name"])
        for sub in dom.get("subdomains", []):
            sub["name"] = clean_text(sub["name"])
            sub["topics"] = [clean_text(t, max_len=120) for t in sub.get("topics", [])]
    spec["name"] = name

    conn = get_db()
    try:
        _insert_profile_tree(conn, spec)
        conn.commit()
    finally:
        conn.close()
    n_subs = sum(len(d.get("subdomains", [])) for d in spec["domains"])
    n_topics = sum(len(s.get("topics", [])) for d in spec["domains"] for s in d.get("subdomains", []))
    click.echo(f"Imported '{name}': {len(spec['domains'])} domains, {n_subs} sub-domains, {n_topics} topics.")


@app.cli.command("set-profile")
@click.argument("email")
@click.argument("profile_name")
def set_profile(email, profile_name):
    """Assign a competency profile to a user, e.g. set-profile neeraj.arora@impressico.com "Data Engineer Coaching"."""
    addr = _get_user_or_fail(email)
    prof = query_one("SELECT id FROM profiles WHERE name = ?", (profile_name,))
    if prof is None:
        raise click.ClickException(f"No profile named '{profile_name}'.")
    execute("UPDATE users SET profile_id = ? WHERE email = ?", (prof["id"], addr))
    click.echo(f"{addr} is now on '{profile_name}'.")


@app.cli.command("list-users")
def list_users():
    """List all accounts."""
    rows = query_all(
        """SELECT u.email, u.role, u.active, u.must_change_password, p.name AS profile_name
           FROM users u LEFT JOIN profiles p ON p.id = u.profile_id ORDER BY u.email""")
    if not rows:
        click.echo("No users yet.")
    for row in rows:
        click.echo(
            f"{row['email']:<40} {row['role']:<6} "
            f"{'active' if row['active'] else 'disabled':<9} "
            f"{(row['profile_name'] or '-'):<28} "
            f"{'must change password' if row['must_change_password'] else ''}"
        )


init_db()

if __name__ == "__main__":
    app.run(debug=True)
