"""Safe PostgreSQL health checks used by Phase 2 verification endpoints."""

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import get_engine


class DatabaseHealth(BaseModel):
    """Connection result that can be returned without exposing credentials."""

    model_config = ConfigDict(extra="forbid")

    connected: bool
    database: str | None = None
    message: str


def get_database_health() -> DatabaseHealth:
    """Test one small SELECT without raising raw database errors to the frontend."""

    try:
        with get_engine().connect() as connection:
            database_name = connection.execute(text("SELECT current_database()")).scalar_one()
        return DatabaseHealth(
            connected=True,
            database=database_name,
            message="PostgreSQL is reachable and accepted the connection.",
        )
    except SQLAlchemyError:
        return DatabaseHealth(
            connected=False,
            database=None,
            message=(
                "PostgreSQL could not be reached. Check that the PostgreSQL service is running and that backend/.env "
                "contains the correct POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, and POSTGRES_PASSWORD values."
            ),
        )
