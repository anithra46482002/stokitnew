"""Admin Profile, change-password and forgot-password endpoints (React admin, /admin-api/...).

All business rules live in adminapi.password_flow; these views only parse input and shape responses.
"""
import logging

from django.conf import settings
from django.db import OperationalError, ProgrammingError
from rest_framework.exceptions import ParseError
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import password_flow as flow
from .db_errors import log_db_error
from .image_urls import image_url
from .profile_image_storage import get_admin_profile_image_path

logger = logging.getLogger("stokit.profile")


class ActiveStaffPermission(BasePermission):
    """Authenticated AND holding an active role assignment (an inactive account must not use protected APIs)."""
    message = "An active administrator role assignment is required for this operation."

    def has_permission(self, request, view):
        user = request.user
        return bool(getattr(user, "is_authenticated", False) and getattr(user, "role", None))


def _body(request):
    data = request.data
    if not hasattr(data, "get"):
        raise ParseError("Request body must be a JSON object.")
    return data


def _text(data, key):
    """A string value, or '' for anything else (lists/numbers/objects are never stringified into secrets)."""
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _error_response(exc):
    body = {"success": False, "code": exc.code, "message": exc.message}
    extra = dict(exc.extra)
    if "errors" in extra:
        body["errors"] = extra.pop("errors")
    response = Response(body, status=exc.status)
    retry_after = extra.get("retry_after")
    if retry_after:
        body["retry_after"] = retry_after
        response["Retry-After"] = str(retry_after)
    return response


def _db_response(context, exc):
    category, _ = log_db_error(context, exc)
    if category == "table_missing":
        code, message = "schema_not_provisioned", "The password-reset tables have not been provisioned in the configured database yet. Contact the system administrator."
    elif category == "database_unavailable":
        code, message = "database_unavailable", "The configured database is unavailable."
    else:
        code, message = "admin_database_error", "The request could not be completed."
    return Response({"success": False, "code": code, "message": message}, status=503)


class _FlowView(APIView):
    """Runs a flow handler and converts FlowError / database failures into safe JSON responses."""
    context = "Admin password flow"

    def run(self, handler):
        try:
            return handler()
        except flow.FlowError as exc:
            return _error_response(exc)
        except (OperationalError, ProgrammingError) as exc:
            return _db_response(self.context, exc)


def _sent_payload(account, message):
    return {
        "success": True,
        "message": message,
        "masked_email": flow.mask_email(account.email),
        "expires_in": settings.ADMIN_OTP_TTL_SECONDS,
        "resend_after": settings.ADMIN_OTP_RESEND_COOLDOWN_SECONDS,
    }


# ------------------------------------------------------------------------------------------------ profile
class AdminProfileView(_FlowView):
    """GET /admin-api/profile - the authenticated account's own profile. Read-only: role, username, status
    and e-mail cannot be edited here (PATCH/PUT/POST are not allowed), so nobody can promote themselves."""
    permission_classes = [IsAuthenticated, ActiveStaffPermission]
    http_method_names = ["get", "head", "options"]
    context = "Admin profile lookup"

    def get(self, request):
        def handler():
            user = request.user
            account = flow.load_account(user.principal_type, user.pk)
            if account is None:
                return Response({"success": False, "message": "Account not found."}, status=404)
            assignment = flow.role_assignment(user.principal_type, user.pk)
            last_login = getattr(account, "last_login", None)
            return Response({"success": True, "profile": {
                "id": account.pk,
                "fullname": getattr(account, "fullname", "") or "",
                "username": account.username,
                "email": account.email,
                "role": user.role,
                "status": "ACTIVE" if assignment is not None and assignment.active else "INACTIVE",
                "principal_type": user.principal_type,
                "last_login": last_login.isoformat() if last_login else None,
                "profile_image_url": image_url(get_admin_profile_image_path(user.principal_type, account.pk), request=request),
            }})
        return self.run(handler)


