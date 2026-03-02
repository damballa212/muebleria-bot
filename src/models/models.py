"""Modelos ORM — definición de todas las tablas de la base de datos."""
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


# ─── Clients ─────────────────────────────────────────────────────────────────

class Client(Base):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)  # +57XXXXXXXXXX
    cedula: Mapped[str | None] = mapped_column(String(20), index=True)  # Cédula de ciudadanía
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    cases: Mapped[list["Case"]] = relationship(back_populates="client")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="client")


# ─── Cases ────────────────────────────────────────────────────────────────────

class Case(Base):
    __tablename__ = "cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)  # GAR-0042
    type: Mapped[str] = mapped_column(Enum("garantia", "cotizacion", name="case_type"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id"), nullable=False)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    description: Mapped[str | None] = mapped_column(Text)
    product: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(
        Enum("abierto", "escalado", "en_proceso", "resuelto", "cerrado", name="case_status"),
        default="escalado",
    )
    assigned_to: Mapped[str] = mapped_column(Enum("michelle", "daniel", name="case_assignee"), nullable=False)
    decision: Mapped[str | None] = mapped_column(Text)
    photos: Mapped[list] = mapped_column(JSON, default=list)  # [{url, uploaded_at, description}]
    alert_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    client: Mapped["Client"] = relationship(back_populates="cases")
    invoice: Mapped["Invoice | None"] = relationship(foreign_keys=[invoice_id])
    updates: Mapped[list["CaseUpdate"]] = relationship(back_populates="case", order_by="CaseUpdate.created_at")


class CaseUpdate(Base):
    __tablename__ = "case_updates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    new_status: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    case: Mapped["Case"] = relationship(back_populates="updates")


# ─── Invoices ────────────────────────────────────────────────────────────────

class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)  # Ej: 0042
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id"), nullable=False)
    items: Mapped[list] = mapped_column(JSON, nullable=False)  # [{descripcion, cantidad, precio_unitario}]
    total: Mapped[float | None] = mapped_column(Numeric(12, 2))
    invoice_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    photo_path: Mapped[str | None] = mapped_column(String(500))   # Path local de la foto original
    raw_ocr: Mapped[dict | None] = mapped_column(JSON)            # JSON completo del OCR
    ocr_status: Mapped[str] = mapped_column(String(20), default="done")  # "done" | "pending_ocr" | "manual"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # ─── Tipo de transacción ──────────────────────────────────────────────────
    # invoice_type: "venta" | "separe" | "abono" | "garantia" | "cambio"
    invoice_type: Mapped[str] = mapped_column(String(20), default="venta", index=True)
    # parent_invoice_number: número de la REMISIÓN original a la que este abono/separé está vinculado.
    # Null si es una venta directa o un separé nuevo sin remisión previa.
    # Si el abono fue procesado pero la remisión no está en el sistema → "huerfano".
    parent_invoice_number: Mapped[str | None] = mapped_column(String(50), index=True)

    # ─── Seguimiento de entrega ───────────────────────────────────────────────
    # delivery_status: "sin_fecha" | "pendiente" | "en_ruta" | "entregado" | "confirmado" | "demorado" | "cancelado"
    delivery_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    delivery_status: Mapped[str] = mapped_column(String(20), default="sin_fecha", index=True)
    delivery_notes: Mapped[str | None] = mapped_column(Text)
    delivery_alert_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))    # 3 días hábiles antes
    delivery_day_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # El mismo día de entrega
    delivery_followup_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # Al día siguiente: ¿llegó?
    delivery_overdue_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # Alerta vencido

    client: Mapped["Client"] = relationship(back_populates="invoices")


# ─── Reminders ───────────────────────────────────────────────────────────────

class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ─── InteractionLog ──────────────────────────────────────────────────────────

class InteractionLog(Base):
    __tablename__ = "interaction_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)   # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
