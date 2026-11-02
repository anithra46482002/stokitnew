"""Regression tests for customer-registration-based cart and checkout GST."""
from decimal import Decimal
from unittest import mock
from django.test import SimpleTestCase
from legacy_api import views_commerce
from legacy_api.tests_cart import PRODUCTS, VARIANTS, FakeCartDb, add, cart_env, fetch_cart, rf
from legacy_api.tests_auth import BEARER, body_of
from legacydb.gst_rules import calculate_gst_amount

NEW_PRODUCTS = {
    31: {"name": "Twenty Percent Pen", "price": 150.0, "view": "1", "gst_applicable": "Y", "gst_price": 30.0},
    32: {"name": "Exempt Product", "price": 150.0, "view": "1", "gst_applicable": "N", "gst_price": 30.0},
    36: {"name": "Variant Box", "price": 1.0, "view": "1", "gst_applicable": "Y", "gst_price": 0},
}
NEW_VARIANTS = {
    301: {"product_id": 31, "name": "Standard", "price": 150.0, "gst": "Y", "gst_price": 30.0},
    302: {"product_id": 32, "name": "Exempt", "price": 150.0, "gst": "Y", "gst_price": 30.0},
    306: {"product_id": 36, "name": "Big", "price": 200.0, "gst": "Y", "gst_price": 36.0},
}

def line_gst(cart):
    return [item["gst_amount"] for bucket in ("discount_available", "discount_not_available")
            for group in cart["cart_items"][bucket].values() for item in group["items"]]

def checkout_preview():
    return body_of(views_commerce.checkout(rf.get("/api/checkout", **BEARER)))["checkout_data"]["cart_summary"]

class CustomerRegistrationGstTests(SimpleTestCase):
    def setUp(self):
        for mapping, extra in ((PRODUCTS, NEW_PRODUCTS), (VARIANTS, NEW_VARIANTS)):
            patcher = mock.patch.dict(mapping, extra)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cart(self, registered, lines):
        db = FakeCartDb(customer_gst_percentage=Decimal("5"))
        db.customer_registered = registered
        with cart_env(db):
            for product_id, variant_id, qty in lines:
                add(product_id, (variant_id, qty))
            return fetch_cart(), checkout_preview()

    def test_unregistered_customer_pays_no_gst(self):
        cart, checkout = self.run_cart("N", [(31, 301, 1)])
        self.assertEqual(line_gst(cart), [0.0])
        self.assertEqual(cart["cart_items"]["final_summary"]["GST"], 0.0)
        self.assertEqual(cart["cart_items"]["final_summary"]["order_total_before_credit"], 150.0)
        self.assertEqual(checkout["gst_amount"], 0.0)

    def test_registered_customer_pays_configured_gst_amount(self):
        cart, checkout = self.run_cart("Y", [(31, 301, 1)])
        self.assertEqual(line_gst(cart), [30.0])
        self.assertEqual(cart["cart_items"]["final_summary"]["GST"], 30.0)
        self.assertEqual(cart["cart_items"]["final_summary"]["order_total_before_credit"], 180.0)
        self.assertEqual(checkout["gst_amount"], 30.0)
        self.assertEqual(checkout["total_with_gst"], 180.0)

    def test_switching_registration_recalculates_the_same_cart(self):
        db = FakeCartDb()
        with cart_env(db):
            add(31, (301, 1))
            db.customer_registered = "N"
            self.assertEqual(fetch_cart()["cart_items"]["final_summary"]["GST"], 0.0)
            db.customer_registered = "Y"
            self.assertEqual(fetch_cart()["cart_items"]["final_summary"]["GST"], 30.0)
            db.customer_registered = "N"
            self.assertEqual(checkout_preview()["gst_amount"], 0.0)

    def test_exempt_product_and_variant_remain_zero_for_both_customer_types(self):
        for registered in ("N", "Y"):
            cart, checkout = self.run_cart(registered, [(32, 302, 1)])
            self.assertEqual(line_gst(cart), [0.0])
            self.assertEqual(checkout["gst_amount"], 0.0)

    def test_variant_and_quantity_use_configured_per_unit_amount(self):
        cart, checkout = self.run_cart("Y", [(36, 306, 2)])
        self.assertEqual(line_gst(cart), [72.0])
        self.assertEqual(cart["cart_items"]["final_summary"]["GST"], 72.0)
        self.assertEqual(checkout["gst_amount"], 72.0)

    def test_shared_rule_requires_registered_customer_and_enabled_product(self):
        kwargs = dict(quantity=1, product_gst_applicable="Y", variant_gst="Y", is_variant=True, configured_amount=30)
        self.assertEqual(calculate_gst_amount(**kwargs, customer_gst_registered="N"), Decimal("0.00"))
        self.assertEqual(calculate_gst_amount(**kwargs, customer_gst_registered="Y"), Decimal("30.00"))
        self.assertEqual(calculate_gst_amount(**{**kwargs, "product_gst_applicable": "N"}, customer_gst_registered="Y"), Decimal("0.00"))

from legacy_api.cart_discount_testing import module_fixtures
setUpModule, tearDownModule = module_fixtures()