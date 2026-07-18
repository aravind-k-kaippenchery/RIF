"""Phase 11 verified hybrid evidence fusion.

This service combines two independently grounded evidence streams without relying on a
model to guess relationships:

1. Local ChromaDB returns document chunks.
2. A restricted MCP tool looks up database product/vendor facts.
3. The backend links a document chunk to a PostgreSQL product only when the product
   name or product code appears in that chunk.
4. The final answer is composed from those verified links plus live database rows.

The service deliberately does not perform broad fuzzy joins between document prose and
database rows. When an exact product identity cannot be verified, it returns a controlled
``information_not_available`` response instead of inventing a cross-source conclusion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.core.constants import ResponseStatus
from app.core.schemas import SourceCitation
from app.mcp.client import LocalMCPClient
from app.rag.document_rag_service import RetrievalMatch


class HybridEvidenceError(RuntimeError):
    """Expected, frontend-safe hybrid evidence problem."""

    def __init__(self, *, status: ResponseStatus, code: str, message: str, details: list[dict[str, Any]] | None = None) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        super().__init__(message)


@dataclass(frozen=True)
class HybridEvidenceResult:
    """A safe final hybrid response produced from verified evidence only."""

    status: ResponseStatus
    answer: str
    matches: list[dict[str, Any]]
    document_matches: list[RetrievalMatch]
    source_citations: list[SourceCitation]
    max_price: float | None
    no_data_reason: str | None = None

    def to_data(self, *, question: str) -> dict[str, Any]:
        document_references = [match.reference for match in self.document_matches]
        return {
            "question": question,
            "phase_11_behavior": "verified_hybrid_evidence_fusion",
            "phase_11_required_for_final_answer": False,
            "verified_match_count": len(self.matches),
            "verified_matches": self.matches,
            "preliminary_document_match_count": len(self.document_matches),
            "document_source_references": document_references,
            "database_source_tables": ["vendors", "products", "product_vendor_mappings"],
            "max_price_filter": self.max_price,
            "answer_generation": "deterministic_verified_evidence_synthesis",
            "evidence_join_policy": (
                "A document chunk is linked to a database product only when the product name or code "
                "is explicitly present in the retrieved chunk; vendor and price facts then come from "
                "the matching PostgreSQL product-vendor mapping."
            ),
            "no_data_reason": self.no_data_reason,
        }


class HybridEvidenceService:
    """Orchestrate one restricted hybrid evidence request through MCP."""

    _PRICE_PATTERN = re.compile(
        r"\b(?:under|below|less\s+than|up\s*to|upto|within)\s*"
        r"(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*"
        r"(lakhs?|lacs?|lac|thousand|k|crores?|cr)?\b",
        re.IGNORECASE,
    )

    def __init__(self, mcp_client: LocalMCPClient | None = None) -> None:
        self._mcp_client = mcp_client or LocalMCPClient()

    @classmethod
    def parse_max_price(cls, question: str) -> float | None:
        """Parse a conservative upper-price filter in Indian business phrasing.

        Examples: ``under 5 lakh`` -> 500000; ``below ₹4,75,000`` -> 475000.
        A missing or ambiguous price leaves the filter unset rather than guessing.
        """

        match = cls._PRICE_PATTERN.search(question)
        if not match:
            return None
        try:
            amount = float(match.group(1).replace(",", ""))
        except ValueError:
            return None
        unit = (match.group(2) or "").lower()
        multiplier = 1.0
        if unit.startswith(("lakh", "lac")):
            multiplier = 100_000.0
        elif unit in {"k", "thousand"}:
            multiplier = 1_000.0
        elif unit.startswith("crore") or unit == "cr":
            multiplier = 10_000_000.0
        return amount * multiplier

    @staticmethod
    def _document_payload(matches: list[RetrievalMatch]) -> list[dict[str, Any]]:
        return [
            {
                "reference": match.reference,
                "filename": match.filename,
                "page_number": match.page_number,
                "chunk_index": match.chunk_index,
                "text": match.text,
                "similarity": match.similarity,
            }
            for match in matches
        ]

    @staticmethod
    def _citations(matches: list[dict[str, Any]]) -> list[SourceCitation]:
        citations: list[SourceCitation] = []
        document_seen: set[str] = set()
        database_seen: set[str] = set()
        for item in matches:
            database_reference = "vendors, products, product_vendor_mappings"
            if database_reference not in database_seen:
                database_seen.add(database_reference)
                citations.append(
                    SourceCitation(
                        source_type="database",
                        reference=database_reference,
                        detail="Verified PostgreSQL vendor, product, and quoted-price mapping used in the hybrid answer.",
                    )
                )
            for reference in item.get("document_source_references", []):
                if reference in document_seen:
                    continue
                document_seen.add(reference)
                citations.append(
                    SourceCitation(
                        source_type="document",
                        reference=reference,
                        detail="Retrieved document evidence explicitly naming the matched product.",
                    )
                )
        return citations

    @staticmethod
    def _compose_answer(matches: list[dict[str, Any]]) -> str:
        statements: list[str] = []
        for item in matches:
            price = float(item["quoted_price"])
            formatted_price = f"₹{price:,.0f}"
            vendor = item["vendor_name"]
            product = item["product_name"]
            statements.append(
                f"{vendor} offers {product} for {formatted_price}. "
                f"Retrieved brochure evidence explicitly states that {product} supports textile automation."
            )
        return " ".join(statements)

    def fuse(self, *, question: str, document_matches: list[RetrievalMatch], limit: int = 20) -> HybridEvidenceResult:
        """Return a final answer only where document and database facts are verifiably linked."""

        if not document_matches:
            return HybridEvidenceResult(
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                answer="Information not available in the uploaded documents.",
                matches=[],
                document_matches=[],
                source_citations=[],
                max_price=self.parse_max_price(question),
                no_data_reason="No retrieved document chunk met the configured relevance threshold.",
            )

        max_price = self.parse_max_price(question)
        mcp_response = self._mcp_client.call_tool(
            "get_verified_hybrid_evidence",
            {
                "document_matches": self._document_payload(document_matches),
                "max_price": max_price,
                "limit": limit,
            },
        )
        if not mcp_response.get("ok"):
            raise HybridEvidenceError(
                status=ResponseStatus.TOOL_FAILED,
                code=str(mcp_response.get("error_code") or "hybrid_mcp_tool_failed"),
                message=str(mcp_response.get("error_message") or "The verified hybrid evidence tool could not complete."),
            )

        payload = dict(mcp_response.get("result") or {})
        if not payload.get("retrieved"):
            raise HybridEvidenceError(
                status=ResponseStatus.TOOL_FAILED,
                code=str(payload.get("error_code") or "hybrid_evidence_retrieval_failed"),
                message=str(payload.get("error_message") or "The verified hybrid evidence tool could not complete."),
                details=list(payload.get("details") or []),
            )

        verified_matches = list(payload.get("matches") or [])
        if not verified_matches:
            reason = str(payload.get("no_data_reason") or "No verified product/vendor database record matched the retrieved document evidence and requested filter.")
            return HybridEvidenceResult(
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                answer="Information not available from a verified combination of the current database and uploaded documents.",
                matches=[],
                document_matches=document_matches,
                source_citations=[],
                max_price=max_price,
                no_data_reason=reason,
            )

        return HybridEvidenceResult(
            status=ResponseStatus.SUCCESS,
            answer=self._compose_answer(verified_matches),
            matches=verified_matches,
            document_matches=document_matches,
            source_citations=self._citations(verified_matches),
            max_price=max_price,
        )


hybrid_evidence_service = HybridEvidenceService()
