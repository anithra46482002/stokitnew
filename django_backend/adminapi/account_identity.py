"""SMTP-verified username and email changes for the authenticated Super Admin."""
import hashlib
import hmac
import logging
import re
import secrets
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, OperationalError, ProgrammingError, transaction
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import AdminAccountChangeChallenge
from legacydb.models import AdminAccount
from .profile import ActiveStaffPermission

logger = logging.getLogger("stokit.account_identity")
PURPOSE_USERNAME = "change_username"
PURPOSE_EMAIL_CURRENT = "change_email_current"
PURPOSE_EMAIL_NEW = "change_email_new"
OTP_TTL_SECONDS = 300
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 120
VERIFIED_WINDOW_SECONDS = 600  # how long a verified stage stays usable for Save (code entry itself still expires after OTP_TTL_SECONDS)
OTP_MAX_PER_HOUR = 5


def _otp_hash(purpose, principal_id, target, destination, otp):
    material = "|".join((purpose, str(principal_id), target.casefold(), destination.casefold(), otp)).encode()
    return hmac.new(settings.SECRET_KEY.encode(), material, hashlib.sha256).hexdigest()


def _smtp_send(recipient, otp, purpose):
    host = getattr(settings, "EMAIL_HOST", "")
    port = int(getattr(settings, "EMAIL_PORT", 587))
    username = getattr(settings, "EMAIL_HOST_USER", "")
    password = getattr(settings, "EMAIL_HOST_PASSWORD", "")
    sender = getattr(settings, "DEFAULT_FROM_EMAIL", "")
    use_ssl = bool(getattr(settings, "EMAIL_USE_SSL", False))
    use_tls = bool(getattr(settings, "EMAIL_USE_TLS", True))
    timeout = int(getattr(settings, "EMAIL_TIMEOUT", 15))
    if not host or not sender or not username or not password or (use_ssl and use_tls) or not (use_ssl or use_tls):
        raise ValueError("SMTP is not configured")
    msg = EmailMessage()
    msg["Subject"] = "StokIt account verification"
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(f"Your StokIt account verification code is {otp}.\n\nIt expires in five minutes and can be used once. Do not share this code.\nPurpose: {purpose.replace('_', ' ')}.")
    context = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as smtp:
            smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.ehlo()
            if use_tls:
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(msg)


def _failure_stage(exc):
    """Classify an SMTP failure into a delivery stage for safe logging (never includes secrets or message content)."""
    if isinstance(exc, ValueError):
        return "config"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "auth"
    if isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError)):
        return "send"
    if isinstance(exc, smtplib.SMTPException):
        return "smtp"
    if isinstance(exc, (OSError, TimeoutError)):
        return "connect"
    return "unknown"


def _safe_send(recipient, otp, purpose):
    try:
        _smtp_send(recipient, otp, purpose)
    except Exception as exc:
        logger.error(
            "Account identity OTP delivery failed stage=%s error=%s smtp_code=%s purpose=%s",
            _failure_stage(exc), type(exc).__name__, getattr(exc, "smtp_code", None), purpose,
        )
        raise ValueError("The verification email could not be sent. Check SMTP configuration and try again.") from None


def _issue(request, account, purpose, target, destination):
    now = timezone.now()
    qs = AdminAccountChangeChallenge.objects.filter(principal_type="ADMIN", principal_id=account.pk, purpose=purpose)
    latest = qs.order_by("-created_at").first()
    if latest:
        wait = OTP_RESEND_SECONDS - int((now - latest.created_at).total_seconds())
        if wait > 0:
            return None, Response({"success": False, "code": "resend_cooldown", "message": "Please wait before requesting another code.", "retry_after": wait}, status=429)
    if qs.filter(created_at__gte=now - timedelta(hours=1)).count() >= OTP_MAX_PER_HOUR:
        return None, Response({"success": False, "code": "rate_limited", "message": "Too many verification requests. Try again later."}, status=429)
    otp = f"{secrets.randbelow(1_000_000):06d}"
    qs.filter(invalidated_at__isnull=True, consumed_at__isnull=True).update(invalidated_at=now)
    challenge = AdminAccountChangeChallenge.objects.create(
        principal_type="ADMIN", principal_id=account.pk, purpose=purpose,
        target_value=target, destination=destination,
        otp_hash=_otp_hash(purpose, account.pk, target, destination, otp),
        created_at=now, expires_at=now + timedelta(seconds=OTP_TTL_SECONDS),
    )
    try:
        _safe_send(destination, otp, purpose)
    except ValueError as exc:
        AdminAccountChangeChallenge.objects.filter(pk=challenge.pk).update(invalidated_at=timezone.now())
        return None, Response({"success": False, "code": "email_delivery_failed", "message": str(exc)}, status=503)
    return challenge, None


