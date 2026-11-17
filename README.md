# Competency Tracker

A small web app where people record their status (Not rated, Weak, Learning, Practising, Confident) on each topic of a competency profile. Admins create accounts, manage profiles, and can view any employee's status (read-only).

See [GUIDE.md](GUIDE.md) for accounts, profiles and every command. Account passwords are in [PASSWORDS.md](PASSWORDS.md), which is excluded from git.

## Requirements

Choose one:

- **Docker Desktop** (recommended). Runs the app in a container, with no Python setup needed.
- **Python 3.11** on Windows, macOS or Linux, to run the app directly.

Google Chrome is only needed for the browser tests.

## Option 1: Run with Docker (recommended)

1. Create the settings file once. Copy `.env.example` to `.env`, then set `JWT_SECRET` to a long random value:

   ```bash
   cd "C:\Users\ADMIN\Documents\ai enablement\11-17-2026\competency-tracker"
   copy .env.example .env
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Paste the printed value after `JWT_SECRET=` in `.env`.

2. Build and start:

   ```bash
   docker compose up -d --build
   ```

3. Open **http://127.0.0.1:5000** in your browser.

The database is stored in the Docker volume `tracker-data`, so it survives restarts and rebuilds. The container restarts automatically unless you stop it.

Common commands:

```bash
docker compose ps                                                # is it running and healthy?
docker compose logs -f tracker                                   # follow the server log
docker compose exec tracker python manage.py list-users          # run any manage.py command
docker compose stop                                              # stop (keeps data)
docker compose start                                             # start again
docker compose down                                              # remove the container (keeps data)
docker compose down -v                                           # remove the container AND DELETE all data
```

To move your existing `tracker.db` into the container's volume, stop the container first and copy the file in:

```bash
docker compose down
docker run --rm -v competency-tracker_tracker-data:/data -v "${PWD}:/src" alpine cp /src/tracker.db /data/tracker.db
docker compose up -d
```

## Option 2: Run with Python

```bash
cd "C:\Users\ADMIN\Documents\ai enablement\11-17-2026\competency-tracker"
python -m pip install -r requirements-dev.txt
python app.py
```

Open **http://127.0.0.1:5000**. Keep the terminal open while you use the app. Press `Ctrl+C` to stop it.

The first run creates `tracker.db` (the database) and `.jwt_secret` (the key used to sign login tokens). Do not share or commit `.jwt_secret`.

## Settings

Set these in `.env` for Docker, or as environment variables for Python (for example `$env:COOKIE_SECURE = "1"` in PowerShell).

| Variable | Default | Purpose |
|---|---|---|
| `JWT_SECRET` | value in `.jwt_secret` (Python only) | Signing key for login tokens. **Required for Docker.** |
| `ALLOWED_EMAIL_DOMAIN` | `impressico.com` | Only emails ending in this domain can have accounts |
| `TOKEN_HOURS` | `8` | How long a sign-in lasts |
| `COOKIE_SECURE` | `0` | Set to `1` when the site is reached over https (for example a tunnel link) |
| `TRACKER_DB` | `tracker.db` next to `app.py` | Database file path (set to `/data/tracker.db` in Docker) |

## Project layout

| Path | What it is |
|---|---|
| `app.py` | The whole application: routes, login, admin pages, command-line commands |
| `manage.py` | Command-line entry point (`python manage.py <command>`) |
| `templates/` | HTML pages |
| `static/style.css` | Styling |
| `sample_profile.json` | Example profile for `import-profile` |
| `claude_automation_analyst_profile.json` | The 14-competency Claude Automation Analyst profile |
| `Dockerfile` | Builds the production image (gunicorn, non-root user) |
| `docker-compose.yml` | Runs the container on `127.0.0.1:5000` with a data volume |
| `.dockerignore` | Keeps passwords, databases and tests out of the image |
| `.env.example` | Template for the `.env` settings file |
| `.github/workflows/tests.yml` | GitHub Actions: tests, browser tests and a Docker smoke test |
| `tests/test_app.py` | Unit and integration tests (pytest) |
| `tests/e2e/test_browser.py` | Browser tests (Playwright, drives Chrome) |
| `pytest.ini` | Test configuration |
| `requirements.txt` / `requirements-dev.txt` | Python packages |
| `GUIDE.md` / `PASSWORDS.md` | User guide, and the local-only account passwords |
| `tracker.db` | SQLite database when running with Python (created on first run) |

## Sharing with a colleague (temporary)

Tunnels such as Cloudflare Tunnel or ngrok give a public link to your laptop. With the app running (Docker or Python):

```bash
cloudflared tunnel --url http://127.0.0.1:5000
```

Send the printed link only to people you trust. Set `COOKIE_SECURE=1` in `.env` first, because the link uses https. Stop the tunnel when you are done.

## Run the tests

```bash
cd "C:\Users\ADMIN\Documents\ai enablement\11-17-2026\competency-tracker"
python -m pytest tests
python -m pytest tests/e2e
```

The browser tests open a visible Chrome window. To run them without a window, set `$env:HEADLESS = "1"` first. The tests use temporary databases, so your database is not touched.

## Continuous integration (GitHub Actions)

The workflow in `.github/workflows/tests.yml` runs on every push to `main` and on every pull request:

1. **Unit and integration tests** (pytest).
2. **Browser tests** (Playwright, Chromium on Ubuntu), after job 1 passes.
3. **Docker build and smoke test**: builds the image with `docker compose build`, starts it, checks that the sign-in page responds, then stops it. Runs after job 1 passes.

Results are at https://github.com/neeraj975arora/competency-tracker/actions.
