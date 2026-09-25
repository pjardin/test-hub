#!/usr/bin/env python3
"""Django management entrypoint for the Test Hub.

Day-to-day commands:

    python manage.py serve       # THE way to run it: web UI + runner + scheduler
    python manage.py sync        # re-populate the DB from the tests/ folder
    python manage.py runtest ID  # run one test from the CLI (no server needed)
    python manage.py migrate     # apply DB schema (installer runs this for you)
"""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testhub.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django is not importable. On a bundle machine run "
            "'source /opt/pw-offline/env.sh' first (or reinstall the app zip); "
            "for local development run app/dev.sh."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
