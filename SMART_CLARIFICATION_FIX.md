# Smart Clarification and Context Fix

This patch prevents unnecessary clarification loops while preserving the no-guess rule.

## Behaviour

- `Show workers from Bangalore` resolves directly to `employees` with `city = Bangalore`.
- Clear table aliases such as worker/employee/vendor/supplier/customer/client are grounded deterministically.
- Ollama still interprets the sentence, but Python repairs only facts that are explicit and uniquely resolvable.
- A low model confidence does not force a question when the current sentence itself provides a clear action, table, and filter.
- Genuine ambiguity still produces `clarification_required`.
- Short answers to a clarification fill the missing slot instead of starting a new unrelated request.
  - `show records` -> `Which table?` -> `vendors` resumes the original request.
  - `vendors table` -> `Rows or columns?` -> `review rows` displays vendor rows.
- New standalone questions still do not inherit old filters.

## Safety

- Ollama does not generate SQL in the semantic path.
- SQLAlchemy compiles grounded plans.
- Existing SQL validation, MCP boundaries, confirmation gates, and audit logging remain active.
