# Phase 16 Query Filter Guard Hotfix

## Issue corrected

A local model can generate a SQL statement that is syntactically safe but semantically too broad.
For example, a request for `Show employees from Mars` must not become a query for all active employees.

## Protection added

For explicit location language such as `from Mars`, `live in Bangalore`, or `based in Chennai`, the backend now:

1. extracts the requested city value from the natural-language question;
2. requires the proposed `SELECT` to contain a matching approved `city` predicate;
3. returns validator feedback to the local model for one correction retry if the filter was dropped;
4. stops the database read if the model still fails to preserve the filter.

The normal SQL AST validator still handles SQL safety, schema allowlists, statement limits, and forbidden commands. This hotfix adds semantic filter preservation on top of that safety layer.

## Expected Mars behavior

```text
Show employees from Mars
→ generated SELECT must contain city = 'Mars'
→ PostgreSQL returns zero rows
→ API returns information_not_available
```

It must never return all active employees merely because the model omitted the requested city filter.
