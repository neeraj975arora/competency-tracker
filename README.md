# Competency Tracker

A small Flask web app where people record their status (Not rated, Weak, Learning, Practising, Confident) on each topic of a competency profile. Admins create accounts, manage profiles, and can view any employee's status (read-only).

See [GUIDE.md](GUIDE.md) for accounts, passwords, profiles and all commands.

## Requirements

- Windows with Python 3.11 (other platforms work the same way)
- Google Chrome, only if you want to run the browser tests

## Bring up the project

Open PowerShell in the project folder:

```bash
cd "C:\Users\ADMIN\Documents\ai enablement\11-17-2026\competency-tracker"
python -m pip install -r requirements-dev.txt
python app.py
```

Then open **http://127.0.0.1:5000** in your browser. Keep the terminal open while you use the app. Press `Ctrl+C` in that terminal to stop it.

The first run creates `tracker.db` (the database) and `.jwt_secret` (the key used to sign login tokens). Do not share or commit `.jwt_secret`.

## Project layout

| Path | What it is |
|---|---|
| `app.py` | The whole application: routes, login, admin pages, command-line commands |
| `manage.py` | Command-line entry point (`python manage.py <command>`) |
| `templates/` | HTML pages |
| `static/style.css` | Styling |
| `sample_profile.json` | Example profile for `import-profile` |
| `claude_automation_analyst_profile.json` | The 14-competency Claude Automation Analyst profile |
| `tracker.db` | SQLite database (created on first run) |
| `tests/test_app.py` | Unit and integration tests (pytest) |
| `tests/e2e/test_browser.py` | Browser tests (Playwright, drives Chrome) |
| `pytest.ini` | Test configuration |
| `requirements.txt` / `requirements-dev.txt` | Python packages |

## Settings (optional environment variables)

Set these in PowerShell before `python app.py`, for example `$env:COOKIE_SECURE = "1"`.

| Variable | Default | Purpose |
|---|---|---|
| `ALLOWED_EMAIL_DOMAIN` | `impressico.com` | Only emails ending in this domain can have accounts |
| `JWT_SECRET` | value in `.jwt_secret` | Signing key for login tokens |
| `TOKEN_HOURS` | `8` | How long a sign-in lasts |
| `COOKIE_SECURE` | `0` | Set to `1` when the site is reached over https (for example a tunnel link) |

## Sharing with a colleague (temporary)

Tunnels such as Cloudflare Tunnel or ngrok give a public link to your laptop. With the app running in one terminal:

```bash
cloudflared tunnel --url http://127.0.0.1:5000
```

Send the printed link only to people you trust. Set `COOKIE_SECURE=1` first when using the https link. Stop the tunnel when you are done.

## Run the tests

```bash
cd "C:\Users\ADMIN\Documents\ai enablement\11-17-2026\competency-tracker"
python -m pytest tests
python -m pytest tests/e2e
```

The browser tests open a visible Chrome window. To run them without a window, set `$env:HEADLESS = "1"` first. The tests use temporary databases, so `tracker.db` is not touched.

## Run with Docker

Requires Docker Desktop. Create the `.env` file once (see `.env.example`), then:

```bash
cd "C:\Users\ADMIN\Documents\ai enablement\11-17-2026\competency-tracker"
docker compose up -d --build
```

Open **http://127.0.0.1:5000**. The database is stored in the Docker volume `tracker-data`, so it survives restarts and rebuilds.

Useful commands:

```bash
docker compose logs -f tracker                                   # follow the server log
docker compose exec tracker python manage.py list-users          # run any manage.py command
docker compose exec tracker python manage.py create-user name@impressico.com --profile "Data Engineer Coaching"
docker compose down                                              # stop (keeps data)
docker compose down -v                                           # stop and DELETE all data
```

To move your existing `tracker.db` into the container's volume, stop the container first and copy the file in:

```bash
docker compose down
docker run --rm -v competency-tracker_tracker-data:/data -v "${PWD}:/src" alpine cp /src/tracker.db /data/tracker.db
docker compose up -d
```

Notes:
- The container listens only on this computer (`127.0.0.1:5000`). Edit `docker-compose.yml` to change that.
- `.env` holds `JWT_SECRET`. Keep it private and never commit it.
- Set `COOKIE_SECURE=1` in `.env` when the site is reached over https.
