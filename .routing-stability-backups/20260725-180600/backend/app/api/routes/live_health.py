"""Fast, bounded, live health probes for the local application stack."""

from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable

import httpx
from fastapi import APIRouter, Request, Response

from app.agents.orchestrator import agent_orchestrator
from app.core.config import get_settings
from app.core.response import ResponseBuilder
from app.db.health import get_database_health
from app.mcp.client import LocalMCPClient
from app.rag.document_rag_service import document_rag_service

router = APIRouter(prefix="/api/system", tags=["System"])


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _service(
    *,
    key: str,
    name: str,
    detail: str,
    available: bool,
    state: str,
    message: str,
    latency_ms: int,
    kind: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "detail": detail,
        "available": available,
        "state": state,
        "message": message,
        "latency_ms": latency_ms,
        "kind": kind,
        "metadata": metadata or {},
    }


def _probe_fastapi() -> dict[str, Any]:
    return _service(
        key="fastapi",
        name="FastAPI",
        detail="Backend request surface",
        available=True,
        state="connected",
        message="The live-health endpoint responded successfully.",
        latency_ms=0,
        kind="connection",
    )


def _probe_postgres() -> dict[str, Any]:
    started = perf_counter()
    health = get_database_health()
    return _service(
        key="postgres",
        name="PostgreSQL",
        detail="Structured business data",
        available=bool(health.connected),
        state="connected" if health.connected else "unavailable",
        message=health.message,
        latency_ms=_elapsed_ms(started),
        kind="connection",
        metadata={"database": health.database} if health.database else {},
    )


def _model_matches(configured_model: str, installed_name: str) -> bool:
    configured = configured_model.strip().lower()
    installed = installed_name.strip().lower()
    if configured == installed:
        return True
    if ":" not in configured and installed == f"{configured}:latest":
        return True
    if configured.endswith(":latest") and installed == configured.removesuffix(":latest"):
        return True
    return False


def _probe_ollama() -> dict[str, Any]:
    """Use a short status timeout instead of the normal generation timeout."""

    started = perf_counter()
    settings = get_settings()
    names: list[str] = []
    try:
        with httpx.Client(timeout=httpx.Timeout(3.0), follow_redirects=False) as client:
            response = client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
            response.raise_for_status()
            payload = response.json()
        models = payload.get("models", []) if isinstance(payload, dict) else []
        names = sorted(
            {
                str(item.get("name") or item.get("model"))
                for item in models
                if isinstance(item, dict) and (item.get("name") or item.get("model"))
            }
        )
        installed = any(_model_matches(settings.ollama_model, name) for name in names)
        return _service(
            key="ollama",
            name="Ollama",
            detail="Local model runtime",
            available=installed,
            state="connected" if installed else "attention",
            message=(
                "Ollama is reachable and the configured model is installed."
                if installed
                else f"Ollama is reachable, but '{settings.ollama_model}' is not installed."
            ),
            latency_ms=_elapsed_ms(started),
            kind="connection",
            metadata={
                "configured_model": settings.ollama_model,
                "installed_model_names": names,
                "server_reachable": True,
            },
        )
    except (httpx.HTTPError, ValueError) as exc:
        return _service(
            key="ollama",
            name="Ollama",
            detail="Local model runtime",
            available=False,
            state="unavailable",
            message=f"Ollama did not answer the bounded live probe: {type(exc).__name__}.",
            latency_ms=_elapsed_ms(started),
            kind="connection",
            metadata={
                "configured_model": settings.ollama_model,
                "installed_model_names": names,
                "server_reachable": False,
            },
        )


def _probe_chromadb() -> dict[str, Any]:
    started = perf_counter()
    try:
        status = document_rag_service.status()
        available = bool(status.get("chromadb_available", False))
        count = int(status.get("indexed_chunk_count", 0) or 0)
        return _service(
            key="chromadb",
            name="ChromaDB",
            detail="Local document knowledge",
            available=available,
            state="connected" if available else "unavailable",
            message=(
                f"ChromaDB is reachable with {count} indexed chunk(s)."
                if available
                else "ChromaDB could not be reached by the document-RAG service."
            ),
            latency_ms=_elapsed_ms(started),
            kind="connection",
            metadata={"indexed_chunk_count": count},
        )
    except Exception as exc:  # defensive boundary: never expose a raw traceback
        return _service(
            key="chromadb",
            name="ChromaDB",
            detail="Local document knowledge",
            available=False,
            state="unavailable",
            message=f"ChromaDB live probe failed safely: {type(exc).__name__}.",
            latency_ms=_elapsed_ms(started),
            kind="connection",
        )


def _probe_langgraph() -> dict[str, Any]:
    started = perf_counter()
    try:
        status = agent_orchestrator.status()
        compiled = bool(status.get("graph_compiled", False))
        routes = list(status.get("supported_routes") or [])
        return _service(
            key="langgraph",
            name="LangGraph",
            detail="Route orchestration",
            available=compiled,
            state="ready" if compiled else "unavailable",
            message=(
                f"The graph is compiled with {len(routes)} controlled route(s)."
                if compiled
                else "The LangGraph workflow is not compiled."
            ),
            latency_ms=_elapsed_ms(started),
            kind="runtime",
            metadata={"supported_routes": routes},
        )
    except Exception as exc:
        return _service(
            key="langgraph",
            name="LangGraph",
            detail="Route orchestration",
            available=False,
            state="unavailable",
            message=f"LangGraph readiness probe failed safely: {type(exc).__name__}.",
            latency_ms=_elapsed_ms(started),
            kind="runtime",
        )


def _probe_mcp() -> dict[str, Any]:
    started = perf_counter()
    try:
        tools = LocalMCPClient().list_tools()
        count = len(tools)
        ready = count > 0
        return _service(
            key="mcp",
            name="MCP",
            detail="Controlled tool boundary",
            available=ready,
            state="ready" if ready else "unavailable",
            message=(
                f"The in-process MCP boundary exposes {count} controlled tool(s)."
                if ready
                else "No controlled MCP tools are registered."
            ),
            latency_ms=_elapsed_ms(started),
            kind="runtime",
            metadata={"tool_count": count},
        )
    except Exception as exc:
        return _service(
            key="mcp",
            name="MCP",
            detail="Controlled tool boundary",
            available=False,
            state="unavailable",
            message=f"MCP readiness probe failed safely: {type(exc).__name__}.",
            latency_ms=_elapsed_ms(started),
            kind="runtime",
        )


@router.get("/live-health", summary="Run bounded live probes for all local components")
def live_system_health(request: Request, response: Response):
    """Return a fresh snapshot; the frontend polls this endpoint repeatedly."""

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    probes: tuple[Callable[[], dict[str, Any]], ...] = (
        _probe_fastapi,
        _probe_postgres,
        _probe_ollama,
        _probe_chromadb,
        _probe_langgraph,
        _probe_mcp,
    )
    services = [probe() for probe in probes]
    overall_ready = all(bool(item["available"]) for item in services)
    checked_at = datetime.now(timezone.utc).isoformat()
    return ResponseBuilder.success(
        request,
        answer="Fresh bounded local-system health probes completed.",
        data={
            "live": True,
            "checked_at": checked_at,
            "poll_after_seconds": 5,
            "overall_ready": overall_ready,
            "services": services,
            "notes": [
                "This is a no-cache point-in-time snapshot refreshed by a browser heartbeat.",
                "LangGraph and MCP are in-process runtime readiness checks, not external network connections.",
                "The Ollama probe uses a three-second health timeout and does not run model inference.",
            ],
        },
    )
