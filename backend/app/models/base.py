"""Declarative SQLAlchemy base for all PostgreSQL models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class shared by every ORM model."""
