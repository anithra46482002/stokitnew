"""Order GST snapshots and historical display under registration-based GST rules."""
from contextlib import ExitStack, nullcontext
from decimal import Decimal
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, override_settings

from legacy_api import views_finance, views_orders
from legacy_api.tests_auth import BEARER, authed, body_of, post
from legacydb import order_gst_snapshot as snap
from legacydb.order_gst_snapshot import SOURCE_LEGACY, SOURCE_PARTIAL, SOURCE_PRE_MIGRATION, SOURCE_SNAPSHOT

rf = RequestFactory()
D = Decimal


def snapshot_columns(present):
    stack = ExitStack()
    stack.enter_context(mock.patch.object(snap, "snapshot_available", return_value=present))
    stack.enter_context(mock.patch.object(views_orders, "snapshot_available", return_value=present))
    return stack


def must_not_recalc(_row):
    raise AssertionError("current product or customer settings must not be consulted for a snapshot-era order")


class SnapshotRuleTests(SimpleTestCase):
    def test_snapshot_era_order_uses_stored_amounts_and_never_recalculates(self):
        with snapshot_columns(True):
            total, source = snap.order_gst([{"snap_gst_amount": D("18.00")}, {"snap_gst_amount": D("0.00")}], must_not_recalc)
        self.assertEqual((total, source), (D("18.00"), SOURCE_SNAPSHOT))

    def test_legacy_orders_are_not_invented_or_recomputed(self):
        with snapshot_columns(True):
            total, source = snap.order_gst([{"snap_gst_amount": None}], must_not_recalc)
        self.assertEqual((total, source), (D("0.00"), SOURCE_LEGACY))

    def test_partial_snapshot_is_reported_explicitly(self):
        with snapshot_columns(True):
            total, source = snap.order_gst([{"snap_gst_amount": D("5.00")}, {"snap_gst_amount": None}], must_not_recalc)
        self.assertEqual((total, source), (D("5.00"), SOURCE_PARTIAL))

    def test_before_migration_uses_the_explicit_pre_snapshot_fallback(self):
        with snapshot_columns(False):
            total, source = snap.order_gst([{"x": 1}], lambda row: D("7.00"))
        self.assertEqual((total, source), (D("7.00"), SOURCE_PRE_MIGRATION))

    def test_line_snapshot_uses_registration_and_full_configured_amount(self):
        enabled = snap.line_snapshot(quantity=2, product_gst_applicable="Y", variant_gst="Y", is_variant=True,
                                     customer_gst_registered="Y", customer_gst_percentage=5, configured_amount=20)
        self.assertEqual((enabled["gst_applicable"], enabled["gst_percentage"], enabled["gst_unit_amount"], enabled["gst_amount"]),
                         ("Y", D("100"), D("20"), D("40.00")))
        disabled = snap.line_snapshot(quantity=2, product_gst_applicable="Y", variant_gst="Y", is_variant=True,
                                      customer_gst_registered="N", configured_amount=9)
        self.assertEqual((disabled["gst_applicable"], disabled["gst_percentage"], disabled["gst_unit_amount"], disabled["gst_amount"]),
                         ("N", D("0.00"), D("0.00"), D("0.00")))


