#!/bin/sh
# Nightly database backup for the server. Traders' books live only in this database, so
# run this from cron and copy the files off the server regularly.
#
#   crontab -e   →   30 2 * * * cd /path/to/Book-Keeper && ./scripts/backup.sh >> backups/backup.log 2>&1
#
# Restore:  gunzip -c backups/bookkeeper-YYYY-MM-DD.sql.gz | docker compose exec -T postgres psql -U "$POSTGRES_USER" "$POSTGRES_DB"
set -eu
cd "$(dirname "$0")/.."
. ./.env
mkdir -p backups
file="backups/bookkeeper-$(date +%F).sql.gz"
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$file"
echo "$(date -Is) wrote $file ($(du -h "$file" | cut -f1))"
# keep 30 days on the server
find backups -name 'bookkeeper-*.sql.gz' -mtime +30 -delete
