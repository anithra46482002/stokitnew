"""Central GST rule: registered customers pay the configured per-unit GST amount.

GST is zero unless the authenticated customer has users.gst = Y and the product GST flag is Y. Variant lines also require the variant GST flag to be Y. For eligible lines, GST is configured gst_price multiplied by quantity, rounded half-up to cents. Customer gst_percentage and selling price are not calculation inputs.

The saved customer registration flag is supplied by backend database queries, never by client data.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal("100")


def _decimal(value):
    """Decimal of `value`, or None when it is missing/blank/unparseable/non-finite."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() else None


def _flag(value):
    return str(value or "N").strip().upper() == "Y"


def customer_gst_rate(value):
    """Deprecated compatibility helper; saved customer percentages do not determine GST."""
    return ZERO


def effective_gst_settings(*, product_gst_applicable, configured_amount=None, variant_gst="N", is_variant=False,
                           customer_gst_registered="N", customer_gst_percentage=None):
    disabled = {"applicable": "N", "percentage": ZERO, "amount": ZERO}
    # GST is charged only when the authenticated customer is GST-registered and product/variant GST is enabled.
    if str(customer_gst_registered or "N").strip().upper() != "Y":
        return disabled
    if not _flag(product_gst_applicable):
        return disabled
    if is_variant and not _flag(variant_gst):
        return disabled
    amount = _decimal(configured_amount)
    if amount is None or amount <= 0:
        return disabled
    return {"applicable": "Y", "percentage": HUNDRED, "amount": amount}


def calculate_gst_amount(*, quantity, product_gst_applicable, variant_gst="N", is_variant=False,
                         configured_amount=None, customer_gst_registered="N", customer_gst_percentage=None):
    """Return configured per-unit GST x quantity only for GST-registered customers and taxable products.

    customer_gst_percentage is retained for call compatibility but does not control GST charging.
    """
    settings = effective_gst_settings(
        product_gst_applicable=product_gst_applicable, configured_amount=configured_amount,
        variant_gst=variant_gst, is_variant=is_variant, customer_gst_registered=customer_gst_registered,
        customer_gst_percentage=customer_gst_percentage)
    if settings["applicable"] != "Y":
        return ZERO
    qty_value = _decimal(quantity)
    qty = max(0, int(qty_value)) if qty_value is not None else 0
    return (settings["amount"] * qty).quantize(CENT, rounding=ROUND_HALF_UP) if qty else ZERO


def validate_gst_configuration(*, percentage=None, amount=None):
    """Admin-side validation of saved values. Returns an error message, or None when acceptable."""
    pct, amt = _decimal(percentage), _decimal(amount)
    if percentage not in (None, "") and pct is None:
        return "GST percentage must be a finite number."
    if amount not in (None, "") and amt is None:
        return "GST amount must be a finite number."
    if pct is not None and (pct < 0 or pct > HUNDRED):
        return "GST percentage must be between 0 and 100."
    if amt is not None and amt < 0:
        return "GST amount cannot be negative."
    return None
