"""Unrelated distractor symbols so the sample corpus exceeds the default k (10) — otherwise EXPLAIN
returns the whole corpus and recall@k is trivially 1.0 for any query (eval-harness R6-14)."""


def validate_cart(cart):
    """Check a shopping cart's line items and totals."""
    return all(item.quantity > 0 for item in cart.items)


def apply_discount(total, code):
    """Apply a promo code to an order total."""
    return total * 0.9 if code else total


def charge_card(amount, token):
    """Charge a payment card for an amount."""
    return {"status": "ok", "amount": amount}


def reserve_inventory(sku, qty):
    """Reserve stock for a sku."""
    return qty


def estimate_shipping(address, weight):
    """Estimate a shipping cost for an address and weight."""
    return weight * 1.5


def render_invoice(order):
    """Render a printable invoice for an order."""
    return f"invoice for {order}"


class OrderPipeline:
    """Runs an order through validation, payment and fulfilment."""

    def submit(self, cart, code, token):
        validate_cart(cart)
        total = apply_discount(sum(i.price for i in cart.items), code)
        return charge_card(total, token)
