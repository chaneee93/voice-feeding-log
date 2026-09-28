#!/bin/bash
# 매일 새벽 기록 파일(feeding.db)을 날짜별로 백업하고, 30일 지난 백업은 삭제
set -e
mkdir -p ~/feeding/backups
STAMP=$(date +%F)
docker exec ijae-app python -c "import sqlite3; s=sqlite3.connect('/data/feeding.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()"
mv ~/feeding/data/backup.db ~/feeding/backups/feeding-$STAMP.db
find ~/feeding/backups -name "feeding-*.db" -mtime +30 -delete
echo "$(date) backup ok: feeding-$STAMP.db"
