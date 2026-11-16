"""Operator overview on the terminal - the SSH-side twin of the bot's /stats.

    docker compose run --rm api python -m scripts.admin_stats          # last 7 days
    docker compose run --rm api python -m scripts.admin_stats 30       # last 30 days
    docker compose run --rm api python -m scripts.admin_stats 30 50    # ...and 50 recent signups

Prints subscriber emails and street addresses, so it is SSH-only by design - there is no HTTP
route to this data anywhere in the project. See core/admin_stats.py for why.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.admin_stats import collect, format_summary


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    days = int(argv[0]) if argv and argv[0].isdigit() else 7
    recent = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 25
    print(format_summary(collect(days=max(1, days), recent_limit=max(1, recent))))


if __name__ == "__main__":
    main()
