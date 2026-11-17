"""End-to-end browser tests (Playwright + Google Chrome) for the Competency Tracker.

The app runs on a local port with a temporary database, so tracker.db is untouched.
By default Chrome opens visibly so you can watch the run.
  Visible:   python -m pytest tests/e2e
  Headless:  set HEADLESS=1 first (PowerShell: $env:HEADLESS = "1")
"""

import os
import re
import threading

import pytest
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

import app as tracker

HEADLESS = os.environ.get("HEADLESS") == "1"
# Uses the Chrome installed on this machine; set BROWSER_CHANNEL=msedge for Edge, or "" for Playwright's Chromium.
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "chrome") or None
SLOW_MO = 0 if HEADLESS else 400  # milliseconds between actions so the run is easy to follow
NEW_PASSWORD = "Browser-Pass-2026"
PROFILE_NAME = "Claude Automation Analyst"


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """Start the Flask app on a free local port with its own database and seed accounts."""
    original_db = tracker.DB_PATH
    tracker.DB_PATH = tmp_path_factory.mktemp("e2e") / "e2e.db"
    tracker.init_db()

    runner = tracker.app.test_cli_runner()
    temps = {}
    for email, role in [("staff.one@impressico.com", "user"),
                        ("staff.two@impressico.com", "user"),
                        ("boss@impressico.com", "admin")]:
        result = runner.invoke(args=["create-user", email, "--role", role])
        assert result.exit_code == 0, result.output
        temps[email] = re.search(r"shown once\): (\S+)", result.output).group(1)

    server = make_server("127.0.0.1", 0, tracker.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    yield {"base_url": base_url, "temps": temps}

    server.shutdown()
    tracker.DB_PATH = original_db


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=BROWSER_CHANNEL, headless=HEADLESS, slow_mo=SLOW_MO)
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    page = context.new_page()
    yield page
    context.close()


def sign_in(page, base_url, email, password):
    page.goto(f"{base_url}/login")
    page.fill("#email", email)
    page.fill("#password", password)
    page.get_by_role("button", name="Sign in").click()


def set_new_password(page, temp):
    page.fill("#current_password", temp)
    page.fill("#new_password", NEW_PASSWORD)
    page.fill("#confirm_password", NEW_PASSWORD)
    page.get_by_role("button", name="Save password").click()
    expect(page.get_by_text("Your password has been changed.")).to_be_visible()


def test_wrong_password_shows_error(page, live_server):
    sign_in(page, live_server["base_url"], "staff.one@impressico.com", "not-the-password")
    expect(page.get_by_text("Invalid email or password.")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/login$"))


def test_first_sign_in_forces_password_change(page, live_server):
    email = "staff.one@impressico.com"
    sign_in(page, live_server["base_url"], email, live_server["temps"][email])
    expect(page).to_have_url(re.compile(r"/account/password$"))
    expect(page.get_by_role("heading", name="Set your new password")).to_be_visible()
    set_new_password(page, live_server["temps"][email])
    expect(page.get_by_text(f"Signed in as {email}")).to_be_visible()


def test_save_status_and_see_weak_area(page, live_server):
    email = "staff.one@impressico.com"
    sign_in(page, live_server["base_url"], email, NEW_PASSWORD)
    expect(page.get_by_text(f"Signed in as {email}")).to_be_visible()
    expect(page.get_by_text(f"Profile: {PROFILE_NAME}")).to_be_visible()

    page.get_by_label("Status for Python: Lists").select_option("Weak")
    page.get_by_label("Notes for Python: Lists").fill("Practise list slicing")
    page.get_by_role("button", name="Save my status").click()

    expect(page.get_by_text("Your status has been saved.")).to_be_visible()
    expect(page.get_by_label("Status for Python: Lists")).to_have_value("Weak")
    expect(page.get_by_label("Notes for Python: Lists")).to_have_value("Practise list slicing")
    expect(page.get_by_text("Weak areas (1)")).to_be_visible()
    expect(page.locator(".weak li", has_text="Python: Lists")).to_be_visible()


def test_sign_out_then_tracker_requires_login(page, live_server):
    email = "staff.one@impressico.com"
    sign_in(page, live_server["base_url"], email, NEW_PASSWORD)
    expect(page.get_by_text(f"Signed in as {email}")).to_be_visible()

    page.get_by_role("button", name="Sign out").click()
    expect(page).to_have_url(re.compile(r"/login$"))

    page.goto(f"{live_server['base_url']}/")
    expect(page).to_have_url(re.compile(r"/login$"))


def test_employees_cannot_see_each_other(browser, live_server):
    base = live_server["base_url"]
    temp = live_server["temps"]["staff.two@impressico.com"]
    context = browser.new_context()
    page = context.new_page()
    try:
        sign_in(page, base, "staff.two@impressico.com", temp)
        set_new_password(page, temp)

        page.goto(f"{base}/?person=staff.one@impressico.com")
        expect(page.get_by_text("(read-only)")).to_have_count(0)
        expect(page.get_by_role("button", name="Save my status")).to_be_visible()
        expect(page.get_by_label("Status for Python: Lists")).to_have_value("Not rated")
    finally:
        context.close()


def test_admin_views_employee_read_only(browser, live_server):
    base = live_server["base_url"]
    context = browser.new_context()
    page = context.new_page()
    try:
        boss = "boss@impressico.com"
        sign_in(page, base, boss, live_server["temps"][boss])
        set_new_password(page, live_server["temps"][boss])

        page.fill("#person", "staff.one@impressico.com")
        page.get_by_role("button", name="View").click()
        expect(page.get_by_text("Viewing staff.one@impressico.com (read-only)")).to_be_visible()
        expect(page.get_by_label("Status for Python: Lists")).to_be_disabled()
        expect(page.get_by_role("button", name="Save my status")).to_have_count(0)
        expect(page.get_by_label("Notes for Python: Lists")).to_have_value("Practise list slicing")
    finally:
        context.close()


def test_admin_adds_user_in_browser_and_new_user_signs_in(browser, live_server):
    base = live_server["base_url"]
    new_email = "new.hire@impressico.com"
    admin_context = browser.new_context()
    admin_page = admin_context.new_page()
    new_context = browser.new_context()
    new_page = new_context.new_page()
    try:
        sign_in(admin_page, base, "boss@impressico.com", NEW_PASSWORD)
        admin_page.goto(f"{base}/admin")
        admin_page.get_by_label("Office email").fill(new_email)
        admin_page.get_by_label("Competency profile").select_option(label=PROFILE_NAME)
        admin_page.get_by_role("button", name="Create account").click()

        expect(admin_page.get_by_text("Account created")).to_be_visible()
        temp = admin_page.locator(".secret").inner_text().strip()

        sign_in(new_page, base, new_email, temp)
        expect(new_page).to_have_url(re.compile(r"/account/password$"))
    finally:
        admin_context.close()
        new_context.close()
