#!/bin/sh
# Installs pg_dump and pg_restore of the same major version as the db service (docker-compose.yml).
# Debian ships an older major version, and pg_dump refuses a newer server, so they come from the
# PostgreSQL project's own apt repository (https://www.postgresql.org/download/linux/debian/).
# Used by the Dockerfile and the dev container (.devcontainer/devcontainer.json). Runs as root.
set -eu

# Keep equal to the major version of the db image; tests/unit/test_backup_tooling.py checks it.
POSTGRES_MAJOR=18
KEY=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc

# python3 instead of curl: the slim image has no curl, and both images have python3.
install -d "$(dirname "$KEY")"
python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc "$KEY"
. /etc/os-release
echo "deb [signed-by=$KEY] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list

apt-get update
apt-get install -y --no-install-recommends "postgresql-client-$POSTGRES_MAJOR"
rm -rf /var/lib/apt/lists/*
