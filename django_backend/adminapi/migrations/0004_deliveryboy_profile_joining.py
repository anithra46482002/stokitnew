"""Delivery Boy profile: Joining Date and the profile image bytes (PostgreSQL BYTEA) on the metadata table.

Additive only: three new nullable / defaulted columns. Existing Delivery Boy rows are untouched
(joining_date NULL, profile_image NULL, profile_image_mime ''). The already-applied 0003 is not modified.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('adminapi', '0003_deliveryboyadminmetadata'),
    ]

    operations = [
        migrations.AddField(
            model_name='deliveryboyadminmetadata',
            name='joining_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='deliveryboyadminmetadata',
            name='profile_image',
            field=models.BinaryField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='deliveryboyadminmetadata',
            name='profile_image_mime',
            field=models.CharField(blank=True, default='', max_length=30),
        ),
    ]