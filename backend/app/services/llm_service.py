"""Phase 5 local Ollama adapter.

This service is deliberately the only module allowed to call the local Ollama HTTP API.
It does not receive PostgreSQL credentials, cannot execute SQL, and cannot call MCP tools.
Every model output is constrained with a Pydantic JSON schema and is revalidated by Python.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.logging_config import log_event
from app.prompts import phase5 as prompts
from app.schemas.phase5 import (
    GroundedAnswerResult,
    IntentClassificationResult,
    OllamaInvocationMetadata,
    RecordExtractionResult,
    SQLGenerationResult,
)
from app.schemas.phase13 import AdminSchemaProposalResult
from app.services.business_glossary import normalize_business_text
from app.services.query_constraint_guard import validate_structured_read_constraints
from app.services.schema_registry import (
    get_allowed_columns,
    get_allowed_tables,
    get_relationships,
    get_schema_contract,
    get_table_schema,
)
from app.services.sql_validation import SQLValidationResult, validate_dml_sql


ModelT = TypeVar("ModelT", bound=BaseModel)
SemanticValidator = Callable[[BaseModel], tuple[bool, str]]


@dataclass(frozen=True)
class OllamaHealth:
    connected: bool
    model_installed: bool
    base_url: str
    configured_model: str
    installed_model_names: list[str]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "model_installed": self.model_installed,
            "base_url": self.base_url,
            "configured_model": self.configured_model,
            "installed_model_names": self.installed_model_names,
            "message": self.message,
        }


class LLMServiceError(RuntimeError):
    """Expected local-model error that can be converted into a safe API response."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class OllamaUnavailableError(LLMServiceError):
    pass


class LLMOutputValidationError(LLMServiceError):
    pass


