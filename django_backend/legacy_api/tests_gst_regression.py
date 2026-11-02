"""Registration-based GST regressions for the shared rule and customer cart flows."""
import inspect
from decimal import Decimal

from django.test import RequestFactory, SimpleTestCase

from legacy_api import views_commerce
from legacy_api.tests_auth import BEARER, body_of
from legacy_api.tests_cart import PRODUCTS, FakeCartDb, add, cart_env, fetch_cart
from legacydb import gst_rules
from legacydb.gst_rules import (calculate_gst_amount, customer_gst_rate, effective_gst_settings, validate_gst_configuration)

rf = RequestFactory()
D = Decimal


def _summary(cart):
    return cart["cart_items"]["final_summary"]


def line(amount, qty=1, pct=5, **extra):
    kwargs = dict(quantity=qty, product_gst_applicable="Y", variant_gst="Y", is_variant=True, configured_amount=amount, customer_gst_registered="Y", customer_gst_percentage=pct)
    kwargs.update(extra)
    return calculate_gst_amount(**kwargs)


class CentralRuleTests(SimpleTestCase):
    def test_business_example_120_135_145_totals_400(self):
        lines = [line(120), line(135), line(145)]
        self.assertEqual(lines, [D("120.00"), D("135.00"), D("145.00")])
        self.assertEqual(sum(lines), D("400.00"))

    def test_rule_has_no_selling_price_input(self):
        for fn in (calculate_gst_amount, effective_gst_settings):
            self.assertFalse({"unit_price", "price", "selling_price"} & set(inspect.signature(fn).parameters))

    def test_quantity_multiplies_the_configured_amount(self):
        self.assertEqual(line(120, qty=2), D("240.00"))
        self.assertEqual(line(135, qty=3), D("405.00"))
        self.assertEqual(line(120, qty=0), D("0.00"))
        self.assertEqual(line(120, qty=-4), D("0.00"))

    def test_amount_is_rounded_half_up_to_cents(self):
        self.assertEqual(line(D("4.255")), D("4.26"))
        self.assertEqual(line(D("8.505")), D("8.51"))
        self.assertEqual(line(D("0.105")), D("0.11"))

    def test_product_flag_is_required_even_when_variant_flag_is_enabled(self):
        for flag in ("N", "n", None, "", "maybe", 0):
            self.assertEqual(line(120, product_gst_applicable=flag), D("0.00"), flag)

    def test_variant_flag_is_also_required(self):
        for flag in ("N", None, "", "x"):
            self.assertEqual(line(120, variant_gst=flag), D("0.00"), flag)

    def test_product_without_variants_uses_product_amount(self):
        self.assertEqual(calculate_gst_amount(quantity=2, product_gst_applicable="Y", is_variant=False, configured_amount=50, customer_gst_registered="Y"), D("100.00"))
        self.assertEqual(calculate_gst_amount(quantity=2, product_gst_applicable="N", is_variant=False, configured_amount=50, customer_gst_registered="Y"), D("0.00"))

    def test_registration_flag_is_required(self):
        for flag in ("N", None, "", "yes", 1):
            self.assertEqual(line(120, customer_gst_registered=flag), D("0.00"), flag)

    def test_amount_alone_never_enables_gst(self):
        self.assertEqual(line(0), D("0.00"))
        self.assertEqual(line(None), D("0.00"))
        self.assertEqual(line(-5), D("0.00"))

    def test_customer_percentage_is_ignored(self):
        for pct in (None, "", 0, 5, 18, 100, -5, 101, "abc", "NaN", "Infinity"):
            self.assertEqual(line(120, pct=pct), D("120.00"), repr(pct))
            self.assertEqual(customer_gst_rate(pct), D("0.00"), repr(pct))

    def test_enabled_settings_record_full_configured_amount(self):
        s = effective_gst_settings(product_gst_applicable="Y", variant_gst="Y", is_variant=True, configured_amount=9, customer_gst_registered="Y", customer_gst_percentage=5)
        self.assertEqual((s["applicable"], s["percentage"], s["amount"]), ("Y", D("100"), D("9")))

    def test_disabled_settings_report_zero_numbers(self):
        s = effective_gst_settings(product_gst_applicable="N", configured_amount=9, customer_gst_registered="Y")
        self.assertEqual(s, {"applicable": "N", "percentage": D("0.00"), "amount": D("0.00")})

    def test_product_percentage_column_is_not_an_input(self):
        self.assertNotIn("gst_percentage", inspect.signature(effective_gst_settings).parameters)
        self.assertNotIn("gst_percentage", inspect.signature(calculate_gst_amount).parameters)

    def test_admin_validation_gives_clear_errors(self):
        self.assertIn("between 0 and 100", validate_gst_configuration(percentage=150))
        self.assertIn("cannot be negative", validate_gst_configuration(amount=-1))
        self.assertIn("finite", validate_gst_configuration(percentage="abc"))
        self.assertIsNone(validate_gst_configuration(percentage=18, amount=0))

    def test_module_constants_have_no_gst_rate(self):
        self.assertFalse([n for n in dir(gst_rules) if "RATE" in n.upper() and n != "customer_gst_rate"])