def _verify(account, purpose, target, destination, otp):
    now = timezone.now()
    invalid = False
    with transaction.atomic():
        challenge = AdminAccountChangeChallenge.objects.select_for_update().filter(
            principal_type="ADMIN", principal_id=account.pk, purpose=purpose,
            target_value=target, destination__iexact=destination,
            invalidated_at__isnull=True, consumed_at__isnull=True, verified_at__isnull=True,
        ).order_by("-created_at").first()
        if challenge is None or challenge.expires_at <= now or challenge.attempts >= OTP_MAX_ATTEMPTS:
            invalid = True
        else:
            expected = _otp_hash(purpose, account.pk, target, challenge.destination, otp)
            if not hmac.compare_digest(challenge.otp_hash, expected):
                challenge.attempts += 1
                if challenge.attempts >= OTP_MAX_ATTEMPTS:
                    challenge.invalidated_at = now
                challenge.save(update_fields=["attempts", "invalidated_at"])
                invalid = True
            else:
                challenge.verified_at = now
                # The five-minute expiry governs entering the code. Once the code is accepted, the verified state gets its own
                # window so a slow second stage cannot silently expire the first (the "Verify both..." failure on Save).
                challenge.expires_at = max(challenge.expires_at, now + timedelta(seconds=VERIFIED_WINDOW_SECONDS))
                challenge.save(update_fields=["verified_at", "expires_at"])
    if invalid:
        raise ValueError("The verification code is invalid or expired. Request a new code.")
    return challenge


def _verified_challenge(account, purpose, target, destination, *, include_expired=False):
    """Latest verified, unconsumed, non-invalidated challenge bound to this admin, purpose, target and destination."""
    qs = AdminAccountChangeChallenge.objects.filter(
        principal_type="ADMIN", principal_id=account.pk, purpose=purpose, target_value=target, destination__iexact=destination,
        verified_at__isnull=False, consumed_at__isnull=True, invalidated_at__isnull=True,
    )
    if not include_expired:
        qs = qs.filter(expires_at__gt=timezone.now())
    return qs.order_by("-created_at").first()


class _SuperAdminView(APIView):
    permission_classes = [IsAuthenticated, ActiveStaffPermission]

    def account(self, request):
        if getattr(request.user, "principal_type", None) != "ADMIN" or getattr(request.user, "role", None) != "SUPER_ADMIN":
            return None, Response({"success": False, "message": "Only the Super Admin can change these account details."}, status=403)
        account = AdminAccount.objects.filter(pk=request.user.pk).first()
        if account is None:
            return None, Response({"success": False, "message": "Admin account not found."}, status=404)
        return account, None

    def post(self, request):
        try:
            return self.handle(request)
        except (OperationalError, ProgrammingError):
            return Response({"success": False, "code": "schema_not_provisioned", "message": "Account-change verification storage is not provisioned. Contact the system administrator."}, status=503)


class UsernameOtpView(_SuperAdminView):
    def handle(self, request):
        account, error = self.account(request)
        if error: return error
        data = request.data if hasattr(request.data, "get") else {}
        username = data.get("username") if isinstance(data.get("username"), str) else ""
        username = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,50}", username):
            return Response({"success": False, "message": "Use 3–50 letters, numbers, dots, underscores, or hyphens."}, status=400)
        if username.casefold() == account.username.casefold():
            return Response({"success": False, "message": "Enter a username different from the current username."}, status=400)
        if AdminAccount.objects.filter(username__iexact=username).exclude(pk=account.pk).exists():
            return Response({"success": False, "message": "That username is already in use."}, status=409)
        _, failure = _issue(request, account, PURPOSE_USERNAME, username, account.email)
        if failure: return failure
        return Response({"success": True, "message": "Verification code sent to your registered email.", "masked_email": _mask(account.email), "expires_in": OTP_TTL_SECONDS, "resend_after": OTP_RESEND_SECONDS})


class UsernameVerifyView(_SuperAdminView):
    def handle(self, request):
        account, error = self.account(request)
        if error: return error
        data = request.data if hasattr(request.data, "get") else {}
        username = data.get("username") if isinstance(data.get("username"), str) else ""
        otp = data.get("otp") if isinstance(data.get("otp"), str) else ""
        username = username.strip()
        if not otp.isdigit() or len(otp) != 6:
            return Response({"success": False, "message": "Enter the 6-digit verification code."}, status=400)
        try:
            with transaction.atomic():
                challenge = _verify(account, PURPOSE_USERNAME, username, account.email, otp)
                if AdminAccount.objects.filter(username__iexact=username).exclude(pk=account.pk).exists():
                    return Response({"success": False, "message": "That username is already in use."}, status=409)
                account.username = username
                account.save(update_fields=["username"])
                AdminAccountChangeChallenge.objects.filter(pk=challenge.pk, consumed_at__isnull=True).update(consumed_at=timezone.now())
        except ValueError as exc:
            return Response({"success": False, "message": str(exc)}, status=400)
        except IntegrityError:
            return Response({"success": False, "message": "That username is already in use."}, status=409)
        return Response({"success": True, "message": "Username updated successfully. Please use the new username next time you sign in.", "username": account.username})


