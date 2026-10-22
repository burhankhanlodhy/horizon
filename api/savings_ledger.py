"""Shared savings total for analytics, plan limits and fee calculations.

Graph rows measure a retrieval alternative, not a comparable whole-session
counterfactual. Retain their diagnostics, but exclude both positive and negative
historical estimates from monetary totals until session savings are validated.
"""

SAVINGS_ELIGIBLE = "COALESCE(data->>'kind','request') <> 'graph_estimate'"
SAVINGS_USD = (
    "COALESCE(SUM(CAST(NULLIF(data->>'savings_usd','') AS numeric)) "
    f"FILTER (WHERE {SAVINGS_ELIGIBLE}),0)"
)
