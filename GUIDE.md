# Competency Tracker - User Guide

Run every command from the project folder:

```bash
cd "C:\Users\ADMIN\Documents\ai enablement\11-17-2026\competency-tracker"
```

Open the app at **http://127.0.0.1:5000** after starting it with `python app.py`.

## 1. Accounts and passwords

Passwords are kept in a separate local file, [PASSWORDS.md](PASSWORDS.md), which is excluded from git. Do not copy them into shared documents.

| Email (user ID) | Role | Profile | Password | Notes |
|---|---|---|---|---|
| `neeraj.arora@impressico.com` | Admin | Claude Automation Analyst (14 competencies) | ***** | Admin account. Can add users and view everyone's status. |
| `tester@impressico.com` | User | Claude Automation Analyst (14 competencies) | ***** | Test account for manual checks. |
| `learner@impressico.com` | User | Data Engineer Coaching | ***** | Temporary password. Must be changed at first sign-in. |
| `demo.learner@impressico.com` | User | Data Engineer Coaching | ***** | Temporary password. Must be changed at first sign-in. |
| `demo.user@impressico.com` | User | Claude Automation Analyst (14 competencies) | ***** | **Disabled.** Cannot sign in until re-enabled. |

Temporary passwords only work until the user changes them. If a temporary password no longer works, reset it (section 4).

Change these passwords, or disable these accounts, before sharing the app with anyone else.

Check the current accounts at any time:

```bash
python manage.py list-users
```

## 2. Add new users

### From the browser (admin)

1. Sign in as an admin, then open **Admin**.
2. Under **Add user**, enter the office email, choose **User** or **Admin**, and choose a **Competency profile**.
3. Click **Create account**.
4. The temporary password is shown **once** on the page. Copy it and send it to the person securely.
5. The person signs in with that password, then must set their own password (at least 10 characters).

Only emails ending in `@impressico.com` are accepted.

### From the command line

```bash
python manage.py create-user name@impressico.com
python manage.py create-user name@impressico.com --profile "Data Engineer Coaching"
python manage.py create-user boss@impressico.com --role admin
```

- Without `--profile`, a user gets the first profile in the list.
- Admins do not need a profile.
- The temporary password is printed once.

## 3. Switch a user's profile

Each user works on one profile at a time. Switching a profile only changes which topics the user sees. Their existing ratings for other profiles are kept.

**In the browser (admin):** **Admin** → find the user → choose a profile in their row → **Save**.

**From the command line:**

```bash
python manage.py set-profile name@impressico.com "Data Engineer Coaching"
```

Profile names must match exactly, and are case-sensitive. Use the quotes if the name has spaces or brackets.

## 4. Manage accounts

```bash
python manage.py reset-password name@impressico.com     # new temporary password, unlocks the account, signs the user out
python manage.py disable-user name@impressico.com       # blocks sign-in and ends current sessions
python manage.py enable-user name@impressico.com        # allows sign-in again
python manage.py list-users                             # all accounts, role, status, profile
```

In the browser, the same actions are on the **Admin** page: **Reset password**, **Disable** and **Enable**. Admins cannot disable their own account.

## 5. Competency profiles

A profile has **domains** (each with a colour), **sub-domains** and **topics**. People rate each topic.

Colours: `blue`, `orange`, `teal`, `purple`, `green`, `rose`.

### Create or edit in the browser (admin)

1. Open **Profiles** and enter a name, then **Create profile**.
2. Add a domain with a colour.
3. Under the domain, add sub-domains.
4. Under a sub-domain, type topics one per line, then **Add topics**.
5. **Remove** deletes a single topic. **Delete domain** and **Delete sub-domain** remove everything inside them.

### Import from a JSON file

```bash
python manage.py import-profile sample_profile.json
python manage.py import-profile claude_automation_analyst_profile.json
```

The file format:

```json
{
  "name": "My Profile",
  "domains": [
    {
      "name": "Coding",
      "color": "blue",
      "subdomains": [
        {"name": "Python", "topics": ["Lists", "Dictionaries"]}
      ]
    }
  ]
}
```

The import refuses a name that already exists, and an unknown colour.

### Delete a profile

A profile can only be deleted when no users are assigned to it. Move those users to another profile first (section 3), then delete it in the browser under **Profiles** → **Delete this profile**.

### Current profiles

- **Claude Automation Analyst (14 competencies):** three domains (A. Claude Core, B. Consulting Craft, C. Build and Run) with the 14 competencies as topics.
- **Data Engineer Coaching:** Python, SQL Server and PySpark, plus Analytical Thinking and Communication.

## 6. Command reference

| Command | What it does |
|---|---|
| `python app.py` | Starts the web app on http://127.0.0.1:5000 |
| `python manage.py create-user EMAIL [--role user\|admin] [--profile "NAME"]` | Creates an account with a one-time temporary password |
| `python manage.py set-profile EMAIL "PROFILE NAME"` | Assigns a profile to a user |
| `python manage.py import-profile FILE.json` | Creates a profile from a JSON file |
| `python manage.py reset-password EMAIL` | Issues a new temporary password and signs the user out |
| `python manage.py disable-user EMAIL` | Blocks sign-in |
| `python manage.py enable-user EMAIL` | Restores sign-in |
| `python manage.py list-users` | Lists all accounts |
| `python manage.py routes` | Lists the web addresses the app serves |
| `python -m pytest tests` | Runs the unit and integration tests |
| `python -m pytest tests/e2e` | Runs the browser tests (opens Chrome) |
| `python -m pip install -r requirements-dev.txt` | Installs the required Python packages |

## 7. Troubleshooting

- **"No competency profile is assigned to you yet."** The account has no profile. Assign one (section 3).
- **"Invalid email or password."** Check the email ends in `@impressico.com` and the password is exact. Five wrong attempts lock the account for 15 minutes.
- **"Too many failed attempts."** Wait 15 minutes, or reset the password (section 4).
- **"Form expired. Please reload the page."** Reload the page and submit again.
- **"ModuleNotFoundError: No module named 'jwt'"** Run `python -m pip install -r requirements-dev.txt` and start again.
- **Site can't be reached.** `python app.py` is not running. Start it in the project folder.
- **`flask` is not recognised.** Use `python manage.py` instead of `flask`.
