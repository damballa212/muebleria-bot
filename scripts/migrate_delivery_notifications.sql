-- Migración: columnas para notificaciones de entrega (día-de y seguimiento al día siguiente)
ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS delivery_day_notified_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivery_followup_sent_at TIMESTAMPTZ;