class LLMService:
    """Controlled local-model adapter required before later LangGraph orchestration."""

    # Deterministic settings for safety-sensitive tasks.
    # SQL, routing, extraction, and schema proposals should be as predictable as possible.
    # Grounded RAG answers retain a very small amount of flexibility for readable phrasing,
    # while still being constrained by backend-provided evidence and Pydantic validation.
    DEFAULT_OLLAMA_SEED = 42
    INTENT_TEMPERATURE = 0.0
    SQL_TEMPERATURE = 0.0
    ADMIN_SCHEMA_TEMPERATURE = 0.0
    RECORD_EXTRACTION_TEMPERATURE = 0.0
    GROUNDED_ANSWER_TEMPERATURE = 0.1

    @staticmethod
    def _validate_temperature(temperature: float) -> float:
        """Reject invalid temperature values before sending a request to Ollama."""
        if not 0.0 <= temperature <= 2.0:
            raise LLMOutputValidationError(
                code="invalid_ollama_temperature",
                message="Ollama temperature must be between 0.0 and 2.0.",
            )
        return temperature

    def _require_local_endpoint(self) -> None:
        settings = get_settings()
        if not settings.is_local_ollama_endpoint:
            raise OllamaUnavailableError(
                code="ollama_non_local_endpoint_blocked",
                message="Phase 5 accepts only a loopback Ollama endpoint. Update OLLAMA_BASE_URL to localhost or 127.0.0.1.",
            )

    @staticmethod
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

    @classmethod
    def _ollama_compatible_schema(cls, value: Any) -> Any:
        """Remove annotation/validation keywords that some Ollama grammar builds reject.

        The complete Pydantic schema is still included in the system prompt and the
        returned JSON is always validated by Pydantic, so removing these format-only
        constraints does not weaken backend validation.
        """

        unsupported = {
            "title",
            "description",
            "examples",
            "default",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "pattern",
        }
        if isinstance(value, dict):
            return {
                key: cls._ollama_compatible_schema(item)
                for key, item in value.items()
                if key not in unsupported
            }
        if isinstance(value, list):
            return [cls._ollama_compatible_schema(item) for item in value]
        return value

    def _send_chat_with_format_fallback(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Use JSON Schema first, then retry the same attempt in JSON mode on HTTP 400.

        This handles Ollama builds that support structured JSON but reject one of the
        Pydantic-schema keywords. Python/Pydantic validation remains mandatory.
        """

        try:
            return self._send_chat_request(payload), "json_schema"
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400 or payload.get("format") == "json":
                raise

            fallback_payload = dict(payload)
            fallback_payload["format"] = "json"
            log_event(
                level="WARNING",
                event="ollama_schema_format_rejected_retrying_json_mode",
                status_code=exc.response.status_code,
            )
            return self._send_chat_request(fallback_payload), "json"

    def _fetch_tags(self) -> dict[str, Any]:
        self._require_local_endpoint()
        settings = get_settings()
        with httpx.Client(timeout=settings.ollama_request_timeout_seconds, follow_redirects=False) as client:
            response = client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
            response.raise_for_status()
            return response.json()

    def check_llm_health(self) -> OllamaHealth:
        """Check local service reachability and whether the configured model is already pulled."""

        settings = get_settings()
        try:
            payload = self._fetch_tags()
            models = payload.get("models", []) if isinstance(payload, dict) else []
            names = sorted(
                {
                    str(item.get("name") or item.get("model"))
                    for item in models
                    if isinstance(item, dict) and (item.get("name") or item.get("model"))
                }
            )
            installed = any(self._model_matches(settings.ollama_model, name) for name in names)
            message = (
                "Local Ollama is reachable and the configured model is available."
                if installed
                else "Local Ollama is reachable, but the configured model is not installed. Pull the configured model before generating."
            )
            return OllamaHealth(
                connected=True,
                model_installed=installed,
                base_url=settings.ollama_base_url,
                configured_model=settings.ollama_model,
                installed_model_names=names,
                message=message,
            )
        except OllamaUnavailableError as exc:
            return OllamaHealth(
                connected=False,
                model_installed=False,
                base_url=settings.ollama_base_url,
                configured_model=settings.ollama_model,
                installed_model_names=[],
                message=exc.message,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return OllamaHealth(
                connected=False,
                model_installed=False,
                base_url=settings.ollama_base_url,
                configured_model=settings.ollama_model,
                installed_model_names=[],
                message="Local Ollama is not reachable. Start Ollama and verify its local API before generating.",
            )

    def _ensure_model_ready(self) -> OllamaHealth:
        health = self.check_llm_health()
        if not health.connected:
            raise OllamaUnavailableError(code="ollama_unavailable", message=health.message)
        if not health.model_installed:
            raise OllamaUnavailableError(
                code="ollama_model_not_installed",
                message=f"Configured local model '{health.configured_model}' is not installed. Pull it with Ollama before generating.",
            )
        return health

    def _send_chat_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one non-streaming structured-output request to local Ollama."""

        self._require_local_endpoint()
        settings = get_settings()
        with httpx.Client(timeout=settings.ollama_request_timeout_seconds, follow_redirects=False) as client:
            response = client.post(f"{settings.ollama_base_url.rstrip('/')}/api/chat", json=payload)
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict):
            raise LLMOutputValidationError(code="ollama_invalid_response", message="Local Ollama returned an invalid response shape.")
        return body

    @staticmethod
    def _response_content(body: dict[str, Any]) -> str:
        message = body.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise LLMOutputValidationError(
                code="ollama_empty_response",
                message="Local Ollama returned no usable structured response.",
            )
        return content

    @staticmethod
    def _metadata(body: dict[str, Any], *, attempts: int) -> OllamaInvocationMetadata:
        settings = get_settings()
        return OllamaInvocationMetadata(
            model=str(body.get("model") or settings.ollama_model),
            attempts=attempts,
            total_duration_ns=body.get("total_duration") if isinstance(body.get("total_duration"), int) else None,
            prompt_eval_count=body.get("prompt_eval_count") if isinstance(body.get("prompt_eval_count"), int) else None,
            eval_count=body.get("eval_count") if isinstance(body.get("eval_count"), int) else None,
        )

    def generate_json(
        self,
        *,
        output_model: type[ModelT],
        system_prompt: str,
        user_prompt: str,
        semantic_validator: SemanticValidator | None = None,
        temperature: float | None = None,
        seed: int | None = None,
    ) -> tuple[ModelT, OllamaInvocationMetadata]:
        """Generate and validate one Pydantic-constrained JSON result.

        At most two total calls are made: initial generation plus one correction retry.
        Prompts and raw output are intentionally not written to logs.
        """

        self._ensure_model_ready()
        settings = get_settings()
        resolved_temperature = self._validate_temperature(
            settings.ollama_temperature if temperature is None else temperature
        )
        resolved_seed = self.DEFAULT_OLLAMA_SEED if seed is None else seed
        json_schema = output_model.model_json_schema()
        ollama_schema = self._ollama_compatible_schema(json_schema)
        full_system_prompt = prompts.with_output_schema(system_prompt, json_schema)
        correction_feedback: str | None = None
        last_error = "The model response did not match the required JSON contract."

        for attempt in range(1, settings.ollama_max_attempts + 1):
            # The first attempt stays deterministic. A correction retry uses a
            # different seed so the local model is not forced to repeat the same
            # rejected output verbatim.
            attempt_seed = resolved_seed + (attempt - 1)

            attempt_user_prompt = user_prompt
            if correction_feedback:
                select_star_guidance = ""
                if "select *" in correction_feedback.lower() or "star expression" in correction_feedback.lower():
                    select_star_guidance = (
                        "\nSQL correction rule: Never use SELECT *, table.*, or alias.*. "
                        "Use explicit approved column names from the controlled schema. "
                        "COUNT(*) is the only allowed star expression."
                    )

                attempt_user_prompt = (
                    f"{user_prompt}\n\nThe previous JSON response was rejected by the backend for this reason:\n"
                    f"{correction_feedback}{select_star_guidance}\n"
                    "Return corrected JSON only."
                )
            payload = {
                "model": settings.ollama_model,
                "messages": [
                    {"role": "system", "content": full_system_prompt},
                    {"role": "user", "content": attempt_user_prompt},
                ],
                "stream": False,
                "format": ollama_schema,
                "keep_alive": settings.ollama_keep_alive,
                "options": {
                    "temperature": resolved_temperature,
                    "seed": attempt_seed,
                },
            }
            try:
                body, format_mode = self._send_chat_with_format_fallback(payload)
                parsed = output_model.model_validate_json(self._response_content(body))
                if semantic_validator is not None:
                    is_valid, semantic_error = semantic_validator(parsed)
                    if not is_valid:
                        last_error = semantic_error
                        correction_feedback = semantic_error
                        continue
                metadata = self._metadata(body, attempts=attempt)
                log_event(
                    level="INFO",
                    event="ollama_structured_generation_completed",
                    model=metadata.model,
                    output_schema=output_model.__name__,
                    attempts=metadata.attempts,
                    total_duration_ns=metadata.total_duration_ns,
                    temperature=resolved_temperature,
                    seed=attempt_seed,
                    format_mode=format_mode,
                )
                return parsed, metadata
            except ValidationError as exc:
                last_error = "The response was not valid JSON for the required output schema."
                correction_feedback = last_error
            except httpx.TimeoutException as exc:
                log_event(
                    level="WARNING",
                    event="ollama_request_timeout",
                    error_type=type(exc).__name__,
                    timeout_seconds=settings.ollama_request_timeout_seconds,
                )
                raise OllamaUnavailableError(
                    code="ollama_request_timed_out",
                    message=(
                        "Local Ollama timed out while generating the structured response. "
                        f"The configured timeout is {settings.ollama_request_timeout_seconds} seconds."
                    ),
                ) from exc
            except httpx.HTTPStatusError as exc:
                response_text = (exc.response.text or "")[:500]
                log_event(
                    level="WARNING",
                    event="ollama_http_status_error",
                    error_type=type(exc).__name__,
                    status_code=exc.response.status_code,
                    response_preview=response_text,
                )
                raise OllamaUnavailableError(
                    code="ollama_http_error",
                    message=f"Local Ollama rejected the structured request with HTTP {exc.response.status_code}.",
                ) from exc
            except httpx.RequestError as exc:
                log_event(level="WARNING", event="ollama_request_error", error_type=type(exc).__name__)
                raise OllamaUnavailableError(
                    code="ollama_request_failed",
                    message="Local Ollama could not complete the generation request.",
                ) from exc
            except LLMOutputValidationError as exc:
                last_error = exc.message
                correction_feedback = exc.message

        raise LLMOutputValidationError(
            code="llm_output_validation_failed",
            message=f"Local Ollama could not produce a safe structured response after {settings.ollama_max_attempts} attempt(s). {last_error}",
        )

    def classify_intent(self, question: str) -> tuple[IntentClassificationResult, OllamaInvocationMetadata, dict[str, Any]]:
        glossary = normalize_business_text(question)
        schema_summary = {
            "business_tables": get_schema_contract()["business_tables"],
            "operational_tables": get_schema_contract()["operational_tables"],
        }
        result, metadata = self.generate_json(
            output_model=IntentClassificationResult,
            system_prompt="Classify intent conservatively. Prefer clarification instead of guessing.",
            user_prompt=prompts.render_intent_prompt(question=question, glossary=glossary, schema_summary=schema_summary),
            temperature=self.INTENT_TEMPERATURE,
            seed=self.DEFAULT_OLLAMA_SEED,
        )
        return result, metadata, glossary

    @staticmethod
    def _compact_sql_schema_contract() -> dict[str, Any]:
        """Build the small, business-only schema context used for local SQL generation.

        The complete Phase 3 schema includes operational tables, field descriptions, and
        extensive metadata. It is correct for APIs but unnecessarily large for an 8B local
        model. Passing it in every prompt can exceed the model context window before the
        user question is considered. This compact contract retains approved business table
        names, column names, primary keys, and join relationships only.
        """

        reflected_contract = get_schema_contract()
        reflected_tables = [
            table
            for table in reflected_contract.get("tables", [])
            if table.get("category") == "business" and table.get("allowed_for_llm", True)
        ]
        business_table_names = [str(table["table_name"]) for table in reflected_tables]

        tables: list[dict[str, Any]] = []
        for table_schema in reflected_tables:
            table_name = str(table_schema["table_name"])
            tables.append(
                {
                    "table_name": table_name,
                    "columns": [column["name"] for column in table_schema["columns"]],
                    "primary_key_columns": table_schema["primary_key_columns"],
                }
            )

        relationships = [
            relationship
            for relationship in reflected_contract.get("relationships", [])
            if relationship["from_table"] in business_table_names
            and relationship["to_table"] in business_table_names
        ]
        parent_child_relationships = [
            {
                "parent_table": relationship["to_table"],
                "child_table": relationship["from_table"],
                "parent_key": f"{relationship['to_table']}.{relationship['to_column']}",
                "child_key": f"{relationship['from_table']}.{relationship['from_column']}",
                "cardinality": "one_to_zero_or_many",
                "delete_rule": relationship.get("delete_rule"),
            }
            for relationship in relationships
        ]
        feature_17 = next(
            (
                feature
                for feature in parent_child_relationships
                if feature["parent_table"] == "employees"
                and feature["child_table"] == "employee_permissions"
            ),
            None,
        )
        return {
            "schema_source": "controlled_compact_business_schema_postgresql_reflection",
            "business_tables": business_table_names,
            "tables": tables,
            "relationships": relationships,
            "parent_child_relationships": parent_child_relationships,
            "feature_17": feature_17,
        }

    @staticmethod
    def _expand_safe_single_table_select_star(sql: str) -> str:
        """Replace only a narrow, model-generated SELECT * with explicit columns.

        This is a defensive normalizer for local-model output, not a raw-SQL
        feature. It is deliberately restricted to a simple, one-table SELECT
        without JOIN, set operations, comments, or table-qualified wildcards.
        Any query outside this small pattern continues to the normal validator
        unchanged and is blocked when unsafe.
        """

        stripped_sql = sql.strip()

        if re.search(
            r"\b(JOIN|UNION|INTERSECT|EXCEPT)\b|--|/\*|\*/",
            stripped_sql,
            flags=re.IGNORECASE,
        ):
            return sql

        match = re.match(
            r"^(?P<prefix>SELECT\s+)\*(?P<from_clause>\s+FROM\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)\b)",
            stripped_sql,
            flags=re.IGNORECASE,
        )

        if match is None:
            return sql

        table_name = match.group("table").lower()
        try:
            table_schema = get_table_schema(table_name)
        except KeyError:
            return sql
        if table_schema.get("category") != "business" or not table_schema.get("allowed_for_llm", True):
            return sql

        excluded_columns = {"id", "created_at", "updated_at"}
        approved_columns = [
            column
            for column in get_allowed_columns(table_name)
            if column not in excluded_columns
        ]

        if not approved_columns:
            return sql

        explicit_projection = ", ".join(approved_columns)
        rewritten_sql = (
            f"{match.group('prefix')}"
            f"{explicit_projection}"
            f"{match.group('from_clause')}"
            f"{stripped_sql[match.end():]}"
        )

        log_event(
            level="INFO",
            event="llm_select_star_expanded_to_explicit_columns",
            table_name=table_name,
            selected_column_count=len(approved_columns),
        )

        return rewritten_sql

    def generate_sql(
        self,
        question: str,
        route: str,
        memory_context: dict[str, Any] | None = None,
    ) -> tuple[
        SQLGenerationResult,
        SQLValidationResult,
        OllamaInvocationMetadata,
        dict[str, Any],
    ]:
        """Generate a safe SQL proposal and normalize only a narrow SELECT * case."""

        if route not in {"structured_read", "crud_write"}:
            raise LLMOutputValidationError(
                code="invalid_sql_route",
                message="SQL generation route must be structured_read or crud_write.",
            )

        glossary = normalize_business_text(question)
        schema_contract = self._compact_sql_schema_contract()
        last_validation: SQLValidationResult | None = None

        def safe_sql_for_validation(proposal: SQLGenerationResult) -> str:
            if route == "structured_read":
                return self._expand_safe_single_table_select_star(proposal.sql)
            return proposal.sql

        def semantic_validator(output: BaseModel) -> tuple[bool, str]:
            nonlocal last_validation

            proposal = SQLGenerationResult.model_validate(output)

            if proposal.route != route:
                return False, f"route must equal '{route}'."

            candidate_sql = safe_sql_for_validation(proposal)
            validation = validate_dml_sql(candidate_sql)
            last_validation = validation

            if not validation.is_valid:
                return (
                    False,
                    validation.error_message
                    or "The generated SQL failed the server safety validator.",
                )

            if route == "structured_read":
                if validation.statement_type != "SELECT":
                    return False, "structured_read must generate SELECT only."

                constraint_check = validate_structured_read_constraints(
                    question=question,
                    sql=validation.normalized_sql or candidate_sql,
                )
                if not constraint_check.is_valid:
                    return False, constraint_check.error_message

            if route == "crud_write" and validation.statement_type not in {
                "INSERT",
                "UPDATE",
                "DELETE",
            }:
                return False, "crud_write must generate INSERT, UPDATE, or DELETE only."

            return True, ""

        result, metadata = self.generate_json(
            output_model=SQLGenerationResult,
            system_prompt=(
                "Generate a validator-safe SQL proposal only. The backend validates every proposal "
                "before any use. Never use SELECT *; use explicit controlled columns."
            ),
            user_prompt=prompts.render_sql_prompt(
                question=question,
                route=route,
                glossary=glossary,
                schema_contract=schema_contract,
                memory_context=memory_context,
            ),
            semantic_validator=semantic_validator,
            temperature=self.SQL_TEMPERATURE,
            seed=self.DEFAULT_OLLAMA_SEED,
        )

        final_sql = safe_sql_for_validation(result)
        if final_sql != result.sql:
            result = result.model_copy(update={"sql": final_sql})

        validation = last_validation or validate_dml_sql(result.sql)

        if validation.normalized_sql:
            result = result.model_copy(update={"sql": validation.normalized_sql})

        return result, validation, metadata, glossary


    def generate_admin_schema_sql(
        self, user_prompt: str
    ) -> tuple[AdminSchemaProposalResult, SQLValidationResult, OllamaInvocationMetadata]:
        """Generate only a previewable restricted admin schema proposal from natural language."""

        from app.services.sql_validation import preview_admin_schema_change

        last_validation: SQLValidationResult | None = None

        def semantic_validator(output: BaseModel) -> tuple[bool, str]:
            nonlocal last_validation
            proposal = AdminSchemaProposalResult.model_validate(output)
            validation = preview_admin_schema_change(proposal.sql)
            last_validation = validation
            if not validation.is_valid:
                return False, validation.error_message or "The generated restricted schema SQL failed validation."
            expected_operation = (
                "create_table" if validation.statement_type == "CREATE_TABLE_PREVIEW" else "alter_table_add_column"
            )
            if proposal.operation != expected_operation:
                return False, f"operation must equal '{expected_operation}'."
            return True, ""

        result, metadata = self.generate_json(
            output_model=AdminSchemaProposalResult,
            system_prompt="Generate only a safe restricted admin schema proposal. It will be AST-validated and stored as a preview; it cannot execute itself.",
            user_prompt=prompts.render_admin_schema_prompt(
                user_prompt=user_prompt,
                allowed_tables=get_allowed_tables(),
            ),
            semantic_validator=semantic_validator,
            temperature=self.ADMIN_SCHEMA_TEMPERATURE,
            seed=self.DEFAULT_OLLAMA_SEED,
        )
        return result, last_validation or preview_admin_schema_change(result.sql), metadata

    def extract_record_fields(
        self, instruction: str, target_table: str
    ) -> tuple[RecordExtractionResult, OllamaInvocationMetadata]:
        normalized_table = target_table.strip().lower()
        try:
            table_schema = get_table_schema(normalized_table)
        except KeyError as exc:
            raise LLMOutputValidationError(
                code="unknown_target_table",
                message="The requested target table is not approved for record extraction.",
            ) from exc
        allowed_columns = set(get_allowed_columns(normalized_table)) - {"id", "created_at", "updated_at"}

        def semantic_validator(output: BaseModel) -> tuple[bool, str]:
            result = RecordExtractionResult.model_validate(output)
            if result.table_name != normalized_table:
                return False, f"table_name must equal '{normalized_table}'."
            invalid_columns = sorted(set(result.values) - allowed_columns)
            if invalid_columns:
                return False, f"values contains unapproved or managed column(s): {', '.join(invalid_columns)}."
            return True, ""

        return self.generate_json(
            output_model=RecordExtractionResult,
            system_prompt="Extract only an unexecuted record proposal from the user instruction.",
            user_prompt=prompts.render_record_extraction_prompt(
                instruction=instruction,
                target_table=normalized_table,
                table_schema=table_schema,
            ),
            semantic_validator=semantic_validator,
            temperature=self.RECORD_EXTRACTION_TEMPERATURE,
            seed=self.DEFAULT_OLLAMA_SEED,
        )

    def generate_grounded_answer(
        self, question: str, evidence: list[dict[str, str]]
    ) -> tuple[GroundedAnswerResult, OllamaInvocationMetadata | None]:
        if not evidence:
            return (
                GroundedAnswerResult(
                    answer="Information not available in the provided evidence.",
                    supported=False,
                    source_references=[],
                ),
                None,
            )
        allowed_references = {item["reference"] for item in evidence}

        def answer_is_extractive(answer: str, references: list[str]) -> bool:
            """Ensure generated facts occur verbatim in their cited evidence.

            Validating a source ID alone is insufficient because a model can cite a
            real source while inventing a value. Whitespace and case are ignored, but
            the model may not add or reorder factual words or numbers.
            """

            normalized_sources = [
                re.sub(r"\s+", " ", item["content"]).strip().casefold()
                for item in evidence
                if item["reference"] in references
            ]
            sentences = [
                re.sub(r"\s+", " ", sentence).strip().casefold()
                for sentence in re.split(r"(?<=[.!?])\s+|\n+", answer)
                if sentence.strip()
            ]
            return bool(sentences) and all(
                any(sentence in source for source in normalized_sources)
                for sentence in sentences
            )

        def semantic_validator(output: BaseModel) -> tuple[bool, str]:
            result = GroundedAnswerResult.model_validate(output)
            if not result.supported:
                if result.answer != "Information not available in the provided evidence.":
                    return False, "Unsupported answers must use the exact unavailable-answer text."
                if result.source_references:
                    return False, "Unsupported answers cannot cite sources."
                return True, ""
            if not result.source_references:
                return False, "Supported answers must include at least one source reference."
            unknown = sorted(set(result.source_references) - allowed_references)
            if unknown:
                return False, f"source_references contains unknown source(s): {', '.join(unknown)}."
            if not answer_is_extractive(result.answer, result.source_references):
                return False, "Every answer sentence must be copied from one of its cited evidence records."
            return True, ""

        return self.generate_json(
            output_model=GroundedAnswerResult,
            system_prompt="Generate a grounded answer only from evidence supplied by the backend.",
            user_prompt=prompts.render_grounded_answer_prompt(question=question, evidence=evidence),
            semantic_validator=semantic_validator,
            temperature=self.GROUNDED_ANSWER_TEMPERATURE,
            seed=self.DEFAULT_OLLAMA_SEED,
        )


llm_service = LLMService()
