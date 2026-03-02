-- Migración: agrega invoice_type para diferenciar tipo de transacción en la misma planilla.
-- Tipos: "venta" (default), "separe", "abono", "garantia", "cambio"
-- Ejecutar: docker exec -i norena-postgres psql -U norena -d norena_db < scripts/migrate_add_invoice_type.sql

ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS invoice_type VARCHAR(20) NOT NULL DEFAULT 'venta';

CREATE INDEX IF NOT EXISTS ix_invoices_invoice_type ON invoices(invoice_type);

-- Retro-alimentar: si raw_ocr ya tiene tipo_transaccion lo copia al nuevo campo
UPDATE invoices
SET invoice_type = raw_ocr->>'tipo_transaccion'
WHERE raw_ocr->>'tipo_transaccion' IS NOT NULL
  AND raw_ocr->>'tipo_transaccion' IN ('venta', 'separe', 'abono', 'garantia', 'cambio');
