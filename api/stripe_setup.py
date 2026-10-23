"""One-time, idempotent Stripe setup for ContextShrink billing.

Creates (only when missing):
  * the "ContextShrink Pro" product and its $0/month price (lookup key
    STRIPE_PRO_PRICE_LOOKUP_KEY), which Checkout uses to save a card;
  * the "ContextShrink Code" product and its $5/month price (lookup key
    STRIPE_CODE_PRICE_LOOKUP_KEY);
  * a Customer Portal configuration (card updates, invoices, cancel at
    period end);
  * with --webhook-url, a webhook endpoint for the events billing.py handles.

Progress goes to stderr. Secrets are never printed, except that
--print-webhook-secret writes a newly created endpoint's signing secret to
stdout so the caller can pipe it straight into a protected .env file.

Run inside the API container, e.g.:
  docker exec contextshrink-api python stripe_setup.py
"""

import argparse
import os
import sys

import stripe

PRO_PRICE_LOOKUP_KEY = os.environ.get("STRIPE_PRO_PRICE_LOOKUP_KEY", "contextshrink_pro_monthly")
CODE_PRICE_LOOKUP_KEY = os.environ.get("STRIPE_CODE_PRICE_LOOKUP_KEY", "contextshrink_code_monthly")
WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.paused",
    "customer.subscription.resumed",
    "invoice.created",
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.voided",
]


def log(message: str) -> None:
    print(message, file=sys.stderr)


def ensure_pro_price(client: stripe.StripeClient) -> None:
    found = client.v1.prices.list({"lookup_keys": [PRO_PRICE_LOOKUP_KEY], "limit": 1})
    if found.data:
        log(f"Pro price exists: {found.data[0].id}")
        return
    product = client.v1.products.create(
        {
            "name": "ContextShrink Pro",
            "description": "Unlimited compression and Advanced Analytics. "
            "5% of monthly savings, only when savings exceed $20.",
        },
        {"idempotency_key": "contextshrink-pro-product-v1"},
    )
    price = client.v1.prices.create(
        {
            "product": product.id,
            "currency": "usd",
            "unit_amount": 0,
            "recurring": {"interval": "month"},
            "lookup_key": PRO_PRICE_LOOKUP_KEY,
            "nickname": "Pro monthly ($0 base)",
        },
        {"idempotency_key": "contextshrink-pro-price-v1"},
    )
    log(f"Created Pro product {product.id} and price {price.id}")


def ensure_code_price(client: stripe.StripeClient) -> None:
    found = client.v1.prices.list({"lookup_keys": [CODE_PRICE_LOOKUP_KEY], "limit": 1})
    if found.data:
        log(f"Code price exists: {found.data[0].id}")
        return
    product = client.v1.products.create(
        {
            "name": "ContextShrink Code",
            "description": "Extra usage for Claude Code and Codex. 7 days free, then $5 a month. "
            "Up to 2 devices.",
        },
        {"idempotency_key": "contextshrink-code-product-v1"},
    )
    price = client.v1.prices.create(
        {
            "product": product.id,
            "currency": "usd",
            "unit_amount": 500,
            "recurring": {"interval": "month"},
            "lookup_key": CODE_PRICE_LOOKUP_KEY,
            "nickname": "Code monthly",
        },
        {"idempotency_key": "contextshrink-code-price-v1"},
    )
    log(f"Created Code product {product.id} and price {price.id}")


def ensure_portal(client: stripe.StripeClient, return_url: str) -> None:
    existing = client.v1.billing_portal.configurations.list({"limit": 1})
    if existing.data:
        log(f"Customer Portal configuration exists: {existing.data[0].id}")
        return
    config = client.v1.billing_portal.configurations.create(
        {
            "default_return_url": return_url,
            "features": {
                "invoice_history": {"enabled": True},
                "payment_method_update": {"enabled": True},
                "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
            },
        },
        {"idempotency_key": "contextshrink-portal-v1"},
    )
    log(f"Created Customer Portal configuration {config.id}")


def ensure_webhook(client: stripe.StripeClient, url: str, print_secret: bool) -> None:
    for endpoint in client.v1.webhook_endpoints.list({"limit": 100}).auto_paging_iter():
        if endpoint.url == url:
            log(f"Webhook endpoint exists: {endpoint.id} (its secret is only shown at creation)")
            if set(endpoint.enabled_events) != set(WEBHOOK_EVENTS):
                client.v1.webhook_endpoints.update(endpoint.id, {"enabled_events": WEBHOOK_EVENTS})
                log("Updated its events to: " + ", ".join(WEBHOOK_EVENTS))
            return
    endpoint = client.v1.webhook_endpoints.create(
        {"url": url, "enabled_events": WEBHOOK_EVENTS, "description": "ContextShrink billing"}
    )
    log(f"Created webhook endpoint {endpoint.id}")
    if print_secret:
        print(endpoint.secret)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--webhook-url")
    parser.add_argument("--print-webhook-secret", action="store_true")
    args = parser.parse_args()
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key.startswith(("sk_test_", "rk_test_", "sk_live_", "rk_live_")):
        sys.exit("STRIPE_SECRET_KEY is not set")
    log("Mode: " + ("TEST" if "_test_" in key else "LIVE"))
    client = stripe.StripeClient(key)
    app_url = os.environ.get("APP_URL", "https://app.contextshrink.com").rstrip("/")
    ensure_pro_price(client)
    ensure_code_price(client)
    ensure_portal(client, f"{app_url}/subscriptions")
    if args.webhook_url:
        ensure_webhook(client, args.webhook_url, args.print_webhook_secret)


if __name__ == "__main__":
    main()
