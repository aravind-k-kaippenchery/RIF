"""FastAPI application entry point for the B2B Product Intelligence Assistant."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.admin_schema import router as admin_schema_router
from app.api.routes.audit import router as audit_router
from app.api.routes.benchmarks import router as benchmarks_router
from app.api.routes.agent import router as agent_router
from app.api.routes.frontend import router as frontend_router
from app.api.routes.contracts import router as contracts_router
from app.api.routes.crud import router as crud_router
from app.api.routes.database import router as database_router
from app.api.routes.documents import router as documents_router
from app.api.routes.document_storage import router as document_storage_router
from app.api.routes.demo import router as demo_router
from app.api.routes.health import router as health_router
from app.api.routes.live_health import router as live_health_router
from app.api.routes.hybrid import router as hybrid_router
from app.api.routes.llm import router as llm_router
from app.api.routes.mcp import router as mcp_router
from app.api.routes.rag import router as rag_router
from app.api.routes.schema import router as schema_router
from app.api.routes.structured_read import router as structured_read_router
from app.api.routes.sessions import router as sessions_router
from app.api.routes.validation import router as validation_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging_config import configure_logging, log_event
from app.core.middleware import RequestContextMiddleware
from app.db.session import close_database_engine
from app.mcp.server import mcp_server


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Run safe startup/shutdown tasks without requiring PostgreSQL or Ollama to be online."""

    settings = get_settings()
    configure_logging()
    log_event(
        level="INFO",
        event="application_started",
        app_name=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
    )
    async with mcp_server.session_manager.run():
        yield
    close_database_engine()
    log_event(level="INFO", event="application_stopped")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Local-first backend for a B2B Product Intelligence Assistant. "
            "Phase 16 completes final integration, reviewer-ready demo evidence, release readiness, and all prior local safety boundaries without business-table writes."
        ),
        debug=settings.app_debug,
        lifespan=lifespan,
    )

    # Ensure CORS preflight (OPTIONS) is handled before any other request middleware.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Session-ID", "Mcp-Session-Id"],
    )

    # Explicitly allow OPTIONS preflight for the API surface.
    # Some routes may still hit exception middleware; returning a Response here
    # prevents CORS preflight from failing.



    # RequestContextMiddleware is for trace headers and logging; it should not interfere with preflight.
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(live_health_router)
    app.include_router(frontend_router)
    app.include_router(admin_schema_router)
    app.include_router(audit_router)
    app.include_router(benchmarks_router)
    app.include_router(agent_router)
    app.include_router(hybrid_router)
    app.include_router(contracts_router)
    app.include_router(database_router)
    app.include_router(document_storage_router)
    app.include_router(documents_router)
    app.include_router(demo_router)
    app.include_router(rag_router)
    app.include_router(schema_router)
    app.include_router(sessions_router)
    app.include_router(validation_router)
    app.include_router(mcp_router)
    app.include_router(llm_router)
    app.include_router(structured_read_router)
    app.include_router(crud_router)
    app.mount("/mcp", mcp_server.streamable_http_app())
    return app


app = create_app()

