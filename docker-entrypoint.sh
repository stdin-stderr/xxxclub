#!/bin/sh
set -eu

role="${1:-scraper}"

case "$role" in
    scraper)
        shift
        set -- python3 /app/entrypoint.py "$@"
        ;;
    web)
        shift
        set -- python3 /app/webapp.py "$@"
        ;;
    tpdb-matcher)
        shift
        set -- python3 /app/tpdb_matcher.py "$@"
        ;;
esac

exec "$@"
