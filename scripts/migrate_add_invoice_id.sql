-- Migración: agregar invoice_id a la tabla cases
-- Ejecutar UNA SOLA VEZ en la base de datos de producción
-- Fecha: 2026-03-01

ALTER TABLE cases
    ADD COLUMN IF NOT EXISTS invoice_id UUID REFERENCES invoices(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_cases_invoice_id ON cases (invoice_id);
