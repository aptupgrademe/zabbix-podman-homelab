#!/bin/bash
# Nightly dump of the Zabbix database (cron, e.g. "45 1 * * * $HOME/zabbix/backup.sh").
# The dump is checked with pg_restore --list before it replaces anything; old dumps are kept
# for KEEP_DAYS days. A failure sends a mail to MAIL_TO (needs a working "mail" command).
set -o pipefail; umask 077
D="${BACKUP_DIR:-$HOME/zabbix/backup}"; KEEP_DAYS="${KEEP_DAYS:-14}"; MAIL_TO="${MAIL_TO:-root}"
mkdir -p "$D"; F="$D/zabbix-$(date +%F).dump"
if podman exec zabbix-db pg_dump -U zabbix -Fc zabbix > "$F.tmp" && [ -s "$F.tmp" ] \
   && podman exec -i zabbix-db pg_restore --list < "$F.tmp" > /dev/null; then
  mv "$F.tmp" "$F"
  find "$D" -name 'zabbix-*.dump' -mtime +"$KEEP_DAYS" -delete
  echo "$(date '+%F %T') ok $(du -h --apparent-size "$F" | cut -f1)" >> "$D/backup.log"
else
  rm -f "$F.tmp"
  echo "$(date '+%F %T') FAILED" >> "$D/backup.log"
  echo "pg_dump of the Zabbix database failed ($(date '+%F %T')). Log: $D/backup.log" |
    mail -s "Zabbix database backup failed" "$MAIL_TO"
  exit 1
fi
