"""Dedicated Delivery Boy authentication backed by legacy accounts and shared OTP challenges."""
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import timedelta
from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.db import OperationalError, ProgrammingError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.urls import reverse
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed, ParseError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from accounts.hashers import hash_password, verify_php_password
from accounts.models import AdminOTPChallenge, DeliveryBoyAuthSession
from adminapi.models import DeliveryBoyAdminMetadata
from adminapi import password_flow as shared_flow
from adminapi.db_errors import log_db_error
from legacydb.models import DeliveryBoy

logger = logging.getLogger("stokit.delivery_auth")
PRINCIPAL_TYPE = "DELIVERY_BOY"
TOKEN_SALT = "stokit.delivery-token"
# Purpose namespaces are deliberately distinct from Admin password OTP purposes.
FORGOT = "delivery_forgot"
CHANGE = "delivery_change"
GENERIC_FORGOT = "If the account is eligible, an OTP will be sent to the registered email."
INVALID_OTP = "The OTP is invalid or has expired."
INVALID_TOKEN = "This verification has expired or was already used. Please start again."


class DeliveryAuthError(Exception):
    def __init__(self, code, message, status=400, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


class DeliveryBoyPrincipal:
    is_authenticated = True
    is_anonymous = False

    def __init__(self, account, session_id):
        self.id = self.pk = account.pk
        self.username = account.username
        self.email = account.email
        self.fullname = getattr(account, "fullname", "") or ""
        self.phone = getattr(account, "phone", "") or ""
        self.address = getattr(account, "address", "") or ""
        self.principal_type = PRINCIPAL_TYPE
        self.role = PRINCIPAL_TYPE
        self.session_id = uuid.UUID(str(session_id))


def active_delivery_boy(delivery_boy_id):
    """Require an existing legacy account and an Admin-created, active metadata record."""
    account = DeliveryBoy.objects.filter(pk=delivery_boy_id).first()
    if account is None:
        return None
    metadata = DeliveryBoyAdminMetadata.objects.filter(delivery_boy_id=account.pk).defer("profile_image").first()
    if metadata is None or not metadata.is_active:
        return None
    return account, metadata


def _token_hash(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_token(account, session_id, token_type, expires_at):
    now = timezone.now()
    payload = {"principal_type": PRINCIPAL_TYPE, "delivery_boy_id": account.pk,
        "role": PRINCIPAL_TYPE, "token_type": token_type, "jti": str(session_id),
        "nonce": secrets.token_urlsafe(12), "iat": int(now.timestamp()), "exp": int(expires_at.timestamp())}
    return signing.dumps(payload, salt=TOKEN_SALT)


def _new_session_tokens(account, session_id, access_expires_at, refresh_expires_at):
    access = _build_token(account, session_id, "access", access_expires_at)
    refresh = _build_token(account, session_id, "refresh", refresh_expires_at)
    return access, refresh


def _create_session(account, request, data):
    now = timezone.now()
    session_id = uuid.uuid4()
    access_expires_at = now + timedelta(seconds=settings.DELIVERY_ACCESS_TOKEN_TTL_SECONDS)
    refresh_expires_at = now + timedelta(seconds=settings.DELIVERY_REFRESH_TOKEN_TTL_SECONDS)
    access, refresh = _new_session_tokens(account, session_id, access_expires_at, refresh_expires_at)
    device_name = _text(data, "device_name")[:120]
    device_platform = _text(data, "device_platform")[:40]
    device_identifier = _text(data, "device_identifier")[:128]
    ip = request.META.get("REMOTE_ADDR") or None
    try:
        from django.core.validators import validate_ipv46_address
        if ip: validate_ipv46_address(ip)
    except Exception:
        ip = None
    DeliveryBoyAuthSession.objects.create(delivery_boy_id=account.pk, session_id=session_id,
        refresh_token_hash=_token_hash(refresh), device_name=device_name, device_platform=device_platform,
        device_identifier=device_identifier, ip_address=ip, user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
        created_at=now, last_used_at=now, access_expires_at=access_expires_at,
        refresh_expires_at=refresh_expires_at, is_active=True)
    return access, refresh, session_id


def _validate_token(raw, expected_type, max_age, *, lock_session=False):
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        raise AuthenticationFailed("Invalid or expired Delivery Boy token.")
    try:
        payload = signing.loads(raw, salt=TOKEN_SALT, max_age=max_age)
    except (signing.BadSignature, signing.SignatureExpired, ValueError, TypeError):
        raise AuthenticationFailed("Invalid or expired Delivery Boy token.")
    if (not isinstance(payload, dict) or payload.get("principal_type") != PRINCIPAL_TYPE
            or payload.get("role") != PRINCIPAL_TYPE or payload.get("token_type") != expected_type):
        raise AuthenticationFailed("Invalid or expired Delivery Boy token.")
    try:
        delivery_boy_id = int(payload.get("delivery_boy_id"))
        session_id = uuid.UUID(str(payload.get("jti")))
        issued_at = int(payload.get("iat"))
        expires_at = int(payload.get("exp"))
    except (ValueError, TypeError, AttributeError):
        raise AuthenticationFailed("Invalid or expired Delivery Boy token.")
    now = timezone.now()
    if expires_at <= int(now.timestamp()) or issued_at > int(now.timestamp()) + 60:
        raise AuthenticationFailed("Invalid or expired Delivery Boy token.")
    sessions = DeliveryBoyAuthSession.objects
    if lock_session:
        sessions = sessions.select_for_update()
    session = sessions.filter(session_id=session_id, delivery_boy_id=delivery_boy_id,
        is_active=True, revoked_at__isnull=True).first()
    if session is None:
        raise AuthenticationFailed("This Delivery Boy session has been revoked or is unavailable.")
    expected_expiry = session.access_expires_at if expected_type == "access" else session.refresh_expires_at
    if expected_expiry <= now or expires_at > int(expected_expiry.timestamp()):
        raise AuthenticationFailed("This Delivery Boy session has expired.")
    if expected_type == "refresh" and not hmac.compare_digest(session.refresh_token_hash, _token_hash(raw)):
        raise AuthenticationFailed("This refresh token has been rotated or revoked.")
    eligible = active_delivery_boy(delivery_boy_id)
    if eligible is None or eligible[0].pk != delivery_boy_id:
        raise AuthenticationFailed("This Delivery Boy account is inactive or unavailable.")
    return eligible[0], session, payload


class DeliveryBoyTokenAuthentication(BaseAuthentication):
    keyword = b"bearer"

    def authenticate_header(self, request):
        return "Bearer"

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts:
            return None
        if parts[0].lower() != self.keyword or len(parts) != 2:
            raise AuthenticationFailed("Invalid authorization header.")
        try:
            raw = parts[1].decode("ascii")
        except UnicodeDecodeError:
            raise AuthenticationFailed("Invalid authorization header.")
        account, session, _payload = _validate_token(raw, "access", settings.DELIVERY_ACCESS_TOKEN_TTL_SECONDS)
        DeliveryBoyAuthSession.objects.filter(pk=session.pk, is_active=True, revoked_at__isnull=True).update(last_used_at=timezone.now())
        return DeliveryBoyPrincipal(account, session.session_id), raw


def _body(request):
    data = request.data
    if not hasattr(data, "get"):
        raise ParseError("Request body must be a JSON object.")
    return data


def _text(data, key):
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _error(exc):
    body = {"success": False, "code": exc.code, "message": exc.message}
    if "errors" in exc.extra:
        body["errors"] = exc.extra["errors"]
    response = Response(body, status=exc.status)
    if exc.extra.get("retry_after"):
        body["retry_after"] = exc.extra["retry_after"]
        response["Retry-After"] = str(exc.extra["retry_after"])
    return response


def _run(handler):
    try:
        return handler()
    except DeliveryAuthError as exc:
        return _error(exc)
    except shared_flow.FlowError as exc:
        return _error(exc)
    except (OperationalError, ProgrammingError) as exc:
        category, state = log_db_error("Delivery Boy authentication", exc)
        logger.error("Delivery Boy authentication database failure category=%s sqlstate=%s", category, state or "-")
        responses = {
            "table_missing": ("schema_not_provisioned", "The Delivery Boy authentication session table has not been provisioned. An authorized database migration is required."),
            "permission_denied": ("database_permission_denied", "The authentication database role does not have the required table permissions."),
            "database_missing": ("database_missing", "The configured PostgreSQL database does not exist."),
            "authentication_failed": ("database_authentication_failed", "PostgreSQL rejected the configured database credentials."),
            "database_unavailable": ("database_unavailable", "The authentication database is unavailable. Check the PostgreSQL service, host and port."),
        }
        code, message = responses.get(category, ("database_error", "The authentication database request failed. Check the sanitized server log for the database error category."))
        return Response({"success": False, "code": code, "message": message}, status=503)


def _check_ip(request):
    shared_flow.check_flow_ip_limit(request)


def _issue_otp(account, purpose):
    shared_flow.ensure_email_configured()
    recipient = (account.email or "").strip()
    if not shared_flow.email_is_valid(recipient):
        raise DeliveryAuthError("no_registered_email", "This account has no valid registered email address.")
    now = timezone.now()
    with transaction.atomic():
        # Serialize issuance per account to prevent parallel requests bypassing cooldown/limits.
        locked_account = DeliveryBoy.objects.select_for_update().filter(pk=account.pk).first()
        if locked_account is None or active_delivery_boy(account.pk) is None:
            raise DeliveryAuthError("account_inactive", "This Delivery Boy account is inactive.", 403)
        base = AdminOTPChallenge.objects.filter(principal_type=PRINCIPAL_TYPE, principal_id=account.pk, purpose=purpose)
        latest = base.order_by("-created_at").first()
        if latest is not None:
            wait = settings.DELIVERY_OTP_RESEND_COOLDOWN_SECONDS - (now - latest.created_at).total_seconds()
            if wait > 0:
                raise DeliveryAuthError("resend_cooldown", "Please wait before requesting another OTP.", 429, retry_after=int(wait) + 1)
        if base.filter(created_at__gte=now - timedelta(hours=1)).count() >= settings.DELIVERY_OTP_MAX_SENDS_PER_HOUR:
            raise DeliveryAuthError("too_many_otp_requests", "Too many OTP requests. Please try again later.", 429, retry_after=3600)
        otp = shared_flow.new_otp()
        challenge = AdminOTPChallenge.objects.create(
            principal_type=PRINCIPAL_TYPE, principal_id=account.pk, purpose=purpose,
            otp_hash=shared_flow.otp_digest(purpose, PRINCIPAL_TYPE, account.pk, otp),
            created_at=now, expires_at=now + timedelta(seconds=settings.DELIVERY_OTP_TTL_SECONDS),
        )
        base.filter(invalidated_at__isnull=True, consumed_at__isnull=True).exclude(pk=challenge.pk).update(invalidated_at=now)
    action = "change your password" if purpose == CHANGE else "reset your password"
    body = (f"Your StokIt Delivery Boy verification code is {otp}.\n\n"
            f"Use it to {action}. It expires in {settings.DELIVERY_OTP_TTL_SECONDS // 60} minutes and can be used once.\n"
            "Never share this code with anyone. If you did not request this, ignore this email.")
    try:
        sent = send_mail("StokIt Delivery Boy verification code", body, settings.DEFAULT_FROM_EMAIL, [recipient], fail_silently=False)
    except Exception as exc:
        AdminOTPChallenge.objects.filter(pk=challenge.pk).update(invalidated_at=timezone.now())
        logger.error("Delivery Boy OTP email delivery failed error_type=%s purpose=%s", type(exc).__name__, purpose)
        raise DeliveryAuthError("email_delivery_failed", "The OTP email could not be sent. Please try again later.", 503)
    if not sent:
        AdminOTPChallenge.objects.filter(pk=challenge.pk).update(invalidated_at=timezone.now())
        logger.error("Delivery Boy OTP email backend reported zero messages purpose=%s", purpose)
        raise DeliveryAuthError("email_delivery_failed", "The OTP email could not be sent. Please try again later.", 503)
    return challenge


def _find_delivery_email(email):
    email = shared_flow.normalise_email(email)
    if not shared_flow.email_is_valid(email):
        return None
    matches = list(DeliveryBoy.objects.filter(email__iexact=email)[:2])
    if len(matches) != 1 or active_delivery_boy(matches[0].pk) is None:
        return None
    return matches[0]


def _verify_otp(delivery_boy_id, purpose, otp):
    otp = (otp or "").strip()
    if len(otp) != 6 or not otp.isdigit():
        raise DeliveryAuthError("invalid_otp", INVALID_OTP)
    now = timezone.now()
    token = None
    with transaction.atomic():
        challenge = (AdminOTPChallenge.objects.filter(
            principal_type=PRINCIPAL_TYPE, principal_id=delivery_boy_id, purpose=purpose,
            invalidated_at__isnull=True, consumed_at__isnull=True, verified_at__isnull=True,
        ).select_for_update().order_by("-created_at").first())
        if challenge is not None and challenge.expires_at > now and challenge.attempts < settings.DELIVERY_OTP_MAX_ATTEMPTS:
            expected = shared_flow.otp_digest(purpose, PRINCIPAL_TYPE, delivery_boy_id, otp)
            if hmac.compare_digest(challenge.otp_hash, expected):
                token = secrets.token_urlsafe(32)
                challenge.verified_at = now
                challenge.token_hash = shared_flow.token_digest(purpose, token)
                challenge.token_expires_at = now + timedelta(seconds=settings.DELIVERY_PASSWORD_TOKEN_TTL_SECONDS)
                challenge.save(update_fields=["verified_at", "token_hash", "token_expires_at"])
            else:
                challenge.attempts += 1
                fields = ["attempts"]
                if challenge.attempts >= settings.DELIVERY_OTP_MAX_ATTEMPTS:
                    challenge.invalidated_at = now
                    fields.append("invalidated_at")
                challenge.save(update_fields=fields)
    if token is None:
        raise DeliveryAuthError("invalid_or_expired_otp", INVALID_OTP)
    return token


def _revoke_delivery_sessions(delivery_boy_id, stamp=None):
    stamp = stamp or timezone.now()
    return DeliveryBoyAuthSession.objects.filter(delivery_boy_id=delivery_boy_id,
        is_active=True, revoked_at__isnull=True).update(is_active=False, revoked_at=stamp)


def _complete_password(purpose, token, new_password, confirm_password, delivery_boy_id=None):
    token = token.strip() if isinstance(token, str) else ""
    if not token or len(token) > 200:
        raise DeliveryAuthError("invalid_or_expired_token", INVALID_TOKEN)
    qs = AdminOTPChallenge.objects.filter(
        principal_type=PRINCIPAL_TYPE, purpose=purpose,
        token_hash=shared_flow.token_digest(purpose, token), verified_at__isnull=False,
        invalidated_at__isnull=True, consumed_at__isnull=True,
    )
    if delivery_boy_id is not None:
        qs = qs.filter(principal_id=delivery_boy_id)
    challenge = qs.first()
    now = timezone.now()
    if challenge is None or challenge.token_expires_at is None or challenge.token_expires_at <= now:
        raise DeliveryAuthError("invalid_or_expired_token", INVALID_TOKEN)
    eligible = active_delivery_boy(challenge.principal_id)
    if eligible is None:
        raise DeliveryAuthError("account_inactive", "This Delivery Boy account is inactive.", 403)
    account = eligible[0]
    shared_flow.validate_new_password(account, new_password, confirm_password)
    new_hash = hash_password(new_password)
    with transaction.atomic():
        locked = qs.select_for_update().filter(pk=challenge.pk).first()
        if locked is None or locked.token_expires_at is None or locked.token_expires_at <= timezone.now():
            raise DeliveryAuthError("invalid_or_expired_token", INVALID_TOKEN)
        locked_account = DeliveryBoy.objects.select_for_update().filter(pk=locked.principal_id).first()
        if locked_account is None or active_delivery_boy(locked.principal_id) is None:
            raise DeliveryAuthError("account_inactive", "This Delivery Boy account is inactive.", 403)
        DeliveryBoy.objects.filter(pk=locked.principal_id).update(password=new_hash)
        stamp = timezone.now()
        AdminOTPChallenge.objects.filter(
            principal_type=PRINCIPAL_TYPE, principal_id=locked.principal_id,
            invalidated_at__isnull=True, consumed_at__isnull=True,
        ).exclude(pk=locked.pk).update(invalidated_at=stamp)
        AdminOTPChallenge.objects.filter(pk=locked.pk).update(consumed_at=stamp)
        # Password changes/resets intentionally revoke every device session for this account.
        _revoke_delivery_sessions(locked.principal_id, stamp)
    logger.info("Delivery Boy password changed purpose=%s delivery_boy_id=%s", purpose, challenge.principal_id)
    return challenge.principal_id


class _PublicView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]


class _ProtectedView(APIView):
    authentication_classes = [DeliveryBoyTokenAuthentication]
    permission_classes = [IsAuthenticated]


class DeliveryBoyLoginView(_PublicView):
    def post(self, request):
        def handler():
            data = _body(request)
            _check_ip(request)
            identifier = _text(data, "username") or _text(data, "email")
            password = _text(data, "password")
            if not identifier or not password:
                return Response({"success": False, "message": "Username/email and password are required."}, status=400)
            # Serialize login against password changes so a just-revoked old-password session
            # cannot be created after the password-change transaction commits.
            with transaction.atomic():
                matches = list(DeliveryBoy.objects.select_for_update().filter(
                    Q(username__iexact=identifier) | Q(email__iexact=identifier))[:2])
                if len(matches) != 1 or not verify_php_password(password, matches[0].password):
                    return Response({"success": False, "message": "Invalid username/email or password."}, status=401)
                account = matches[0]
                eligible = active_delivery_boy(account.pk)
                if eligible is None:
                    return Response({"success": False, "message": "Invalid username/email or password."}, status=401)
                DeliveryBoy.objects.filter(pk=account.pk).update(last_login=timezone.now())
                access, refresh, session_id = _create_session(account, request, data)
                return Response({"success": True, "message": "Login successful", "access": access, "refresh": refresh,
                    "token_type": "Bearer", "session_id": str(session_id),
                    "expires_in": settings.DELIVERY_ACCESS_TOKEN_TTL_SECONDS,
                    "refresh_expires_in": settings.DELIVERY_REFRESH_TOKEN_TTL_SECONDS,
                    "delivery_boy": _profile_payload(account, eligible[1], request)})
        return _run(handler)


class DeliveryBoyForgotRequestView(_PublicView):
    def post(self, request):
        def handler():
            data = _body(request)
            email = shared_flow.normalise_email(_text(data, "email"))
            if not shared_flow.email_is_valid(email):
                return Response({"success": False, "message": "Enter a valid email address."}, status=400)
            shared_flow.ensure_email_configured()
            _check_ip(request)
            if shared_flow.rate_blocked("delivery_forgot_email", email, settings.DELIVERY_OTP_MAX_SENDS_PER_HOUR, 3600):
                return Response({"success": True, "message": GENERIC_FORGOT})
            shared_flow.rate_record("delivery_forgot_email", email)
            account = _find_delivery_email(email)
            if account is None:
                return Response({"success": True, "message": GENERIC_FORGOT})
            try:
                _issue_otp(account, FORGOT)
            except DeliveryAuthError as exc:
                if exc.code in {"resend_cooldown", "too_many_otp_requests"}:
                    return Response({"success": True, "message": GENERIC_FORGOT})
                raise
            return Response({"success": True, "message": GENERIC_FORGOT})
        return _run(handler)


class DeliveryBoyForgotVerifyView(_PublicView):
    def post(self, request):
        def handler():
            data = _body(request)
            email = shared_flow.normalise_email(_text(data, "email"))
            _check_ip(request)
            account = _find_delivery_email(email)
            if account is None:
                raise DeliveryAuthError("invalid_or_expired_otp", INVALID_OTP)
            token = _verify_otp(account.pk, FORGOT, _text(data, "otp"))
            return Response({"success": True, "message": "OTP verified successfully", "reset_token": token,
                "expires_in": settings.DELIVERY_PASSWORD_TOKEN_TTL_SECONDS})
        return _run(handler)


class DeliveryBoyForgotResetView(_PublicView):
    def post(self, request):
        def handler():
            data = _body(request)
            _check_ip(request)
            _complete_password(FORGOT, _text(data, "reset_token"), data.get("new_password"), data.get("confirm_password"))
            return Response({"success": True, "message": "Password reset successfully"})
        return _run(handler)


class DeliveryBoyChangeRequestView(_ProtectedView):
    def post(self, request):
        def handler():
            _body(request)
            _check_ip(request)
            account = DeliveryBoy.objects.filter(pk=request.user.pk).first()
            if account is None or active_delivery_boy(account.pk) is None:
                raise DeliveryAuthError("account_inactive", "This Delivery Boy account is inactive.", 403)
            _issue_otp(account, CHANGE)
            return Response({"success": True, "message": "Change-password OTP sent to your registered email."})
        return _run(handler)


class DeliveryBoyChangeVerifyView(_ProtectedView):
    def post(self, request):
        def handler():
            data = _body(request)
            _check_ip(request)
            token = _verify_otp(request.user.pk, CHANGE, _text(data, "otp"))
            return Response({"success": True, "message": "OTP verified successfully", "change_token": token,
                "expires_in": settings.DELIVERY_PASSWORD_TOKEN_TTL_SECONDS})
        return _run(handler)


class DeliveryBoyChangePasswordView(_ProtectedView):
    def post(self, request):
        def handler():
            data = _body(request)
            _check_ip(request)
            _complete_password(CHANGE, _text(data, "change_token"), data.get("new_password"),
                data.get("confirm_password"), delivery_boy_id=request.user.pk)
            return Response({"success": True, "message": "Password changed successfully. Please log in again."})
        return _run(handler)


def _profile_payload(account, metadata, request):
    image_path = reverse("delivery-profile-image") if metadata and metadata.profile_image_mime else ""
    image_url = request.build_absolute_uri(image_path) if image_path else None
    joining = getattr(metadata, "joining_date", None) if metadata else None
    return {"id": account.pk, "delivery_boy_id": account.pk, "username": account.username,
        "full_name": getattr(account, "fullname", "") or "", "email": account.email,
        "phone": getattr(account, "phone", "") or "", "address": getattr(account, "address", "") or "",
        "joining_date": joining.isoformat() if joining else None, "profile_image_url": image_url,
        "status": "ACTIVE" if metadata and metadata.is_active else "INACTIVE"}


class DeliveryBoyProfileView(_ProtectedView):
    def get(self, request):
        def handler():
            account, metadata = active_delivery_boy(request.user.pk)
            return Response({"success": True, "delivery_boy": _profile_payload(account, metadata, request)})
        return _run(handler)


class DeliveryBoyProfileImageView(_ProtectedView):
    def get(self, request):
        def handler():
            _account, metadata = active_delivery_boy(request.user.pk)
            data = bytes(metadata.profile_image) if metadata.profile_image else b""
            if not data:
                return Response({"detail": "No profile image is available."}, status=404)
            response = HttpResponse(data, content_type=metadata.profile_image_mime or "application/octet-stream")
            response["Cache-Control"] = "private, no-cache"
            response["X-Content-Type-Options"] = "nosniff"
            response["Content-Disposition"] = 'inline; filename="delivery-boy-profile.webp"'
            return response
        return _run(handler)


class DeliveryBoyRefreshView(_PublicView):
    def post(self, request):
        def handler():
            data = _body(request)
            refresh = _text(data, "refresh")
            with transaction.atomic():
                account, session, _payload = _validate_token(refresh, "refresh",
                    settings.DELIVERY_REFRESH_TOKEN_TTL_SECONDS, lock_session=True)
                now = timezone.now()
                access_expires_at = now + timedelta(seconds=settings.DELIVERY_ACCESS_TOKEN_TTL_SECONDS)
                access, rotated_refresh = _new_session_tokens(account, session.session_id,
                    access_expires_at, session.refresh_expires_at)
                session.refresh_token_hash = _token_hash(rotated_refresh)
                session.access_expires_at = access_expires_at
                session.last_used_at = now
                session.save(update_fields=["refresh_token_hash", "access_expires_at", "last_used_at"])
            return Response({"success": True, "access": access, "refresh": rotated_refresh,
                "token_type": "Bearer", "session_id": str(session.session_id),
                "expires_in": settings.DELIVERY_ACCESS_TOKEN_TTL_SECONDS,
                "refresh_expires_in": max(0, int((session.refresh_expires_at - now).total_seconds()))})
        return _run(handler)


def _session_payload(session, current_session_id):
    return {"session_id": str(session.session_id), "device_name": session.device_name,
        "device_platform": session.device_platform, "device_identifier": session.device_identifier,
        "ip_address": session.ip_address, "user_agent": session.user_agent,
        "created_at": session.created_at.isoformat(), "last_used_at": session.last_used_at.isoformat(),
        "access_expires_at": session.access_expires_at.isoformat(),
        "refresh_expires_at": session.refresh_expires_at.isoformat(),
        "is_current": session.session_id == current_session_id}


class DeliveryBoySessionsView(_ProtectedView):
    def get(self, request):
        def handler():
            now = timezone.now()
            sessions = DeliveryBoyAuthSession.objects.filter(delivery_boy_id=request.user.pk,
                is_active=True, revoked_at__isnull=True, refresh_expires_at__gt=now).order_by("-last_used_at")
            return Response({"success": True, "sessions": [_session_payload(row, request.user.session_id) for row in sessions]})
        return _run(handler)


class DeliveryBoyRevokeSessionView(_ProtectedView):
    def post(self, request):
        def handler():
            data = _body(request)
            try:
                target_session_id = uuid.UUID(_text(data, "session_id"))
            except (ValueError, TypeError, AttributeError):
                raise DeliveryAuthError("invalid_session_id", "A valid session_id is required.")
            with transaction.atomic():
                target = DeliveryBoyAuthSession.objects.select_for_update().filter(
                    session_id=target_session_id, delivery_boy_id=request.user.pk,
                    is_active=True, revoked_at__isnull=True).first()
                if target is None:
                    raise DeliveryAuthError("session_not_found", "The active session was not found.", 404)
                target.is_active = False
                target.revoked_at = timezone.now()
                target.save(update_fields=["is_active", "revoked_at"])
            return Response({"success": True, "message": "Session revoked successfully."})
        return _run(handler)


class DeliveryBoyLogoutView(_ProtectedView):
    def post(self, request):
        def handler():
            with transaction.atomic():
                session = DeliveryBoyAuthSession.objects.select_for_update().filter(
                    session_id=request.user.session_id, delivery_boy_id=request.user.pk,
                    is_active=True, revoked_at__isnull=True).first()
                if session is None:
                    raise DeliveryAuthError("session_revoked", "This session has already been revoked.", 401)
                session.is_active = False
                session.revoked_at = timezone.now()
                session.save(update_fields=["is_active", "revoked_at"])
            return Response({"success": True, "message": "This device has been logged out."})
        return _run(handler)
