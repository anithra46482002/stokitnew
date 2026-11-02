"""Database-free tests for the per-variant GST percentage (zg_variant.gst_percentage) and its server-side GST amount."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from adminapi import crud


def _user():
    return SimpleNamespace(is_authenticated=True, role="ADMIN", pk=1, principal_type="ADMIN")


def _row(**extra):
    row = SimpleNamespace(
        variant_id=3, product_id=10, variant_name="500g", variant_price=Decimal("100"), mrp=Decimal("100"),
        quantity=5, disc="0", gst="Y", gst_price=Decimal("0"), gst_percentage=Decimal("0"),
        minimum_buying_count=1, save=MagicMock())
    for key, value in extra.items():
        setattr(row, key, value)
    return row


def _post(payload):
    body = {"product_id": 10, "name": "25 kg bag", "price": "100", "quantity": 5, **payload}
    request = APIRequestFactory().post("/admin-api/product-variants", body, format="json")
    force_authenticate(request, user=_user())

    def make(**kwargs):
        return SimpleNamespace(variant_id=77, variant_name=kwargs["variant_name"], **{
            key: kwargs[key] for key in ("product_id", "variant_price", "mrp", "quantity", "disc", "gst",
                                         "gst_price", "gst_percentage", "minimum_buying_count")})

    with patch.object(crud.Product.objects, "filter") as products, \
            patch.object(crud.Variant.objects, "create", side_effect=make) as create_variant:
        products.return_value.exists.return_value = True
        response = crud.VariantCRUDView.as_view()(request)
    return response, create_variant


def _patch(row, payload):
    request = APIRequestFactory().patch("/admin-api/product-variants", {"id": 3, **payload}, format="json")
    force_authenticate(request, user=_user())
    with patch.object(crud.Variant.objects, "filter") as variants:
        variants.return_value.first.return_value = row
        return crud.VariantCRUDView.as_view()(request)


class GstAmountFormulaTests(SimpleTestCase):
    def test_documented_examples(self):
        for price, pct, expected in (("100", "5", "5.00"), ("200", "5", "10.00"), ("100", "18", "18.00"),
                                     ("100", "0", "0.00"), ("0", "18", "0.00")):
            with self.subTest(price=price, pct=pct):
                self.assertEqual(crud._variant_gst_amount(Decimal(price), Decimal(pct)), Decimal(expected))

    def test_rounds_half_up_to_two_decimals(self):
        self.assertEqual(crud._variant_gst_amount(Decimal("33.33"), Decimal("5")), Decimal("1.67"))   # 1.6665
        self.assertEqual(crud._variant_gst_amount(Decimal("0.10"), Decimal("5")), Decimal("0.01"))    # 0.005
        self.assertEqual(crud._variant_gst_amount(Decimal("19.99"), Decimal("12.5")), Decimal("2.50"))  # 2.49875


class GstPercentageValidatorTests(SimpleTestCase):
    def test_accepts_the_valid_range_and_keeps_two_decimals(self):
        for value, expected in (("0", "0.00"), (0, "0.00"), ("5", "5.00"), (18, "18.00"), (" 12.345 ", "12.35"), ("100", "100.00")):
            with self.subTest(value=value):
                self.assertEqual(crud._validated_gst_percentage(value), Decimal(expected))

    def test_rejects_empty_negative_over_100_text_boolean_and_non_finite(self):
        for value in (None, "", "  ", "-1", -0.01, "100.01", 101, "abc", "5%", True, "NaN", "Infinity", "-Infinity", "1e400"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                crud._validated_gst_percentage(value)


class VariantCreateGstPercentageTests(SimpleTestCase):
    def test_create_stores_the_percentage_and_the_calculated_amount(self):
        for price, pct, amount in (("100", "5", "5.00"), ("200", "5", "10.00"), ("100", "18", "18.00")):
            with self.subTest(price=price, pct=pct):
                response, create = _post({"price": price, "gst": "Y", "gst_percentage": pct})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(create.call_args.kwargs["gst_percentage"], Decimal(pct).quantize(Decimal("0.01")))
                self.assertEqual(create.call_args.kwargs["gst_price"], Decimal(amount))
                self.assertEqual(response.data["gst_percentage"], str(Decimal(pct).quantize(Decimal("0.01"))))
                self.assertEqual(response.data["gst_price"], amount)

    def test_server_calculation_wins_over_a_contradicting_client_amount(self):
        response, create = _post({"gst": "Y", "gst_percentage": "5", "gst_price": "99"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(create.call_args.kwargs["gst_price"], Decimal("5.00"))

    def test_gst_no_forces_a_zero_amount_even_with_a_percentage_and_client_amount(self):
        response, create = _post({"gst": "N", "gst_percentage": "18", "gst_price": "50"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(create.call_args.kwargs["gst_price"], Decimal("0.00"))
        self.assertEqual(response.data["gst_price"], "0.00")

    def test_omitted_percentage_keeps_the_legacy_amount_and_stores_zero_percentage(self):
        response, create = _post({"gst": "Y", "gst_price": "3.50"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(create.call_args.kwargs["gst_percentage"], Decimal("0.00"))
        self.assertEqual(create.call_args.kwargs["gst_price"], Decimal("3.50"))

    def test_invalid_percentages_are_rejected_before_any_write(self):
        for value in ("-1", "100.01", "abc", "NaN", "Infinity", "", None, True):
            with self.subTest(value=value):
                response, create = _post({"gst": "Y", "gst_percentage": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("gst_percentage", response.data)
                create.assert_not_called()

    def test_several_variants_calculate_independently(self):
        results = [_post({"name": name, "price": price, "gst": gst, "gst_percentage": pct})[0].data["gst_price"]
                   for name, price, gst, pct in (("A", "100", "Y", "5"), ("B", "200", "Y", "18"), ("C", "100", "N", "5"))]
        self.assertEqual(results, ["5.00", "36.00", "0.00"])


class VariantPatchGstPercentageTests(SimpleTestCase):
    def test_patch_updates_percentage_and_recalculates_the_amount(self):
        row = _row()
        response = _patch(row, {"gst_percentage": "18"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((row.gst_percentage, row.gst_price), (Decimal("18.00"), Decimal("18.00")))
        self.assertEqual(response.data["gst_percentage"], "18.00")
        self.assertEqual(response.data["gst_price"], "18.00")
        self.assertEqual(row.save.call_args.kwargs["update_fields"], ["gst_percentage", "gst_price"])

    def test_changing_the_price_recalculates_with_the_stored_percentage(self):
        row = _row(gst_percentage=Decimal("5.00"), gst_price=Decimal("5.00"))
        response = _patch(row, {"price": "200"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(row.gst_price, Decimal("10.00"))
        self.assertEqual(row.save.call_args.kwargs["update_fields"], ["gst_price", "variant_price"])

    def test_turning_gst_off_zeroes_the_amount_and_ignores_a_client_amount(self):
        row = _row(gst_percentage=Decimal("5.00"), gst_price=Decimal("5.00"))
        response = _patch(row, {"gst": "N", "gst_price": "7"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(row.gst_price, Decimal("0.00"))

    def test_client_amount_cannot_contradict_the_calculation(self):
        row = _row(gst_percentage=Decimal("5.00"), gst_price=Decimal("5.00"))
        _patch(row, {"gst_price": "99"})
        self.assertEqual(row.gst_price, Decimal("5.00"))

    def test_legacy_variant_without_a_percentage_keeps_its_amount(self):
        row = _row(gst_percentage=Decimal("0"), gst_price=Decimal("4.00"))
        _patch(row, {"price": "150"})
        self.assertEqual(row.gst_price, Decimal("4.00"))
        self.assertNotIn("gst_price", row.save.call_args.kwargs["update_fields"])
        _patch(row, {"gst_price": "6"})
        self.assertEqual(row.gst_price, Decimal("6"))

    def test_unrelated_edit_does_not_touch_gst_columns(self):
        row = _row(gst_percentage=Decimal("5.00"), gst_price=Decimal("5.00"))
        _patch(row, {"quantity": 9})
        self.assertEqual(row.save.call_args.kwargs["update_fields"], ["quantity"])

    def test_invalid_percentage_is_rejected_and_nothing_is_saved(self):
        for value in ("-5", "101", "abc", "NaN", "Infinity", ""):
            with self.subTest(value=value):
                row = _row()
                response = _patch(row, {"gst_percentage": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("gst_percentage", response.data)
                row.save.assert_not_called()


class VariantReadGstPercentageTests(SimpleTestCase):
    def test_list_and_detail_rows_include_the_percentage(self):
        listed = crud.VariantCRUDView._list_row(_row(gst_percentage=Decimal("12.50")), {10: "Tea"}, {})
        self.assertEqual(listed["gst_percentage"], "12.50")
        self.assertEqual(crud.VariantCRUDView._payload(_row(gst_percentage=Decimal("5.00")))["gst_percentage"], "5.00")


class ProductGstPercentageTests(SimpleTestCase):
    def _post_product(self, extra):
        payload = {'name': 'GST test product', 'code': 'GST-TEST', 'price': '100', 'category_id': 1, **extra}
        request = APIRequestFactory().post('/admin-api/products', payload, format='json')
        force_authenticate(request, user=_user())

        def make(**kwargs):
            return SimpleNamespace(
                product_id=7, product_name=kwargs['product_name'], code=kwargs['code'],
                product_price=kwargs['product_price'], category_id=kwargs['category_id'],
                subcategory_id=kwargs['subcategory_id'], gst_applicable=kwargs['gst_applicable'],
                gst_percentage=kwargs['gst_percentage'], gst_price=kwargs['gst_price'],
                no_discount_banner=kwargs['no_discount_banner'], disable_variants=kwargs['disable_variants'],
                moq=kwargs['moq'], subtitle=kwargs['subtitle'], brand=kwargs['brand'],
            )

        with patch.object(crud.Category.objects, 'filter') as categories,                 patch.object(crud.Product.objects, 'create', side_effect=make) as create_product:
            categories.return_value.exists.return_value = True
            response = crud.ProductCRUDView.as_view()(request)
        return response, create_product

    def test_product_create_persists_and_returns_the_default_percentage(self):
        response, create = self._post_product({'gst_applicable': 'Y', 'gst_percentage': '5'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(create.call_args.kwargs['gst_percentage'], Decimal('5.00'))
        self.assertEqual(response.data['gst_percentage'], '5.00')

    def test_product_create_rejects_invalid_percentages_before_write(self):
        for value in ('-1', '100.01', 'NaN', 'Infinity', 'abc', True):
            with self.subTest(value=value):
                response, create = self._post_product({'gst_percentage': value})
                self.assertEqual(response.status_code, 400)
                self.assertIn('gst_percentage', response.data)
                create.assert_not_called()

    def test_product_patch_updates_and_returns_the_default_percentage(self):
        row = SimpleNamespace(
            product_id=10, product_name='Tea', code='T1', product_price=Decimal('100'),
            mrp=Decimal('100'), product_discount=Decimal('0'), category_id=1, subcategory_id=None,
            gst_applicable='Y', gst_percentage=Decimal('5.00'), gst_price=Decimal('5.00'),
            subtitle='', brand='', set_contains='[]', no_discount_banner='N', disable_variants='N',
            moq=1, view='1', highlight=False, save=MagicMock(),
        )
        request = APIRequestFactory().patch('/admin-api/products', {'id': 10, 'gst_percentage': '18'}, format='json')
        force_authenticate(request, user=_user())
        with patch.object(crud.Product.objects, 'filter') as products:
            products.return_value.first.return_value = row
            response = crud.ProductCRUDView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(row.gst_percentage, Decimal('18.00'))
        self.assertEqual(response.data['gst_percentage'], '18.00')
        self.assertEqual(row.save.call_args.kwargs['update_fields'], ['gst_percentage'])
