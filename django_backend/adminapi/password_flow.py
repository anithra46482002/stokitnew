"""Secure change-password / forgot-password logic for staff principals (admin, salesman, delivery boy).

Design notes
* OTPs and completion tokens are stored only as HMAC-SHA256 digests keyed with SECRET_KEY, and every digest is
  bound to the principal and the purpose, so a forgot-password OTP can never complete a change-password flow.
* Passwords are verified with accounts.hashers.verify_php_password and written with hash_password (Argon2id):
  the legacy admin/salesman/delivery tables are not Django auth users, so set_password()/check_password()
  do not apply. Django's *configured password validators* are still used (validate_password).
* The completion token is random, single-use and short-lived; the client never supplies an "otp_verified" flag.
* Rate limiting is database-backed (AdminAuthRateEvent) so it holds across worker processes.
* Bearer tokens are stateless signed values; password changes revoke older tokens through AdminCredentialState.
"""
import hashlib
import hmac
import logging
import secrets
import time
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from accounts.hashers import hash_password, verify_php_password
from accounts.models import AdminAuthRateEvent, AdminCredentialState, AdminOTPChallenge, AdminRoleAssignment
from legacydb.models import AdminAccount, DeliveryBoy, Salesman

logger = logging.getLogger("stokit.password_flow")

PRINCIPAL_MODELS = {"ADMIN": AdminAccount, "SALESMAN": Salesman, "DELIVERY_BOY": DeliveryBoy}
CHANGE = AdminOTPChallenge.PURPOSE_CHANGE
FORGOT = AdminOTPChallenge.PURPOSE_FORGOT

FORGOT_GENERIC_MESSAGE = "If an eligible account exists for this email, an OTP will be sent."
INVALID_OTP_MESSAGE = "The OTP is invalid or has expired."
INVALID_TOKEN_MESSAGE = "This verification has expired or was already used. Please start again."


class FlowError(Exception):
    """A safe, client-facing failure. Never carries secrets, OTPs or internal details."""

    def __init__(self, code, message, status=400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


# ----------------------------------------------------------------------------------------------- helpers
def mask_email(email):
    """a***@gmail.com - the registered address, never the full value."""
    local, _, domain = (email or "").partition("@")
    if not local or not domain:
        return ""
    return f"{local[0]}***@{domain}"


def normalise_email(value):
    return (value or "").strip().lower()


def email_is_valid(value):
    if not value or len(value) > 254 or "\x00" in value:
        return False
    try:
        validate_email(value)
    except ValidationError:
        return False
    return True


def _hmac(label, *parts):
    material = ":".join([label, *[str(p) for p in parts]]).encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), material, hashlib.sha256).hexdigest()


def otp_digest(purpose, principal_type, principal_id, otp):
    return _hmac("stokit-admin-otp", purpose, principal_type, principal_id, otp)


def token_digest(purpose, token):
    return _hmac("stokit-admin-completion", purpose, token)


def new_otp():
    return f"{secrets.randbelow(1_000_000):06d}"


def client_ip(request):
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def load_account(principal_type, principal_id):
    model = PRINCIPAL_MODELS.get(principal_type)
    if model is None:
        return None
    return model.objects.filter(pk=principal_id).first()


def role_assignment(principal_type, principal_id):
    return AdminRoleAssignment.objects.filter(principal_type=principal_type, principal_id=principal_id).first()


def account_has_active_role(principal_type, principal_id):
    return AdminRoleAssignment.objects.filter(
        principal_type=principal_type, principal_id=principal_id, active=True
    ).exists()


# ----------------------------------------------------------------------------------------------- rate limits
def _rate_key(key):
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def rate_count(scope, key, window_seconds):
    since = timezone.now() - timedelta(seconds=window_seconds)
    return AdminAuthRateEvent.objects.filter(scope=scope, key_hash=_rate_key(key), created_at__gte=since).count()