class CartCheckoutFlowTests(SimpleTestCase):
    def _toggle(self, mapping, key, value):
        previous = mapping[key]
        mapping[key] = value
        self.addCleanup(mapping.__setitem__, key, previous)

    def test_enabled_to_disabled_then_back_recalculates_current_cart(self):
        db = FakeCartDb()
        with cart_env(db):
            add(15, (0, 2))
            enabled = _summary(fetch_cart())["GST"]
            self._toggle(PRODUCTS[15], "gst_applicable", "N")
            disabled = _summary(fetch_cart())
            checkout = body_of(views_commerce.checkout(rf.get("/api/checkout", **BEARER)))["checkout_data"]["cart_summary"]
            self._toggle(PRODUCTS[15], "gst_applicable", "Y")
            re_enabled = _summary(fetch_cart())["GST"]
        self.assertEqual(enabled, 40.0)
        self.assertEqual(disabled["GST"], 0.0)
        self.assertEqual(disabled["order_total_before_credit"], disabled["original_price"])
        self.assertEqual(checkout["gst_amount"], 0.0)
        self.assertEqual(re_enabled, 40.0)

    def test_client_supplied_gst_values_cannot_override_backend(self):
        db = FakeCartDb()
        self._toggle(PRODUCTS[15], "gst_applicable", "N")
        forged = "?gst=999&GST=999&gst_amount=999&gst_percentage=28&total=1&payable_amount=1"
        with cart_env(db):
            add(15, (0, 2))
            cart = body_of(views_commerce.cart_api_test(rf.get("/api/cart" + forged, **BEARER)))
            checkout = body_of(views_commerce.checkout(rf.get("/api/checkout" + forged, **BEARER)))["checkout_data"]["cart_summary"]
        self.assertEqual(_summary(cart)["GST"], 0.0)
        self.assertEqual(_summary(cart)["payable_amount"], 200.0)
        self.assertEqual(checkout["gst_amount"], 0.0)

    def test_gst_is_added_once_on_top_of_the_list_price(self):
        db = FakeCartDb()
        with cart_env(db):
            add(15, (0, 1))
            summary = _summary(fetch_cart())
        self.assertEqual(summary["items_subtotal"], 100.0)
        self.assertEqual(summary["GST"], 20.0)
        self.assertEqual(summary["order_total_before_credit"], 120.0)

    def test_flutter_summary_keys_remain_present(self):
        db = FakeCartDb()
        with cart_env(db):
            add(15, (0, 1))
            cart = fetch_cart()
            checkout = body_of(views_commerce.checkout(rf.get("/api/checkout", **BEARER)))["checkout_data"]["cart_summary"]
        for key in ("GST", "items_subtotal", "original_price", "order_total_before_credit", "payable_amount", "credit_release", "delivery", "vip_discount", "final_total_price", "total_cart_items"):
            self.assertIn(key, _summary(cart))
        for key in ("gst_amount", "total_with_gst", "total_price", "delivery", "currency", "total_items", "total_quantity"):
            self.assertIn(key, checkout)
        item = next(iter(cart["cart_items"]["discount_not_available"].values()))["items"][0]
        self.assertIn("gst_amount", item)


from legacy_api.cart_discount_testing import module_fixtures
setUpModule, tearDownModule = module_fixtures()
