from datetime import timedelta
import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.conf import settings


class SessionQuerySet(models.QuerySet):
    def active(self, now=None):
        """Sessions that may authenticate: not revoked, not past expires_at, not inactive too long."""
        now = now or timezone.now()
        q = Q(revoked_at__isnull=True) & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        days = settings.SESSION_INACTIVITY_DAYS
        if days and days > 0:
            q &= Q(last_activity_at__gt=now - timedelta(days=days))
        return self.filter(q)


class UserDeviceSession(models.Model):
    """One row per active login of a Flutter customer account (max MAX_ACTIVE_SESSIONS).

    The raw bearer token is never stored: only its SHA-256 hex digest. The legacy
    `users.token` column is no longer consulted for authentication.
    NOTE: "device" is a label only. The current Flutter client sends no verified device id,
    so the server limits concurrent *sessions*, not physical devices.
    """

    user = models.ForeignKey(
        "legacydb.LegacyUser",
        on_delete=models.CASCADE,
        related_name="device_sessions",
        db_constraint=False,  # never add an FK constraint to the legacy `users` table
    )
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_activity_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=32, blank=True, default="")
    user_agent = models.CharField(max_length=255, blank=True, default="")  # informational only
    ip_address = models.GenericIPAddressField(null=True, blank=True)       # informational only

    objects = SessionQuerySet.as_manager()

    class Meta:
        db_table = "accounts_userdevicesession"
        indexes = [models.Index(fields=["user", "revoked_at"], name="uds_user_revoked_idx")]

    def __str__(self):
        return f"session {self.pk} user={self.user_id}"


class DeviceToken(models.Model):
    """Push (FCM) device tokens sent to /api/notify. Replaces the PHP tokens.txt file."""

    user = models.ForeignKey("legacydb.LegacyUser", on_delete=models.CASCADE, db_constraint=False)
    device_token = models.TextField()
    device_token_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "accounts_devicetoken"
        constraints = [models.UniqueConstraint(fields=["user", "device_token_hash"], name="uniq_user_devicetoken")]


class PasswordResetOTP(models.Model):
    """Secure replacement for the PHP reset flow (which used a fixed OTP)."""

    user = models.ForeignKey("legacydb.LegacyUser", on_delete=models.CASCADE, db_constraint=False)
    otp_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_passwordresetotp"

class CheckoutPayload(models.Model):
    """Persist the legacy saveCheck JSON payload without writing arbitrary files to disk."""
    user_id = models.IntegerField(db_index=True)
    payload = models.JSONField()
    legacy_order_id = models.PositiveBigIntegerField(unique=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "accounts_checkoutpayload"
        ordering = ["-created_at"]
class AdminRoleAssignment(models.Model):
    PRINCIPAL_TYPES = [
        ('ADMIN', 'Legacy admin account'),
        ('SALESMAN', 'Legacy salesman account'),
        ('DELIVERY_BOY', 'Legacy delivery account'),
    ]
    ROLE_CHOICES = [
        ('SUPER_ADMIN', 'Super Admin'),
        ('ADMIN', 'Admin'),
        ('SALESMAN', 'Salesman'),
        ('WAREHOUSE_STAFF', 'Warehouse Staff'),
        ('DELIVERY_BOY', 'Delivery Boy'),
    ]
    principal_type = models.CharField(max_length=20, choices=PRINCIPAL_TYPES)
    principal_id = models.IntegerField(db_index=True)
    role = models.CharField(max_length=24, choices=ROLE_CHOICES)
    active = models.BooleanField(default=True)
    assigned_by_admin_id = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'accounts_adminroleassignment'
        constraints = [models.UniqueConstraint(fields=['principal_type', 'principal_id'], name='uniq_admin_role_principal')]

class AdminOTPChallenge(models.Model):
    """One e-mailed OTP challenge for a staff principal (admin / salesman / delivery boy).

    The OTP and the completion token are stored only as HMAC digests. A challenge is bound to the
    principal AND to its purpose, so a forgot-password OTP can never complete a change-password flow.
    State: active (no *_at flag) -> otp verified (verified_at, token issued) -> consumed (password changed).
    invalidated_at marks a superseded / locked-out challenge; consumed_at marks a successful completion.
    """
    PURPOSE_CHANGE = "change_password"
    PURPOSE_FORGOT = "forgot_password"
    PURPOSES = [(PURPOSE_CHANGE, "Change password"), (PURPOSE_FORGOT, "Forgot password")]

    principal_type = models.CharField(max_length=20)
    principal_id = models.IntegerField()
    purpose = models.CharField(max_length=24, choices=PURPOSES)
    otp_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    current_password_verified_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    token_hash = models.CharField(max_length=64, blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_adminotpchallenge"
        indexes = [
            models.Index(fields=["principal_type", "principal_id", "purpose", "created_at"], name="adm_otp_principal_idx"),
            models.Index(fields=["token_hash"], name="adm_otp_token_idx"),
        ]


class AdminCredentialState(models.Model):
    """Per-principal revocation marker for the stateless admin bearer tokens.

    Tokens carry their issue time; AdminTokenAuthentication rejects any token issued before
    tokens_valid_after (epoch seconds). Set when the password changes or is reset.
    """
    principal_type = models.CharField(max_length=20)
    principal_id = models.IntegerField()
    tokens_valid_after = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "accounts_admincredentialstate"
        constraints = [models.UniqueConstraint(fields=["principal_type", "principal_id"], name="uniq_admin_credential_principal")]


class AdminAuthRateEvent(models.Model):
    """Database-backed event log used for rate limiting (works across worker processes).

    key_hash is a SHA-256 digest of the account/e-mail/IP key, so no e-mail address or IP is stored in clear.
    """
    scope = models.CharField(max_length=40)
    key_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "accounts_adminauthrateevent"
        indexes = [models.Index(fields=["scope", "key_hash", "created_at"], name="adm_rate_lookup_idx")]

class AdminAccountChangeChallenge(models.Model):
    """Dedicated, purpose- and destination-bound OTP challenge for username/email changes."""
    principal_type = models.CharField(max_length=20)
    principal_id = models.IntegerField(db_index=True)
    purpose = models.CharField(max_length=32)
    target_value = models.CharField(max_length=254, blank=True, default="")
    destination = models.CharField(max_length=254)
    otp_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_adminaccountchangechallenge"
        indexes = [models.Index(fields=["principal_type", "principal_id", "purpose", "created_at"], name="adm_acctchg_lookup_idx")]


class DeliveryBoyAuthSession(models.Model):
    """One independently revocable login session for an existing legacy Delivery Boy.

    delivery_boy_id intentionally has no FK constraint: the identity lives in the
    unmanaged legacydb.delivery_boys table and this session table must not alter it.
    Only a SHA-256 digest of the current refresh token is stored.
    """
    delivery_boy_id = models.PositiveBigIntegerField(db_index=True)
    session_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    refresh_token_hash = models.CharField(max_length=64, unique=True)
    device_name = models.CharField(max_length=120, blank=True, default="")
    device_platform = models.CharField(max_length=40, blank=True, default="")
    device_identifier = models.CharField(max_length=128, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(default=timezone.now)
    access_expires_at = models.DateTimeField()
    refresh_expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "accounts_deliveryboyauthsession"
        indexes = [
            models.Index(fields=["delivery_boy_id", "is_active", "created_at"], name="dbas_owner_active_idx"),
            models.Index(fields=["delivery_boy_id", "revoked_at"], name="dbas_owner_revoked_idx"),
        ]