class NewOrderSnapshotTests(SimpleTestCase):
    user = {"fullname": "T", "phone": "1", "address": "a", "email": "t@example.invalid",
            "delivery_charges": D("0.00"), "vip_discount": "N", "vip_percentage": D("0"),
            "gst": "Y", "gst_percentage": D("5")}
    item = {"product_id": 15, "variant_id": 0, "qty": 2, "product_name": "Configured GST", "has_disc": 0,
            "discount_percent": 0, "discount_value": 0, "product_discount": 0, "product_gst_applicable": "Y",
            "gst_price": D("20.00"), "variant_gst": "N", "variant_name": "", "price": D("100.00"), "stock_qty": 1}

    def setUp(self):
        patcher = mock.patch.object(views_orders, "apply_order_credit", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def place(self, item, present, forged=None, user=None):
        items = item if isinstance(item, list) else [item]
        request = rf.post("/api/order_post", data=forged or {}, **BEARER)
        with authed(), snapshot_columns(present), override_settings(STOKIT_MIN_ORDER_VALUE=D("0.00")), \
                mock.patch.object(views_orders.transaction, "atomic", return_value=nullcontext()), \
                mock.patch.object(views_orders, "fetchone", return_value=user or self.user), \
                mock.patch.object(views_orders, "fetchall", return_value=items), \
                mock.patch.object(views_orders, "_insert_checkout_with_unique_id", return_value=500), \
                mock.patch.object(views_orders, "execute") as execute:
            response = views_orders.order_post(request)
        inserts = [call for call in execute.call_args_list if "INSERT INTO zg_order_items" in call.args[0]]
        return response, inserts

    def variant_item(self, variant_id, amount, price, qty=1, **extra):
        return {**self.item, "product_id": 16, "variant_id": variant_id, "variant_name": f"V{variant_id}",
                "qty": qty, "variant_gst": "Y", "gst_price": D(str(amount)), "price": D(str(price)),
                "stock_qty": 50, **extra}

    def test_new_order_stores_registration_based_snapshot_once_columns_exist(self):
        response, inserts = self.place(self.item, True)
        self.assertEqual(response.status_code, 200)
        sql, params = inserts[0].args
        self.assertIn("gst_amount", sql)
        self.assertIn("gst_snapshot_at", sql)
        self.assertEqual(sql.count("%s"), len(params))
        self.assertEqual(params[-4:], ["Y", D("100"), D("20.00"), D("40.00")])

    def test_unregistered_customer_stores_zero_gst_snapshot(self):
        response, inserts = self.place(self.item, True, user={**self.user, "gst": "N"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(inserts[0].args[1][-4:], ["N", D("0.00"), D("0.00"), D("0.00")])

    def test_order_placement_business_example_totals_400(self):
        items = [self.variant_item(201, 120, 999), self.variant_item(202, 135, 50), self.variant_item(203, 145, 1000)]
        response, inserts = self.place(items, True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([call.args[1][-1] for call in inserts], [D("120.00"), D("135.00"), D("145.00")])
        self.assertEqual(sum(call.args[1][-1] for call in inserts), D("400.00"))

    def test_order_gst_agrees_with_cart_and_checkout(self):
        from legacy_api.tests_cart import FakeCartDb, add, cart_env, fetch_cart
        db = FakeCartDb()
        with cart_env(db):
            add(16, (201, 1), (202, 1), (203, 1))
            cart_gst = fetch_cart()["cart_items"]["final_summary"]["GST"]
        items = [self.variant_item(201, 120, 999), self.variant_item(202, 135, 50), self.variant_item(203, 145, 1000)]
        _, inserts = self.place(items, True)
        self.assertEqual(float(sum(call.args[1][-1] for call in inserts)), cart_gst)

    def test_quantity_two_doubles_order_line_gst(self):
        _, inserts = self.place(self.variant_item(201, 120, 999, qty=2), True)
        self.assertEqual(inserts[0].args[1][-1], D("240.00"))

    def test_customer_percentage_does_not_change_order_gst(self):
        for pct in (D("5"), D("10"), D("12.5"), D("0"), None, "abc"):
            _, inserts = self.place(self.item, True, user={**self.user, "gst_percentage": pct})
            self.assertEqual(inserts[0].args[1][-4:], ["Y", D("100"), D("20.00"), D("40.00")])

    def test_variant_disabled_stores_zero_for_that_variant_only(self):
        items = [self.variant_item(201, 120, 999), self.variant_item(202, 135, 50, variant_gst="N")]
        _, inserts = self.place(items, True)
        self.assertEqual([call.args[1][-1] for call in inserts], [D("120.00"), D("0.00")])

    def test_forged_client_gst_values_cannot_change_snapshot(self):
        forged = {"gst_amount": "999", "gst": "N", "gst_percentage": "0", "customer_gst_percentage": "0"}
        _, inserts = self.place(self.item, True, forged=forged)
        self.assertEqual(inserts[0].args[1][-4:], ["Y", D("100"), D("20.00"), D("40.00")])

    def test_before_snapshot_columns_exist_insert_remains_legacy_shape(self):
        response, inserts = self.place(self.item, False)
        self.assertEqual(response.status_code, 200)
        sql, params = inserts[0].args
        self.assertNotIn("gst_", sql)
        self.assertEqual(len(params), 9)


    def test_registered_customer_with_product_gst_disabled_stores_zero_snapshot(self):
        item = {**self.item, "product_gst_applicable": "N"}
        response, inserts = self.place(item, True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(inserts[0].args[1][-4:], ["N", D("0.00"), D("0.00"), D("0.00")])


class HistoricalDisplayTests(SimpleTestCase):
    def row(self, **extra):
        base = {"order_id": 9, "order_date": "2026-10-01", "status": "Placed", "total_amount": D("200.00"),
                "full_name": "A", "phone": "1", "address": "a", "city": "c", "state": "s", "postal_code": "1",
                "country": "IN", "payment_method": "online", "shipping_charge": 0, "cod_charge": 0, "item_id": 1,
                "product_id": 15, "variant_id": 0, "product_name": "Configured GST", "variant_name": "", "quantity": 2,
                "price": D("100.00"), "discount": D("0"), "total_price": D("200.00"),
                "snap_gst_amount": D("40.00"), "product_gst_applicable": "N",
                "configured_gst_amount": D("0"), "variant_gst": "N", "image_path": None}
        base.update(extra)
        return base

    def details(self, rows, present=True, customer_pct=0, customer_registered="Y"):
        def one(sql, params=()):
            if "FROM users" in sql:
                return {"email": "alice@example.invalid", "gst": customer_registered, "gst_percentage": customer_pct,
                        "delivery_charges": 0, "vip_discount": None, "vip_percentage": 0}
            return None
        request = post("/api/order_details", {"order_id": 9}, **BEARER)
        with authed(), snapshot_columns(present), mock.patch.object(views_orders, "fetchone", side_effect=one), \
                mock.patch.object(views_orders, "fetchall", return_value=rows):
            return body_of(views_orders.order_details(request))

    def test_snapshot_era_order_ignores_current_product_settings(self):
        pricing = self.details([self.row()])["pricing"]
        self.assertEqual((pricing["gst_amount"], pricing["gst_source"]), (40.0, "snapshot"))

    def test_customer_percentage_change_does_not_change_saved_snapshot(self):
        before = self.details([self.row(snap_gst_amount=D("40.00"))], customer_pct=5)["pricing"]
        after = self.details([self.row(snap_gst_amount=D("40.00"))], customer_pct=28)["pricing"]
        self.assertEqual((before["gst_amount"], before["total_with_gst"]), (after["gst_amount"], after["total_with_gst"]))

    def test_legacy_order_with_missing_snapshot_is_flagged_not_recomputed(self):
        pricing = self.details([self.row(snap_gst_amount=None, product_gst_applicable="Y", configured_gst_amount=D("20"))])["pricing"]
        self.assertEqual((pricing["gst_amount"], pricing["gst_source"]), (0.0, "legacy_unrecorded"))

    def test_pre_snapshot_fallback_uses_saved_registration_and_configured_amount(self):
        row = self.row(snap_gst_amount=None, product_gst_applicable="Y", configured_gst_amount=D("20"))
        registered = self.details([row], present=False, customer_pct=5, customer_registered="Y")["pricing"]
        unregistered = self.details([row], present=False, customer_pct=5, customer_registered="N")["pricing"]
        self.assertEqual((registered["gst_amount"], registered["gst_source"]), (40.0, "current_settings_pre_snapshot"))
        self.assertEqual(unregistered["gst_amount"], 0.0)

    def test_flutter_pricing_keys_remain_present(self):
        pricing = self.details([self.row()])["pricing"]
        for key in ("gst_percentage", "gst_amount", "total_with_gst", "delivery_charge", "grand_total", "subtotal", "customer_billed_percentage", "gst_source"):
            self.assertIn(key, pricing)

    def test_statement_gst_reads_the_snapshot(self):
        with snapshot_columns(True), mock.patch.object(views_finance, "fetchall", return_value=[
                {"product_id": 15, "variant_id": 0, "quantity": 2, "price": D("100"), "snap_gst_amount": D("40.00"),
                 "product_gst_applicable": "N", "configured_amount": 0, "variant_gst": "N"}]):
            total, source = views_finance._order_gst(9, "N", 28)
        self.assertEqual((total, source), (D("40.00"), SOURCE_SNAPSHOT))

# The blank-percentage case is covered by the catalog and central-rule registration regressions.
    def test_statement_pre_snapshot_fallback_uses_registration_flag(self):
        rows = [{"product_id": 16, "variant_id": 201, "quantity": 1, "price": D("999"), "snap_gst_amount": None,
                 "product_gst_applicable": "Y", "configured_amount": D("120"), "variant_gst": "Y"}]
        with snapshot_columns(False), mock.patch.object(views_finance, "fetchall", return_value=rows):
            total, source = views_finance._order_gst(9, "Y", D("5"))
            no_gst, _ = views_finance._order_gst(9, "N", D("5"))
        self.assertEqual((total, source), (D("120.00"), SOURCE_PRE_MIGRATION))
        self.assertEqual(no_gst, D("0.00"))


from legacy_api.cart_discount_testing import module_fixtures
setUpModule, tearDownModule = module_fixtures()
