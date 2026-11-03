"""Database-free checks for the product discount banner table (Products API HTTP 500 fix).

Production's zg_discount_banners is an unrelated table (id, title, image, link, is_active, created_at), so the
product-specific rows live in zg_product_discount_banners. These tests pin: the model mapping, that migration
0006 is additive/idempotent and never touches the production table, and that no runtime code still queries
the colliding table name.
"""
import importlib
import re
from pathlib import Path

from django.test import SimpleTestCase

from legacydb.models import ProductDiscountBanner

BACKEND = Path(__file__).resolve().parent.parent
MIGRATION = importlib.import_module("legacydb.migrations.0006_product_discount_banner")


class ProductDiscountBannerModelTests(SimpleTestCase):
    def test_model_maps_the_dedicated_unmanaged_table(self):
        self.assertEqual(ProductDiscountBanner._meta.db_table, "zg_product_discount_banners")
        self.assertFalse(ProductDiscountBanner._meta.managed)
        self.assertEqual({f.name for f in ProductDiscountBanner._meta.fields}, {"id", "product_id", "image_path", "created_at"})


class MigrationSafetyTests(SimpleTestCase):
    def test_migration_is_additive_idempotent_and_leaves_the_production_table_alone(self):
        sql = MIGRATION.CREATE_SQL
        self.assertIn("CREATE TABLE IF NOT EXISTS zg_product_discount_banners", sql)
        self.assertIn("CREATE INDEX IF NOT EXISTS", sql)
        self.assertIn("ON DELETE CASCADE", sql)
        self.assertIsNone(re.search(r"\b(DROP|TRUNCATE|UPDATE|INSERT)\b|(?<!ON )\bDELETE\b", sql, re.I))
        self.assertNotRegex(sql, r"zg_discount_banners")
        self.assertEqual(MIGRATION.DROP_SQL, "DROP TABLE IF EXISTS zg_product_discount_banners;")

    def test_migration_depends_on_the_latest_existing_migration(self):
        self.assertEqual(MIGRATION.Migration.dependencies, [("legacydb", "0005_variant_gst_percentage")])


class NoRuntimeUseOfTheCollidingTableTests(SimpleTestCase):
    def test_runtime_code_never_queries_the_unrelated_production_table(self):
        pattern = re.compile(r"(?<!product_)zg_discount_banners")
        for rel in ("adminapi/crud.py", "legacy_api/views_catalog.py"):
            with self.subTest(file=rel):
                self.assertIsNone(pattern.search((BACKEND / rel).read_text(encoding="utf-8")))

    def test_response_keys_are_unchanged(self):
        self.assertIn('"discound_banners": banners', (BACKEND / "legacy_api/views_catalog.py").read_text(encoding="utf-8"))
        self.assertIn("'discount_banners': discount_banners.get(p.product_id, [])", (BACKEND / "adminapi/crud.py").read_text(encoding="utf-8"))