def rate_record(scope, key):
    now = timezone.now()
    AdminAuthRateEvent.objects.filter(created_at__lt=now - timedelta(days=1)).delete()  # keep the table small
    AdminAuthRateEvent.objects.create(scope=scope, key_hash=_rate_key(key), created_at=now)


def rate_blocked(scope, key, limit, window_seconds):
    return rate_count(scope, key, window_seconds) >= limit


# ----------------------------------------------------------------------------------------------- e-mail
def _email_backend_is_dummy():
    return str(settings.EMAIL_BACKEND).endswith("dummy.EmailBackend")


def ensure_email_configured():
    if _email_backend_is_dummy():
        raise FlowError(
            "email_not_configured",
            "E-mail delivery is not configured on this server, so an OTP cannot be sent. Contact the system administrator.",
            503,
        )


def send_otp_email(recipient, otp, purpose):
    """Send the OTP; raise FlowError when delivery fails. Never logs the OTP, recipient or credentials."""
    ensure_email_configured()
    what = "change your password" if purpose == CHANGE else "reset your password"
    minutes = max(1, settings.ADMIN_OTP_TTL_SECONDS // 60)
    body = (
        f"Your StokIt Admin verification code is {otp}.\n\n"
        f"Use it to {what}. It expires in {minutes} minutes and can be used once.\n"
        "If you did not request this, ignore this e-mail and consider changing your password."
    )
    try:
        sent = send_mail("StokIt Admin verification code", body, settings.DEFAULT_FROM_EMAIL, [recipient], fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - any SMTP/network/config failure must become a safe error
        logger.error("Admin OTP e-mail delivery failed (%s) purpose=%s", type(exc).__name__, purpose)
        raise FlowError("email_delivery_failed", "The OTP e-mail could not be sent. Please try again later.", 503)
    if not sent:
        logger.error("Admin OTP e-mail backend reported 0 messages sent purpose=%s", purpose)
        raise FlowError("email_delivery_failed", "The OTP e-mail could not be sent. Please try again later.", 503)


# ----------------------------------------------------------------------------------------------- challenges
def _active_filter(principal_type, principal_id, purpose):
    return AdminOTPChallenge.objects.filter(
        principal_type=principal_type, principal_id=principal_id, purpose=purpose,
        invalidated_at__isnull=True, consumed_at__isnull=True,
    )


def issue_challenge(principal_type, account, purpose, *, current_password_verified_at=None, enforce_limits=True):
    """Create a new OTP challenge (superseding older ones) and e-mail it to the account's registered address.

    Raises FlowError for cooldown / hourly limit / delivery problems. Returns the challenge.
    """
    ensure_email_configured()
    recipient = (account.email or "").strip()
    if not email_is_valid(recipient):
        raise FlowError("no_registered_email", "This account has no valid registered e-mail address.", 400)
    now = timezone.now()
    base = AdminOTPChallenge.objects.filter(principal_type=principal_type, principal_id=account.pk, purpose=purpose)
    if enforce_limits:
        latest = base.order_by("-created_at").first()
        if latest is not None:
            wait = settings.ADMIN_OTP_RESEND_COOLDOWN_SECONDS - (now - latest.created_at).total_seconds()
            if wait > 0:
                raise FlowError("resend_cooldown", "Please wait before requesting another OTP.", 429, retry_after=int(wait) + 1)
        if base.filter(created_at__gte=now - timedelta(hours=1)).count() >= settings.ADMIN_OTP_MAX_SENDS_PER_HOUR:
            raise FlowError("too_many_otp_requests", "Too many OTP requests. Please try again later.", 429, retry_after=3600)
    otp = new_otp()
    with transaction.atomic():
        base.filter(invalidated_at__isnull=True, consumed_at__isnull=True).update(invalidated_at=now)
        challenge = AdminOTPChallenge.objects.create(
            principal_type=principal_type, principal_id=account.pk, purpose=purpose,
            otp_hash=otp_digest(purpose, principal_type, account.pk, otp),
            created_at=now, expires_at=now + timedelta(seconds=settings.ADMIN_OTP_TTL_SECONDS),
            current_password_verified_at=current_password_verified_at,
        )
    try:
        send_otp_email(recipient, otp, purpose)
    except FlowError:
        AdminOTPChallenge.objects.filter(pk=challenge.pk).update(invalidated_at=timezone.now())
        raise
    return challenge


def verify_otp(principal_type, principal_id, purpose, otp):
    """Check an OTP against the principal's active challenge. Returns (completion_token, ttl_seconds).

    Every failure raises the same FlowError so callers/clients cannot learn why it failed.
    """
    otp = (otp or "").strip()
    if len(otp) != 6 or not otp.isdigit():
        raise FlowError("invalid_otp_format", "Enter the 6-digit OTP from your e-mail.", 400)
    now = timezone.now()
    token = None
    # The wrong-guess counter must be COMMITTED, so the failure is raised only after the atomic block has exited.
    with transaction.atomic():
        challenge = (
            _active_filter(principal_type, principal_id, purpose).filter(verified_at__isnull=True)
            .select_for_update().order_by("-created_at").first()
        )
        if challenge is None or challenge.expires_at <= now or challenge.attempts >= settings.ADMIN_OTP_MAX_ATTEMPTS:
            pass
        elif not hmac.compare_digest(challenge.otp_hash, otp_digest(purpose, principal_type, principal_id, otp)):
            challenge.attempts += 1
            fields = ["attempts"]
            if challenge.attempts >= settings.ADMIN_OTP_MAX_ATTEMPTS:
                challenge.invalidated_at = now
                fields.append("invalidated_at")
            challenge.save(update_fields=fields)
        else:
            token = secrets.token_urlsafe(32)
            challenge.verified_at = now
            challenge.token_hash = token_digest(purpose, token)
            challenge.token_expires_at = now + timedelta(seconds=settings.ADMIN_PASSWORD_TOKEN_TTL_SECONDS)
            challenge.save(update_fields=["verified_at", "token_hash", "token_expires_at"])
    if token is None:
        raise FlowError("invalid_or_expired_otp", INVALID_OTP_MESSAGE, 400)
    return token, settings.ADMIN_PASSWORD_TOKEN_TTL_SECONDS


def _find_token_challenge(purpose, token, principal=None):
    token = (token or "").strip() if isinstance(token, str) else ""
    if not token or len(token) > 200:
        raise FlowError("invalid_or_expired_token", INVALID_TOKEN_MESSAGE, 400)
    qs = AdminOTPChallenge.objects.filter(
        purpose=purpose, token_hash=token_digest(purpose, token),
        verified_at__isnull=False, invalidated_at__isnull=True, consumed_at__isnull=True,
    )
    if principal is not None:  # authenticated change flow: the token must belong to the caller
        qs = qs.filter(principal_type=principal[0], principal_id=principal[1])
    return qs


def validate_new_password(account, new_password, confirm_password):
    errors = {}
    if not isinstance(new_password, str) or not new_password:
        errors["new_password"] = ["This field is required."]
    if not isinstance(confirm_password, str) or not confirm_password:
        errors["confirm_password"] = ["This field is required."]
    if errors:
        raise FlowError("validation_error", "Enter and confirm the new password.", 400, errors=errors)
    if new_password != confirm_password:
        raise FlowError("password_mismatch", "The two passwords do not match.", 400,
                        errors={"confirm_password": ["The two passwords do not match."]})
    try:
        validate_password(new_password, user=account)
    except ValidationError as exc:
        raise FlowError("weak_password", "The new password does not meet the password rules.", 400,
                        errors={"new_password": list(exc.messages)})
    if verify_php_password(new_password, account.password):
        raise FlowError("password_unchanged", "The new password must be different from the current password.", 400,
                        errors={"new_password": ["The new password must be different from the current password."]})


def revoke_tokens(principal_type, principal_id):
    """Invalidate every admin bearer token issued up to now for this principal."""
    AdminCredentialState.objects.update_or_create(
        principal_type=principal_type, principal_id=principal_id,
        defaults={"tokens_valid_after": int(time.time()), "updated_at": timezone.now()},
    )


def issue_admin_token(principal_type, principal_id):
    if principal_type == "ADMIN":
        payload = {"admin_id": principal_id}
    else:
        payload = {"principal_type": principal_type, "principal_id": principal_id}
    return signing.dumps(payload, salt="stokit.admin-token")


def complete_password_change(purpose, token, new_password, confirm_password, *, principal=None):
    """Validate the new password, then atomically consume the token, set the hash and revoke old tokens.

    Returns (principal_type, principal_id). The token is only consumed when the password is accepted, so a
    weak/mismatched password can be corrected without repeating the OTP step.
    """
    challenge = _find_token_challenge(purpose, token, principal).first()
    now = timezone.now()
    if challenge is None or challenge.token_expires_at is None or challenge.token_expires_at <= now:
        raise FlowError("invalid_or_expired_token", INVALID_TOKEN_MESSAGE, 400)
    account = load_account(challenge.principal_type, challenge.principal_id)
    if account is None or not account_has_active_role(challenge.principal_type, challenge.principal_id):
        raise FlowError("invalid_or_expired_token", INVALID_TOKEN_MESSAGE, 400)
    validate_new_password(account, new_password, confirm_password)
    new_hash = hash_password(new_password)
    with transaction.atomic():
        locked = _find_token_challenge(purpose, token, principal).select_for_update().first()
        if locked is None or locked.token_expires_at is None or locked.token_expires_at <= timezone.now():
            raise FlowError("invalid_or_expired_token", INVALID_TOKEN_MESSAGE, 400)
        stamp = timezone.now()
        PRINCIPAL_MODELS[locked.principal_type].objects.filter(pk=locked.principal_id).update(password=new_hash)
        # consume this challenge and kill every other outstanding challenge of the principal
        AdminOTPChallenge.objects.filter(
            principal_type=locked.principal_type, principal_id=locked.principal_id,
            invalidated_at__isnull=True, consumed_at__isnull=True,
        ).exclude(pk=locked.pk).update(invalidated_at=stamp)
        AdminOTPChallenge.objects.filter(pk=locked.pk).update(consumed_at=stamp)
        revoke_tokens(locked.principal_type, locked.principal_id)
    logger.info("Admin password changed purpose=%s principal=%s:%s", purpose, locked.principal_type, locked.principal_id)
    return locked.principal_type, locked.principal_id


# ----------------------------------------------------------------------------------------------- change flow
def check_flow_ip_limit(request):
    ip = client_ip(request)
    if rate_blocked("pw_flow_ip", ip, settings.ADMIN_PASSWORD_FLOW_IP_LIMIT, 3600):
        raise FlowError("too_many_requests", "Too many requests. Please try again later.", 429, retry_after=3600)
    rate_record("pw_flow_ip", ip)


def change_verify_current(request, principal, current_password):
    """Step 1: verify the current password; on success e-mail an OTP to the registered address."""
    check_flow_ip_limit(request)
    if not isinstance(current_password, str) or not current_password:
        raise FlowError("validation_error", "Enter your current password.", 400,
                        errors={"current_password": ["This field is required."]})
    key = f"{principal.principal_type}:{principal.pk}"
    window = 900
    if rate_blocked("pw_current_fail", key, settings.ADMIN_CURRENT_PASSWORD_MAX_FAILURES, window):
        raise FlowError("too_many_failed_attempts", "Too many incorrect attempts. Please try again in 15 minutes.", 429,
                        retry_after=window)
    account = load_account(principal.principal_type, principal.pk)
    if account is None or not account_has_active_role(principal.principal_type, principal.pk):
        raise FlowError("account_inactive", "This account is not active.", 403)
    if not verify_php_password(current_password, account.password):
        rate_record("pw_current_fail", key)
        raise FlowError("current_password_incorrect", "The current password is incorrect.", 400,
                        errors={"current_password": ["The current password is incorrect."]})
    challenge = issue_challenge(principal.principal_type, account, CHANGE, current_password_verified_at=timezone.now())
    return account, challenge


def change_resend(request, principal):
    """Resend the change-password OTP; only after the current password was verified recently."""
    check_flow_ip_limit(request)
    now = timezone.now()
    previous = (
        AdminOTPChallenge.objects.filter(
            principal_type=principal.principal_type, principal_id=principal.pk, purpose=CHANGE,
            verified_at__isnull=True, consumed_at__isnull=True,
            current_password_verified_at__gte=now - timedelta(seconds=settings.ADMIN_CHANGE_VERIFY_WINDOW_SECONDS),
            attempts__lt=settings.ADMIN_OTP_MAX_ATTEMPTS,
        ).order_by("-created_at").first()
    )
    if previous is None:
        raise FlowError("current_password_required", "Verify your current password first.", 400)
    account = load_account(principal.principal_type, principal.pk)
    if account is None or not account_has_active_role(principal.principal_type, principal.pk):
        raise FlowError("account_inactive", "This account is not active.", 403)
    # Keep the ORIGINAL verification time so resends cannot extend the window indefinitely.
    challenge = issue_challenge(principal.principal_type, account, CHANGE,
                                current_password_verified_at=previous.current_password_verified_at)
    return account, challenge


# ----------------------------------------------------------------------------------------------- forgot flow
def find_eligible_account(email):
    """The single active staff account registered with this e-mail, as (principal_type, account) or None.

    Ambiguous matches (several accounts share the address) are treated as ineligible so an OTP can never
    unlock the wrong account.
    """
    email = normalise_email(email)
    if not email_is_valid(email):
        return None
    matches = []
    for principal_type, model in PRINCIPAL_MODELS.items():
        for account in model.objects.filter(email__iexact=email)[:2]:
            matches.append((principal_type, account))
    if len(matches) != 1:
        return None
    principal_type, account = matches[0]
    if not account_has_active_role(principal_type, account.pk):
        return None
    return principal_type, account


def forgot_send_otp(request, email):
    """Always behaves the same for unknown / ineligible / throttled emails (no enumeration)."""
    check_flow_ip_limit(request)
    email = normalise_email(email)
    if not email_is_valid(email):
        raise FlowError("invalid_email", "Enter a valid email address.", 400, errors={"email": ["Enter a valid email address."]})
    ensure_email_configured()  # global server state, identical for every address
    if rate_blocked("pw_forgot_email", email, settings.ADMIN_OTP_MAX_SENDS_PER_HOUR * 2, 3600):
        return False
    rate_record("pw_forgot_email", email)
    found = find_eligible_account(email)
    if found is None:
        return False
    principal_type, account = found
    try:
        issue_challenge(principal_type, account, FORGOT)
    except FlowError as exc:
        # Cooldown, hourly limit or SMTP failure: log only, and answer generically so eligibility is not revealed.
        logger.warning("Forgot-password OTP not sent code=%s", exc.code)
        return False
    return True


def forgot_verify_otp(request, email, otp):
    check_flow_ip_limit(request)
    email = normalise_email(email)
    found = find_eligible_account(email)
    if found is None:
        # Same error and same status as a wrong OTP; count the attempt so guessing emails is also throttled.
        if rate_blocked("pw_forgot_verify_email", email or "-", settings.ADMIN_OTP_MAX_ATTEMPTS * 2, 3600):
            raise FlowError("too_many_requests", "Too many requests. Please try again later.", 429, retry_after=3600)
        rate_record("pw_forgot_verify_email", email or "-")
        raise FlowError("invalid_or_expired_otp", INVALID_OTP_MESSAGE, 400)
    principal_type, account = found
    return verify_otp(principal_type, account.pk, FORGOT, otp)
