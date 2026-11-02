from django.core.validators import MinValueValidator
from django.db import models


class DeliveryBoyAdminMetadata(models.Model):
    """Admin-only status/audit metadata; legacy delivery_boys table remains untouched."""
    delivery_boy_id = models.PositiveIntegerField(unique=True, db_index=True)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Added by migration 0004 (prepared, not applied). Existing rows keep NULL / '' until an admin sets them.
    joining_date = models.DateField(null=True, blank=True)
    # The image bytes themselves (PostgreSQL BYTEA). Never part of a listing response; served by an authenticated endpoint.
    profile_image = models.BinaryField(null=True, blank=True, editable=False)
    # Set together with profile_image; a non-empty value is also the cheap "has an image" flag (no BYTEA read needed).
    profile_image_mime = models.CharField(max_length=30, blank=True, default="")

    class Meta:
        db_table = "admin_delivery_boy_metadata"


class CartDiscountSettings(models.Model):
    """Single-row configuration for the fixed cart discount; migration requires approval."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=False)
    minimum_subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    fixed_discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "admin_cart_discount_settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
