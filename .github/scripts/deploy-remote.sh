#!/usr/bin/env bash
# Runs ON the production server, piped in over SSH by deploy.yml.
#
# A real file rather than a heredoc inside the workflow YAML, because the
# first version was a heredoc and it never ran: the terminator has to sit at
# column 0, YAML indents everything inside `run: |`, and `<<-` only strips
# tabs. The step failed with a shell syntax error before touching the server.
# A file also means this can be shell-checked and run by hand, which is how
# it got verified before being trusted to a push.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/apps/OpportunityAI}"
cd "$APP_DIR"

echo "--- pulling ---"
git pull --ff-only

echo "--- rebuilding ---"
docker compose up -d --build

# searxng's settings.yml is a bind-mounted file, so `compose up -d` does not
# recreate the container when only that file changed - an engine change would
# deploy to disk and nowhere else. This was a real miss during a manual deploy.
echo "--- restarting searxng for bind-mounted config ---"
docker compose restart searxng

echo "--- containers ---"
docker compose ps

# A deploy that leaves the site down must fail loudly rather than report
# success because ssh happened to exit 0.
echo "--- waiting for the API ---"
for _ in $(seq 1 20); do
  code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health || true)"
  if [ "$code" = "200" ]; then
    echo "health: 200"
    exit 0
  fi
  sleep 3
done

echo "health check never returned 200" >&2
docker compose logs api --tail 40 >&2
exit 1
