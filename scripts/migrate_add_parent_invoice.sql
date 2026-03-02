-- Migración: vincula abonos/separés a su factura de venta original.
-- parent_invoice_number: número de la REMISIÓN original a la que este abono/separé pertenece.
-- Ejecutar: docker exec -i asistentenorena-postgres-1 psql -U assistant -d assistant < scripts/migrate_add_parent_invoice.sql

ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS parent_invoice_number VARCHAR(50);

CREATE INDEX IF NOT EXISTS ix_invoices_parent_invoice_number
    ON invoices(parent_invoice_number)
    WHERE parent_invoice_number IS NOT NULL;
