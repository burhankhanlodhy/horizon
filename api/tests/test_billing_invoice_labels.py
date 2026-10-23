"""Fallback labels for invoices that have no description (no Stripe network calls)."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billing  # noqa: E402
import code_plan  # noqa: E402


class _Metadata:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data)


def _invoice(product=None, description=None):
    details = None
    if product is not None:
        details = SimpleNamespace(
            subscription="sub_123",
            metadata=_Metadata({"product": product} if product else {}),
        )
    parent = SimpleNamespace(type="subscription_details", subscription_details=details)
    return SimpleNamespace(id="in_1", description=description, parent=parent)


def test_code_subscription_invoices_are_labelled_as_code():
    inv = _invoice(product="code")
    assert code_plan.invoice_is_code(inv)
    assert billing._subscription_invoice_label(inv) == "ContextShrink Code subscription"


def test_pro_subscription_invoices_keep_the_pro_label():
    inv = _invoice(product="pro")
    assert not code_plan.invoice_is_code(inv)
    assert billing._subscription_invoice_label(inv) == "ContextShrink Pro subscription"


def test_invoices_without_subscription_details_keep_the_pro_label():
    inv = SimpleNamespace(id="in_2", description=None, parent=None)
    assert not code_plan.invoice_is_code(inv)
    assert billing._subscription_invoice_label(inv) == "ContextShrink Pro subscription"


def test_invoices_with_empty_metadata_keep_the_pro_label():
    inv = _invoice(product="")
    assert billing._subscription_invoice_label(inv) == "ContextShrink Pro subscription"
