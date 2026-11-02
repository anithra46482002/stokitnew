from django.conf import settings
from django.core import signing
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from django.db import OperationalError, ProgrammingError

from legacydb.models import AdminAccount, Salesman, DeliveryBoy
from accounts.models import AdminCredentialState, AdminRoleAssignment
from accounts.hashers import verify_php_password
from .db_errors import log_db_error


def active_admin_role(principal_type, principal_id, *, raise_on_error=False):
    try:
        assignment = AdminRoleAssignment.objects.filter(
            principal_type=principal_type, principal_id=principal_id, active=True
        ).first()
        return assignment.role if assignment else None
    except (OperationalError, ProgrammingError) as exc:
        # Role migration has not been applied or DB is unavailable: fail closed on protected APIs.
        if raise_on_error:
            # The strict caller owns the HTTP response and logs this once with its context.
            raise
        log_db_error('Admin role lookup', exc)
        return None


class AdminPrincipal:
    """Authenticated legacy staff principal with an explicit database role mapping."""
    is_authenticated = True
    is_anonymous = False

    def __init__(self, account, principal_type):
        self.id = account.pk
        self.pk = account.pk
        self.username = account.username
        self.email = account.email
        self.fullname = getattr(account, 'fullname', '')
        self.principal_type = principal_type
        self.role = active_admin_role(principal_type, account.pk)
def _token_issued_at(token):
    """Issue time (epoch seconds) embedded in a django.core.signing token, or None if unreadable."""
    try:
        return signing.b62_decode(token.rsplit(":", 2)[1])
    except (IndexError, ValueError):
        return None


def _tokens_valid_after(principal_type, principal_id):
    """Epoch second before which this principal's tokens are revoked (password changed/reset); 0 = none.

    If the revocation table is not provisioned yet (migration pending) or the database errors, no revocation
    marker can be read: log it and keep existing behaviour instead of locking every administrator out.
    """
    try:
        value = AdminCredentialState.objects.filter(
            principal_type=principal_type, principal_id=principal_id
        ).values_list("tokens_valid_after", flat=True).first()
    except (OperationalError, ProgrammingError) as exc:
        log_db_error("Admin token revocation lookup", exc)
        return 0
    return value or 0


class AdminTokenAuthentication(BaseAuthentication):
    keyword = b"bearer"

    def authenticate_header(self, request):
        # Advertise the supported scheme so DRF returns 401 for missing credentials.
        # Role denials for authenticated principals remain HTTP 403.
        return "Bearer"
    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts:
            return None
        if parts[0].lower() != self.keyword:
            return None
        if len(parts) != 2:
            raise AuthenticationFailed("Invalid authorization header")
        try:
            payload = signing.loads(
                parts[1].decode('ascii'),
                salt='stokit.admin-token',
                max_age=settings.ADMIN_TOKEN_MAX_AGE_SECONDS,
            )
            if 'admin_id' in payload:
                principal_type = 'ADMIN'
                principal_id = payload['admin_id']
                account = AdminAccount.objects.get(pk=principal_id)
            else:
                principal_type = payload.get('principal_type')
                principal_id = payload.get('principal_id')
                model = {'ADMIN': AdminAccount, 'SALESMAN': Salesman, 'DELIVERY_BOY': DeliveryBoy}.get(principal_type)
                if model is None or principal_id is None:
                    raise KeyError('principal')
                account = model.objects.get(pk=principal_id)
        except (ValueError, TypeError, UnicodeDecodeError, signing.BadSignature, signing.SignatureExpired,
                AdminAccount.DoesNotExist, Salesman.DoesNotExist, DeliveryBoy.DoesNotExist, KeyError):
            raise AuthenticationFailed('Invalid or expired admin token')
        # Tokens issued before the last password change/reset are revoked (the tokens themselves are stateless).
        issued_at = _token_issued_at(parts[1].decode('ascii'))
        if issued_at is None or issued_at < _tokens_valid_after(principal_type, account.pk):
            raise AuthenticationFailed('Invalid or expired admin token')
        return AdminPrincipal(account, principal_type), parts[1].decode('ascii')
