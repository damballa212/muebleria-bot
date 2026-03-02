-- ─── Migración: Sistema de Seguimiento de Pedidos ─────────────────────────────
-- Agrega columnas de entrega a la tabla invoices para el módulo de seguimiento.
-- Para deployments existentes ejecutar: psql $DATABASE_URL -f scripts/migrate_add_order_tracking.sql
-- En deployments nuevos, SQLAlchemy crea las columnas automáticamente con create_all.

ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS delivery_date            TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS delivery_status          VARCHAR(20) NOT NULL DEFAULT 'sin_fecha',
    ADD COLUMN IF NOT EXISTS delivery_notes           TEXT,
    ADD COLUMN IF NOT EXISTS delivery_alert_sent_at   TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS delivery_overdue_alert_at TIMESTAMP WITH TIME ZONE;

-- Índices para consultas frecuentes del scheduler
CREATE INDEX IF NOT EXISTS ix_invoices_delivery_date   ON invoices(delivery_date);
CREATE INDEX IF NOT EXISTS ix_invoices_delivery_status ON invoices(delivery_status);

-- Retroalimentar delivery_date y delivery_status desde raw_ocr para registros existentes
-- Solo aplica a remisiones manuales que ya tenían fecha_entrega en raw_ocr
UPDATE invoices
SET
    delivery_date = CASE
        WHEN raw_ocr->>'fecha_entrega' ~ '^\d{2}/\d{2}/\d{2}$'
            THEN TO_TIMESTAMP(raw_ocr->>'fecha_entrega', 'DD/MM/YY') AT TIME ZONE 'America/Bogota'
        WHEN raw_ocr->>'fecha_entrega' ~ '^\d{2}/\d{2}/\d{4}$'
            THEN TO_TIMESTAMP(raw_ocr->>'fecha_entrega', 'DD/MM/YYYY') AT TIME ZONE 'America/Bogota'
        ELSE NULL
    END,
    delivery_status = CASE
        WHEN raw_ocr->>'fecha_entrega' IS NOT NULL
         AND raw_ocr->>'fecha_entrega' != ''
            THEN 'pendiente'
        ELSE 'sin_fecha'
    END
WHERE raw_ocr IS NOT NULL
  AND raw_ocr->>'fecha_entrega' IS NOT NULL
  AND raw_ocr->>'fecha_entrega' != ''
  AND delivery_status = 'sin_fecha';
