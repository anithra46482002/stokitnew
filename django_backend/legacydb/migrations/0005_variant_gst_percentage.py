"""Add zg_variant.gst_percentage (GST rate per variant, 0-100).

zg_variant is an unmanaged legacy table, so Django's AddField would change only migration
state. The real column is added with explicit SQL, wrapped in SeparateDatabaseAndState so the
model state and the database stay in step. The change is additive and idempotent: existing rows
receive the default 0 (no percentage is inferred from gst_price) and no other column, table or
row value is touched.
"""
from django.db import migrations, models

ADD_COLUMN_SQL = """
ALTER TABLE zg_variant
    ADD COLUMN IF NOT EXISTS gst_percentage numeric(5,2) NOT NULL DEFAULT 0;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_zg_variant_gst_percentage'
          AND conrelid = 'zg_variant'::regclass
    ) THEN
        ALTER TABLE zg_variant
            ADD CONSTRAINT ck_zg_variant_gst_percentage CHECK (gst_percentage >= 0 AND gst_percentage <= 100);
    END IF;
END
$$;
"""

DROP_COLUMN_SQL = """
ALTER TABLE zg_variant DROP CONSTRAINT IF EXISTS ck_zg_variant_gst_percentage;
ALTER TABLE zg_variant DROP COLUMN IF EXISTS gst_percentage;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('legacydb', '0004_variant_minimum_buying_count'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='variant',
                    name='gst_percentage',
                    field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
                ),
            ],
            database_operations=[
                migrations.RunSQL(sql=ADD_COLUMN_SQL, reverse_sql=DROP_COLUMN_SQL),
            ],
        ),
    ]
