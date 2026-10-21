#!/bin/sh
# Starts the agent and the dashboard. The image is rebuilt when .env or the keystore password changed: the build
# puts the secrets from .env into an encrypted keystore in the image (your .env stays as it is).
# The running agent keeps working until the new image replaces it, so changing the password is safe.
# The evals image is rebuilt too, so `docker compose run --rm evals` always has the same secrets.
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
mkdir -p output

placeholder=CHANGE_ME
[ -f keystore_password ] || printf '%s' "$placeholder" > keystore_password
password="$(tr -d '\r\n' < keystore_password)"
if [ "$password" = "$placeholder" ] || [ ${#password} -lt 8 ]; then
    echo "Set your keystore password first: open ./keystore_password, replace $placeholder with a password"
    echo "of at least 8 characters, save it, and run ./start.sh again. (The file is never committed.)"
    exit 1
fi

if command -v sha256sum >/dev/null; then hash=sha256sum; else hash="shasum -a 256"; fi
CONFIG_HASH="$(cat .env keystore_password | $hash | cut -c1-16)"
export CONFIG_HASH
docker compose up -d --build
docker compose --profile evals build evals
echo "Running. Dashboard: http://localhost:3000   Logs: docker compose logs -f email-agent"
