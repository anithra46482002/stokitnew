# Additive profile-image column for the legacy AdminAccount table. Review only; do not apply without approval.
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("adminapi", "0001_cart_discount_settings"),
        ("accounts", "0003_admin_password_flow"),
    ]

    operations = [
        migrations.RunSQL(
            sql='ALTER TABLE "admins" ADD COLUMN "profile_image" varchar(500) NULL',
            reverse_sql='ALTER TABLE "admins" DROP COLUMN "profile_image"',
        ),
    ]
