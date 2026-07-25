# Phase 5 SQL Context Hotfix

The original Phase 5 SQL-generation prompt included the full 16-table schema contract, detailed column metadata, operational table definitions, and the JSON output schema. On a local Llama 3 8B runtime this can approach or exceed the default context window, producing a generic Ollama HTTP failure before SQL generation begins.

This hotfix changes only the SQL-generation context. It supplies a compact business-only schema: approved business table names, column names, primary keys, join relationships, and the required employee-permission relationship. It excludes operational tables, descriptions, credentials, and all execution capability.

Safety remains unchanged: generated SQL is still validated by the Phase 4 AST validator and is never executed by this Phase 5 endpoint.