class EmailOtpView(_SuperAdminView):
    purpose = PURPOSE_EMAIL_CURRENT
    destination_kind = "current"

    def handle(self, request):
        account, error = self.account(request)
        if error: return error
        data = request.data if hasattr(request.data, "get") else {}
        email = data.get("email") if isinstance(data.get("email"), str) else ""
        email = email.strip().lower()
        try: validate_email(email)
        except ValidationError: return Response({"success": False, "message": "Enter a valid email address."}, status=400)
        if len(email) > 100: return Response({"success": False, "message": "Email address must be 100 characters or fewer."}, status=400)
        if email.casefold() == account.email.casefold(): return Response({"success": False, "message": "Enter an email address different from the current address."}, status=400)
        if AdminAccount.objects.filter(email__iexact=email).exclude(pk=account.pk).exists(): return Response({"success": False, "message": "That email address is already in use."}, status=409)
        purpose = self.purpose
        if purpose == PURPOSE_EMAIL_NEW and _verified_challenge(account, PURPOSE_EMAIL_CURRENT, email, account.email) is None:
            return Response({"success": False, "code": "current_email_not_verified", "message": "Verify the code sent to your current email address before requesting the new-email code."}, status=409)
        destination = account.email if self.destination_kind == "current" else email
        target = email
        _, failure = _issue(request, account, purpose, target, destination)
        if failure: return failure
        return Response({"success": True, "message": "Verification code sent.", "masked_email": _mask(destination), "expires_in": OTP_TTL_SECONDS, "resend_after": OTP_RESEND_SECONDS})


def _mask(email):
    local, sep, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if sep else ""


class EmailCurrentOtpView(EmailOtpView):
    purpose = PURPOSE_EMAIL_CURRENT
    destination_kind = "current"


class EmailNewOtpView(EmailOtpView):
    purpose = PURPOSE_EMAIL_NEW
    destination_kind = "new"


class EmailVerifyView(_SuperAdminView):
    def handle(self, request):
        account, error = self.account(request)
        if error: return error
        data = request.data if hasattr(request.data, "get") else {}
        email = data.get("email") if isinstance(data.get("email"), str) else ""
        otp = data.get("otp") if isinstance(data.get("otp"), str) else ""
        stage = data.get("stage") if isinstance(data.get("stage"), str) else ""
        email = email.strip().lower()
        purpose = PURPOSE_EMAIL_CURRENT if stage == "current" else PURPOSE_EMAIL_NEW if stage == "new" else ""
        if not purpose or not otp.isdigit() or len(otp) != 6:
            return Response({"success": False, "message": "Choose a verification step and enter the 6-digit code."}, status=400)
        destination = account.email if stage == "current" else email
        try: _verify(account, purpose, email, destination, otp)
        except ValueError as exc: return Response({"success": False, "message": str(exc)}, status=400)
        return Response({"success": True, "message": "Email address verified.", "stage": stage, "verified_expires_in": VERIFIED_WINDOW_SECONDS})


class EmailCompleteView(_SuperAdminView):
    def handle(self, request):
        account, error = self.account(request)
        if error: return error
        data = request.data if hasattr(request.data, "get") else {}
        email = data.get("email") if isinstance(data.get("email"), str) else ""
        email = email.strip().lower()
        now = timezone.now()
        current = _verified_challenge(account, PURPOSE_EMAIL_CURRENT, email, account.email)
        proposed = _verified_challenge(account, PURPOSE_EMAIL_NEW, email, email)
        if not current or not proposed:
            # Distinguish "never verified / different address" from "verified, but the save window ran out" so the UI can say so.
            stale_current = current or _verified_challenge(account, PURPOSE_EMAIL_CURRENT, email, account.email, include_expired=True)
            stale_proposed = proposed or _verified_challenge(account, PURPOSE_EMAIL_NEW, email, email, include_expired=True)
            if stale_current and stale_proposed:
                return Response({"success": False, "code": "verification_expired", "message": "Verification expired. Please start again and request new codes."}, status=400)
            return Response({"success": False, "code": "verification_required", "message": "Verify both the current and new email addresses before saving."}, status=400)
        if AdminAccount.objects.filter(email__iexact=email).exclude(pk=account.pk).exists():
            return Response({"success": False, "message": "That email address is already in use."}, status=409)
        try:
            with transaction.atomic():
                locked = AdminAccountChangeChallenge.objects.select_for_update().filter(pk__in=[current.pk, proposed.pk], consumed_at__isnull=True, invalidated_at__isnull=True, expires_at__gt=now, verified_at__isnull=False)
                if locked.count() != 2: return Response({"success": False, "code": "verification_expired", "message": "Verification expired. Please start again."}, status=400)
                account.email = email
                account.save(update_fields=["email"])
                locked.update(consumed_at=now)
        except IntegrityError:
            return Response({"success": False, "message": "That email address is already in use."}, status=409)
        return Response({"success": True, "message": "Email address updated successfully.", "email": account.email})
