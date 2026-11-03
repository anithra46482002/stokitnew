"""Create zg_product_discount_banners: the product-specific discount-banner table.

Why a NEW table name: production already has a table called zg_discount_banners with the unrelated shape
(id, title, image, link, is_active, created_at). The source schema's product-linked table
(id, product_id, image_path, created_at) cannot be created under that name, and the existing table must
never be altered, dropped or repurposed. The product-specific rows therefore live in their own table.

The change is additive and idempotent (CREATE ... IF NOT EXISTS; the foreign key is added only when it is
missing and zg_product exists). No existing table or row is touched, and no product-to-banner mapping is
invented: the new table starts empty. Old product banners cannot be recovered because production never held them.
Reverse drops only the new table.
"""
from django.db import migrations, models

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS zg_product_discount_banners (
    id serial PRIMARY KEY,
    product_id integer NOT NULL,
    image_path varchar(255) NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_zg_product_discount_banners_product_id
    ON zg_product_discount_banners (product_id);
DO $$
BEGIN
    IF to_regclass('zg_product') IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_product_discount_banners_product'
          AND conrelid = 'zg_product_discount_banners'::regclass
    ) THEN
        ALTER TABLE zg_product_discount_banners
            ADD CONSTRAINT fk_product_discount_banners_product
            FOREIGN KEY (product_id) REFERENCES zg_product (product_id) ON DELETE CASCADE;
    END IF;
END
$$;
"""

DROP_SQL = "DROP TABLE IF EXISTS zg_product_discount_banners;"


class Migration(migrations.Migration):

    dependencies = [
        ('legacydb', '0005_variant_gst_percentage'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='ProductDiscountBanner',
                    fields=[
                        ('id', models.AutoField(primary_key=True, serialize=False)),
                        ('product_id', models.IntegerField()),
                        ('image_path', models.CharField(max_length=255)),
                        ('created_at', models.DateTimeField(blank=True, null=True)),
                    ],
                    options={'db_table': 'zg_product_discount_banners', 'managed': False},
                ),
            ],
            database_operations=[
                migrations.RunSQL(sql=CREATE_SQL, reverse_sql=DROP_SQL),
            ],
        ),
    ]
