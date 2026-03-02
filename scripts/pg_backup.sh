#!/bin/sh
# Backup diario de PostgreSQL con rotación de 14 días
# Ejecutado por cron a las 3:00am via el container pg-backup

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="/backups/assistant_${DATE}.sql"

echo "[pg_backup] Iniciando backup: $BACKUP_FILE"

pg_dump -h postgres -U assistant assistant > "$BACKUP_FILE"

if [ $? -eq 0 ]; then
    echo "[pg_backup] ✅ Backup exitoso: $BACKUP_FILE"
else
    echo "[pg_backup] ❌ Error en backup"
    exit 1
fi

# Eliminar backups de más de 14 días
DELETED=$(find /backups -name "assistant_*.sql" -mtime +14 -delete -print | wc -l)
echo "[pg_backup] 🗑️  Eliminados $DELETED backups viejos (>14 días)"