# ------------------------------------------------------------------------------------------------ change password
class ChangePasswordVerifyCurrentView(_FlowView):
    """POST {current_password}: verify it, then e-mail an OTP to the account's registered address."""
    permission_classes = [IsAuthenticated, ActiveStaffPermission]
    context = "Admin change-password (verify current)"

    def post(self, request):
        def handler():
            data = _body(request)
            account, _challenge = flow.change_verify_current(request, request.user, _text(data, "current_password"))
            return Response(_sent_payload(account, "A verification code has been sent to your registered email address."))
        return self.run(handler)


class ChangePasswordSendOtpView(_FlowView):
    """POST (no body): resend the OTP; only valid shortly after the current password was verified."""
    permission_classes = [IsAuthenticated, ActiveStaffPermission]
    context = "Admin change-password (resend OTP)"

    def post(self, request):
        def handler():
            _body(request)
            account, _challenge = flow.change_resend(request, request.user)
            return Response(_sent_payload(account, "A new verification code has been sent to your registered email address."))
        return self.run(handler)


class ChangePasswordVerifyOtpView(_FlowView):
    """POST {otp}: returns a short-lived, single-use verification_token for the confirm step."""
    permission_classes = [IsAuthenticated, ActiveStaffPermission]
    context = "Admin change-password (verify OTP)"

    def post(self, request):
        def handler():
            data = _body(request)
            flow.check_flow_ip_limit(request)
            token, ttl = flow.verify_otp(request.user.principal_type, request.user.pk, flow.CHANGE, _text(data, "otp"))
            return Response({"success": True, "message": "OTP verified.", "verification_token": token, "expires_in": ttl})
        return self.run(handler)


class ChangePasswordConfirmView(_FlowView):
    """POST {verification_token, new_password, confirm_password}: set the new password."""
    permission_classes = [IsAuthenticated, ActiveStaffPermission]
    context = "Admin change-password (confirm)"

    def post(self, request):
        def handler():
            data = _body(request)
            flow.check_flow_ip_limit(request)
            ptype, pid = flow.complete_password_change(
                flow.CHANGE, _text(data, "verification_token"), data.get("new_password"), data.get("confirm_password"),
                principal=(request.user.principal_type, request.user.pk),
            )
            # Other sessions were revoked; hand this browser a fresh token so it is not signed out needlessly.
            return Response({"success": True, "message": "Password changed successfully.", "token": flow.issue_admin_token(ptype, pid)})
        return self.run(handler)


# ------------------------------------------------------------------------------------------------ forgot password
class _PublicFlowView(_FlowView):
    authentication_classes = []
    permission_classes = [AllowAny]


class ForgotPasswordSendOtpView(_PublicFlowView):
    """POST {email}: generic response whether or not an eligible account exists."""
    context = "Admin forgot-password (send OTP)"

    def post(self, request):
        def handler():
            data = _body(request)
            flow.forgot_send_otp(request, _text(data, "email"))
            return Response({"success": True, "message": flow.FORGOT_GENERIC_MESSAGE,
                             "expires_in": settings.ADMIN_OTP_TTL_SECONDS,
                             "resend_after": settings.ADMIN_OTP_RESEND_COOLDOWN_SECONDS})
        return self.run(handler)


class ForgotPasswordVerifyOtpView(_PublicFlowView):
    """POST {email, otp}: returns a short-lived, single-use reset_token."""
    context = "Admin forgot-password (verify OTP)"

    def post(self, request):
        def handler():
            data = _body(request)
            token, ttl = flow.forgot_verify_otp(request, _text(data, "email"), _text(data, "otp"))
            return Response({"success": True, "message": "OTP verified.", "reset_token": token, "expires_in": ttl})
        return self.run(handler)


class ForgotPasswordResetView(_PublicFlowView):
    """POST {reset_token, new_password, confirm_password}: the token, not any client-supplied id, identifies the account."""
    context = "Admin forgot-password (reset)"

    def post(self, request):
        def handler():
            data = _body(request)
            flow.check_flow_ip_limit(request)
            flow.complete_password_change(
                flow.FORGOT, _text(data, "reset_token"), data.get("new_password"), data.get("confirm_password"),
            )
            return Response({"success": True, "message": "Password reset successfully."})
        return self.run(handler)
