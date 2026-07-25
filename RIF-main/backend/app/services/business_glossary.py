"""Business glossary and synonym resolution for Phase 3 schema intelligence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GlossaryEntry:
    """One controlled business synonym mapping."""

    term: str
    canonical_term: str
    target_type: str
    target_name: str
    explanation: str


GLOSSARY_ENTRIES: tuple[GlossaryEntry, ...] = (
    GlossaryEntry("worker", "employee", "table", "employees", "Workers are represented by employee records."),
    GlossaryEntry("workers", "employees", "table", "employees", "Workers are represented by employee records."),
    GlossaryEntry("staff", "employees", "table", "employees", "Staff members are stored in the employees table."),
    GlossaryEntry("employee", "employee", "table", "employees", "Employee records are stored in the employees table."),
    GlossaryEntry("employees", "employees", "table", "employees", "Employee records are stored in the employees table."),
    GlossaryEntry("permission", "employee permission", "table", "employee_permissions", "Employee permissions are child records of employees."),
    GlossaryEntry("permissions", "employee permissions", "table", "employee_permissions", "Employee permissions are child records of employees."),
    GlossaryEntry("user permission", "employee permission", "table", "employee_permissions", "User permissions are modeled as employee permission child records."),
    GlossaryEntry("user permissions", "employee permissions", "table", "employee_permissions", "User permissions are modeled as employee permission child records."),
    GlossaryEntry("supplier", "vendor", "table", "vendors", "Suppliers are represented by vendor records."),
    GlossaryEntry("suppliers", "vendors", "table", "vendors", "Suppliers are represented by vendor records."),
    GlossaryEntry("vendor", "vendor", "table", "vendors", "Vendor records are stored in the vendors table."),
    GlossaryEntry("vendors", "vendors", "table", "vendors", "Vendor records are stored in the vendors table."),
    GlossaryEntry("client", "customer", "table", "customers", "Clients are represented by customer records."),
    GlossaryEntry("clients", "customers", "table", "customers", "Clients are represented by customer records."),
    GlossaryEntry("customer", "customer", "table", "customers", "Customer records are stored in the customers table."),
    GlossaryEntry("customers", "customers", "table", "customers", "Customer records are stored in the customers table."),
    GlossaryEntry("item", "product", "table", "products", "Items are represented by product records."),
    GlossaryEntry("items", "products", "table", "products", "Items are represented by product records."),
    GlossaryEntry("product", "product", "table", "products", "Products are stored in the products table."),
    GlossaryEntry("products", "products", "table", "products", "Products are stored in the products table."),
    GlossaryEntry("deal", "sales deal", "table", "sales_deals", "Deals are represented by sales pipeline records."),
    GlossaryEntry("deals", "sales deals", "table", "sales_deals", "Deals are represented by sales pipeline records."),
    GlossaryEntry("pipeline", "sales deals", "table", "sales_deals", "Pipeline data is stored in the sales_deals table."),
    GlossaryEntry("sales", "sales deals", "table", "sales_deals", "Sales pipeline data is stored in the sales_deals table."),
    GlossaryEntry("cost", "price", "column", "products.price", "Cost usually maps to the product price column."),
    GlossaryEntry("pricing", "price", "column", "products.price", "Pricing usually maps to the product price column."),
    GlossaryEntry("revenue", "amount", "column", "sales_deals.amount", "Revenue-style deal values are stored as deal amount."),
    GlossaryEntry("location", "city", "column", "*.city", "Location often maps to city fields."),
)


def list_glossary_entries() -> list[dict[str, str]]:
    """Return the glossary in API-friendly form."""

    return [entry.__dict__.copy() for entry in GLOSSARY_ENTRIES]


def resolve_business_term(term: str) -> Optional[dict[str, str]]:
    """Resolve a user-facing word to the controlled table/column target."""

    normalized = term.strip().lower().replace("_", " ")
    for entry in GLOSSARY_ENTRIES:
        if entry.term == normalized:
            return entry.__dict__.copy()
    return None


def normalize_business_text(text: str) -> dict:
    """Find known business terms in text and report their canonical schema targets.

    This does not rewrite SQL. It only gives later LLM prompts a controlled hint list.
    """

    lowered = text.lower()
    matches: list[dict[str, str]] = []
    seen_terms: set[str] = set()
    for entry in sorted(GLOSSARY_ENTRIES, key=lambda item: len(item.term), reverse=True):
        pattern = r"\b" + re.escape(entry.term) + r"\b"
        if re.search(pattern, lowered) and entry.term not in seen_terms:
            matches.append(entry.__dict__.copy())
            seen_terms.add(entry.term)
    return {
        "original_text": text,
        "matched_terms": matches,
        "schema_hints": sorted({match["target_name"] for match in matches}),
    }
