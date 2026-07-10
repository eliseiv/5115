"""Audit Service: append-only event log (audit/03-architecture.md, TD-001)."""

from app.audit.service import AuditEvent, AuditService

__all__ = ["AuditEvent", "AuditService"]
