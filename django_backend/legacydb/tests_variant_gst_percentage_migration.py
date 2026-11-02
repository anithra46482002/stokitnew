"""Database-free checks for migration 0005 (zg_variant.gst_percentage) and the Variant model mapping."""
import importlib
from decimal import Decimal

from django.db import migrations
from django.test import SimpleTestCase

from legacydb.models import Variant

migration_module = importlib.import_module('legacydb.migrations.0005_variant_gst_percentage')


class VariantGstPercentageMigrationTests(SimpleTestCase):
    def test_migration_is_narrow_and_follows_0004(self):
        migration = migration_module.Migration
        self.assertEqual(migration.dependencies, [('legacydb', '0004_variant_minimum_buying_count')])
        self.assertEqual(len(migration.operations), 1)
        self.assertIsInstance(migration.operations[0], migrations.SeparateDatabaseAndState)

    def test_forward_sql_is_additive_and_defaults_existing_rows_to_zero(self):
        sql = migration_module.ADD_COLUMN_SQL
        self.assertIn('ALTER TABLE zg_variant', sql)
        self.assertIn('ADD COLUMN IF NOT EXISTS gst_percentage numeric(5,2) NOT NULL DEFAULT 0', sql)
        self.assertIn('CHECK (gst_percentage >= 0 AND gst_percentage <= 100)', sql)
        # No row is rewritten, so nothing is inferred from gst_price and no other table or column is touched.
        for forbidden in ('DROP', 'DELETE', 'TRUNCATE', 'UPDATE', 'RENAME', 'INSERT', 'zg_product', 'gst_price', 'quantity'):
            self.assertNotIn(forbidden, sql)

    def test_reverse_sql_removes_only_the_new_constraint_and_column(self):
        sql = migration_module.DROP_COLUMN_SQL
        self.assertIn('DROP CONSTRAINT IF EXISTS ck_zg_variant_gst_percentage', sql)
        self.assertIn('DROP COLUMN IF EXISTS gst_percentage', sql)
        self.assertNotIn('DROP TABLE', sql)

    def test_model_maps_the_column_and_stays_unmanaged(self):
        field = Variant._meta.get_field('gst_percentage')
        self.assertEqual(field.get_internal_type(), 'DecimalField')
        self.assertEqual((field.max_digits, field.decimal_places), (5, 2))
        self.assertEqual(field.default, 0)
        self.assertFalse(field.null)
        self.assertFalse(Variant._meta.managed)
        self.assertEqual(Variant._meta.db_table, 'zg_variant')

    def test_variant_gst_flag_and_amount_fields_are_unchanged(self):
        self.assertEqual(Variant._meta.get_field('gst').max_length, 1)
        amount = Variant._meta.get_field('gst_price')
        self.assertEqual((amount.max_digits, amount.decimal_places), (10, 2))
        self.assertEqual(Decimal(Variant._meta.get_field('gst_percentage').default), Decimal(0))
