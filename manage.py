"""Command-line entry point for the Competency Tracker.

Works without `flask` being on PATH:
    python manage.py create-user name@impressico.com --role user
    python manage.py import-profile sample_profile.json
    python manage.py list-users
"""

from flask.cli import FlaskGroup

from app import app

cli = FlaskGroup(create_app=lambda: app)

if __name__ == "__main__":
    cli()
