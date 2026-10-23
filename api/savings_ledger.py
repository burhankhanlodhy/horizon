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

# Savings-fee fairness: a subscription-login (oauth) or ContextShrink Code
# (subscription) request saves tokens on a plan the user already pays for, so it
# is not a payg dollar-savings event. The hosted proxy tags each event's
# auth_mode; events from older proxies carry no tag and count as payg.
FEE_ELIGIBLE_SAVINGS_USD = (
    "COALESCE(SUM(CAST(NULLIF(data->>'savings_usd','') AS numeric)) "
    f"FILTER (WHERE {SAVINGS_ELIGIBLE} AND COALESCE(data->>'auth_mode','payg') = 'payg'),0)"
)
