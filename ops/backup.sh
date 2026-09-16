#!/usr/bin/env bash
#
# Daily backup of everything a user would not forgive us for losing.
#
# Two stores, because the data is in two places and losing either one alone is
# still a disaster:
#
#   postgres  - accounts, profiles, opportunities, drafts, notifications.
#               Dumped with pg_dump in custom format, which restores with
#               pg_restore and compresses as it goes.
#   minio     - the uploaded documents themselves: tax clearance certificates,
#               CVs, national IDs, CR14s. A database restored without these
#               leaves every user unable to apply for anything.
#
# Deliberately boring: no cloud, no credentials, no network. It writes to a
# directory on the same box, which protects against the failure that actually
# happens to small deployments - a bad migration, a wrong DELETE, a container
# recreated with the wrong volume - and not against the box burning down.
# Copying these off-site is the next step and needs somewhere to copy them to.
#
# Install:
#   crontab -e
#   17 2 * * * /home/isaiah/apps/OpportunityAI/ops/backup.sh >> /home/isaiah/backups/opportunityai/backup.log 2>&1
#
# Restore the database:
#   docker compose exec -T db pg_restore -U opportunity -d opportunity_agent --clean < db-YYYYmmdd-HHMM.dump
#
# Restore the documents:
#   docker run --rm -v opportunityai_minio-data:/data -v "$PWD":/backup alpine \
#     sh -c 'rm -rf /data/* && tar xzf /backup/documents-YYYYmmdd.tar.gz -C /data'

set -euo pipefail

APP_DIR="${APP_DIR:-/home/isaiah/apps/OpportunityAI}"
DEST="${DEST:-/home/isaiah/backups/opportunityai}"
KEEP_DB_DAYS="${KEEP_DB_DAYS:-14}"
KEEP_DOC_DAYS="${KEEP_DOC_DAYS:-7}"

stamp="$(date +%Y%m%d-%H%M)"
mkdir -p "$DEST"
cd "$APP_DIR"

echo "=== $(date -Is) backup starting ==="

# --- database -------------------------------------------------------------
# Written to a .part file and renamed only on success, so a dump interrupted
# half way through is never mistaken for a usable backup by whoever is
# restoring at 3am.
db_file="$DEST/db-$stamp.dump"
docker compose exec -T db pg_dump -U opportunity -Fc opportunity_agent > "$db_file.part"
mv "$db_file.part" "$db_file"
echo "database: $(du -h "$db_file" | cut -f1)"

# --- uploaded documents ---------------------------------------------------
# Read straight off the volume rather than through the MinIO API: no keys
# needed, and it captures the bucket exactly as it is on disk.
doc_file="$DEST/documents-$stamp.tar.gz"
docker run --rm \
  -v opportunityai_minio-data:/data:ro \
  -v "$DEST":/backup \
  alpine tar czf "/backup/$(basename "$doc_file").part" -C /data .
mv "$doc_file.part" "$doc_file"
echo "documents: $(du -h "$doc_file" | cut -f1)"

# --- a backup nobody checks is not a backup -------------------------------
# Verify the dump is readable before old ones are deleted. pg_restore -l reads
# the table of contents without touching the database, so a corrupt or
# truncated file fails here rather than on the day it is needed.
if docker compose exec -T db pg_restore -l "/dev/stdin" < "$db_file" > /dev/null 2>&1; then
    echo "verified: the dump's table of contents reads back"
else
    echo "WARNING: the dump could not be read back - keeping every old backup"
    exit 1
fi

# --- rotate ---------------------------------------------------------------
find "$DEST" -name 'db-*.dump' -mtime "+$KEEP_DB_DAYS" -delete
find "$DEST" -name 'documents-*.tar.gz' -mtime "+$KEEP_DOC_DAYS" -delete
find "$DEST" -name '*.part' -mtime +1 -delete

echo "kept: $(find "$DEST" -name 'db-*.dump' | wc -l) database, $(find "$DEST" -name 'documents-*.tar.gz' | wc -l) document backups"
echo "=== $(date -Is) backup finished ==="
