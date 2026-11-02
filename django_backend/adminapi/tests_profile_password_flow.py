"""Database-free tests for the Admin Profile, change-password and forgot-password flows.

The project's unmanaged legacy tables cannot be created in a Django test database (migration legacydb.0004
alters zg_variant), so - like the rest of this suite - these tests never touch PostgreSQL. A tiny in-memory
queryset stands in for the ORM so the REAL security logic (digests, expiry, attempts, replay, purpose binding,
revocation, rate limits) is exercised end to end through the real views and authentication class.
"""
import importlib
import re
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.core import mail, signing
from django.db import ProgrammingError, migrations
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.hashers import hash_password, verify_php_password
from accounts.models import AdminAuthRateEvent, AdminCredentialState, AdminOTPChallenge, AdminRoleAssignment
from adminapi import auth as admin_auth
from adminapi import password_flow as flow
from adminapi import profile as views
from legacydb.models import AdminAccount, DeliveryBoy, Salesman

OLD_PASSWORD = "Old-Passw0rd!x9"
NEW_PASSWORD = "Brand-New#Secret47"
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
_OLD_HASH = hash_password(OLD_PASSWORD)


# ----------------------------------------------------------------------------------------- in-memory ORM
class Row:
    # Django's UserAttributeSimilarityValidator asks the user object for model metadata when it rejects a password.
    _meta = SimpleNamespace(get_field=lambda name: SimpleNamespace(verbose_name=name))

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.pk = kw.get("id")

    def save(self, update_fields=None):  # rows are shared objects: mutations are already "persisted"
        pass


def _match(row, key, value):
    field, _, op = key.partition("__")
    got = getattr(row, "pk" if field == "pk" else field, None)
    if op in ("", "exact"):
        return got == value
    if op == "iexact":
        return isinstance(got, str) and got.lower() == value.lower()
    if op == "isnull":
        return (got is None) == value
    if got is None:
        return False
    return {"lt": got < value if op == "lt" else None, "lte": got <= value if op == "lte" else None,
            "gt": got > value if op == "gt" else None, "gte": got >= value if op == "gte" else None}[op]


class Values:
    def __init__(self, items):
        self.items = items

    def first(self):
        return self.items[0] if self.items else None


class QS:
    def __init__(self, manager, rows):
        self.manager, self.rows = manager, list(rows)

    def filter(self, **kw):
        return QS(self.manager, [r for r in self.rows if all(_match(r, k, v) for k, v in kw.items())])

    def exclude(self, **kw):
        return QS(self.manager, [r for r in self.rows if not all(_match(r, k, v) for k, v in kw.items())])

    def order_by(self, name):
        field = name.lstrip("-")
        return QS(self.manager, sorted(self.rows, key=lambda r: getattr(r, field), reverse=name.startswith("-")))

    def select_for_update(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def count(self):
        return len(self.rows)

    def exists(self):
        return bool(self.rows)

    def update(self, **kw):
        for r in self.rows:
            for k, v in kw.items():
                setattr(r, k, v)
        return len(self.rows)

    def delete(self):
        self.manager.store[:] = [r for r in self.manager.store if r not in self.rows]

    def values_list(self, field, flat=False):
        return Values([getattr(r, field) for r in self.rows])

    def __getitem__(self, item):
        return QS(self.manager, self.rows[item])

    def __iter__(self):
        return iter(self.rows)


class Manager:
    def __init__(self, defaults=None, does_not_exist=Exception):
        self.store, self.next_id, self.defaults, self.does_not_exist = [], 1, defaults or {}, does_not_exist

    def _qs(self):
        return QS(self, self.store)

    filter = lambda self, **kw: self._qs().filter(**kw)  # noqa: E731
    exclude = lambda self, **kw: self._qs().exclude(**kw)  # noqa: E731

    def create(self, **kw):
        values = {k: (v() if callable(v) else v) for k, v in self.defaults.items()}
        values.update(kw)
        values.setdefault("id", self.next_id)
        self.next_id = max(self.next_id, values["id"]) + 1
        row = Row(**values)
        self.store.append(row)
        return row

    def get(self, **kw):
        found = self.filter(**kw).first()
        if found is None:
            raise self.does_not_exist()
        return found

    def update_or_create(self, defaults=None, **kw):
        row = self.filter(**kw).first()
        if row is None:
            return self.create(**kw, **(defaults or {})), True
        for k, v in (defaults or {}).items():
            setattr(row, k, v)
        return row, False


def _now():
    return timezone.now()


class World:
    """Fake tables + patches. Accounts are added with add_account()."""

    def __init__(self):
        self.challenges = Manager({"attempts": 0, "current_password_verified_at": None, "verified_at": None,
                                   "token_hash": "", "token_expires_at": None, "invalidated_at": None,
                                   "consumed_at": None, "created_at": _now})
        self.rates = Manager({"created_at": _now})
        self.states = Manager({"updated_at": _now})
        self.roles = Manager({"active": True})
        self.accounts = {
            "ADMIN": Manager(does_not_exist=AdminAccount.DoesNotExist),
            "SALESMAN": Manager(does_not_exist=Salesman.DoesNotExist),
            "DELIVERY_BOY": Manager(does_not_exist=DeliveryBoy.DoesNotExist),
        }
        self.patchers = [
            mock.patch.object(AdminOTPChallenge, "objects", self.challenges),
            mock.patch.object(AdminAuthRateEvent, "objects", self.rates),
            mock.patch.object(AdminCredentialState, "objects", self.states),
            mock.patch.object(AdminRoleAssignment, "objects", self.roles),
            mock.patch.object(AdminAccount, "objects", self.accounts["ADMIN"]),
            mock.patch.object(Salesman, "objects", self.accounts["SALESMAN"]),
            mock.patch.object(DeliveryBoy, "objects", self.accounts["DELIVERY_BOY"]),
            mock.patch.object(views, "get_admin_profile_image_path", return_value=""),
            mock.patch.object(flow, "transaction", SimpleNamespace(atomic=lambda: nullcontext())),
        ]
        for p in self.patchers:
            p.start()

    def stop(self):
        for p in reversed(self.patchers):
            p.stop()

    def add_account(self, ptype, pk, *, username, email, fullname="Test Person", role="ADMIN", active=True,
                    password=_OLD_HASH):
        row = self.accounts[ptype].create(id=pk, username=username, email=email, fullname=fullname, password=password)
        if role:
            self.roles.create(principal_type=ptype, principal_id=pk, role=role, active=active)
        return row


def principal(ptype, pk, role="SUPER_ADMIN"):
    return SimpleNamespace(principal_type=ptype, pk=pk, id=pk, role=role, is_authenticated=True)


def last_otp():
    body = mail.outbox[-1].body
    return re.search(r"code is (\d{6})", body).group(1)


@override_settings(EMAIL_BACKEND=LOCMEM, DEFAULT_FROM_EMAIL="no-reply@stokit.test")
class FlowBase(SimpleTestCase):
    def setUp(self):
        mail.outbox = []
        self.world = World()
        self.addCleanup(self.world.stop)
        self.admin = self.world.add_account("ADMIN", 1, username="boss", email="boss@gmail.com",
                                            fullname="Boss Admin", role="SUPER_ADMIN")
        self.other = self.world.add_account("SALESMAN", 7, username="sam", email="sam@gmail.com",
                                            fullname="Sam Sales", role="SALESMAN")
        self.me = principal("ADMIN", 1)

    def post(self, view, data=None, user=None, ip="10.0.0.1"):
        request = APIRequestFactory().post("/admin-api/x", data if data is not None else {}, format="json", REMOTE_ADDR=ip)
        if user is not None:
            force_authenticate(request, user=user)
        return view.as_view()(request)

    def start_change(self, password=OLD_PASSWORD):
        response = self.post(views.ChangePasswordVerifyCurrentView, {"current_password": password}, self.me)
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def verified_change_token(self):
        self.start_change()
        response = self.post(views.ChangePasswordVerifyOtpView, {"otp": last_otp()}, self.me)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["verification_token"]

    def age_latest_challenge(self, seconds):
        c = sorted(self.world.challenges.store, key=lambda r: r.created_at)[-1]
        c.created_at -= timedelta(seconds=seconds)
        return c

    def forgot_token(self, email="boss@gmail.com"):
        self.post(views.ForgotPasswordSendOtpView, {"email": email})
        response = self.post(views.ForgotPasswordVerifyOtpView, {"email": email, "otp": last_otp()})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["reset_token"]


# ----------------------------------------------------------------------------------------------- profile
class ProfileTests(FlowBase):
    def get(self, user):
        request = APIRequestFactory().get("/admin-api/profile")
        if user is not None:
            force_authenticate(request, user=user)
        return views.AdminProfileView.as_view()(request)

    def test_authenticated_user_gets_their_actual_profile(self):
        response = self.get(self.me)
        self.assertEqual(response.status_code, 200)
        profile = response.data["profile"]
        self.assertEqual(profile["fullname"], "Boss Admin")
        self.assertEqual(profile["username"], "boss")
        self.assertEqual(profile["email"], "boss@gmail.com")
        self.assertEqual(profile["role"], "SUPER_ADMIN")
        self.assertEqual(profile["status"], "ACTIVE")

    def test_other_accounts_data_is_never_returned(self):
        profile = self.get(principal("SALESMAN", 7, role="SALESMAN")).data["profile"]
        self.assertEqual((profile["username"], profile["role"], profile["principal_type"]), ("sam", "SALESMAN", "SALESMAN"))

    def test_sensitive_values_are_never_returned(self):
        response = self.get(self.me)
        self.assertEqual(set(response.data["profile"]),
                         {"id", "fullname", "username", "email", "role", "status", "principal_type", "last_login", "profile_image_url"})
        self.assertNotIn(_OLD_HASH, str(response.data))
        self.assertNotIn("argon2", str(response.data))

    def test_unauthenticated_request_is_rejected(self):
        self.assertEqual(self.get(None).status_code, 401)

    def test_account_without_active_role_is_rejected(self):
        self.assertEqual(self.get(principal("ADMIN", 1, role=None)).status_code, 403)

    def test_profile_cannot_be_edited_so_role_and_username_cannot_be_changed(self):
        for method in ("patch", "put", "post", "delete"):
            request = getattr(APIRequestFactory(), method)("/admin-api/profile", {"role": "SUPER_ADMIN", "username": "x"}, format="json")
            force_authenticate(request, user=principal("SALESMAN", 7, role="SALESMAN"))
            self.assertEqual(views.AdminProfileView.as_view()(request).status_code, 405, method)
        self.assertEqual(self.other.username, "sam")
        self.assertEqual(self.world.roles.filter(principal_type="SALESMAN", principal_id=7).first().role, "SALESMAN")

    def test_inactive_assignment_is_reported_inactive_but_never_authorised(self):
        self.world.roles.filter(principal_type="ADMIN", principal_id=1).update(active=False)
        self.assertEqual(self.get(principal("ADMIN", 1, role=None)).status_code, 403)
        self.assertEqual(flow.account_has_active_role("ADMIN", 1), False)


# ----------------------------------------------------------------------------------------------- change password
class ChangePasswordTests(FlowBase):
    def test_incorrect_current_password_is_rejected_and_sends_no_otp(self):
        response = self.post(views.ChangePasswordVerifyCurrentView, {"current_password": "wrong-password"}, self.me)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "current_password_incorrect")
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self.world.challenges.store, [])

    def test_missing_or_non_string_current_password_is_rejected(self):
        for body in ({}, {"current_password": ["x"]}, {"current_password": 12345}):
            self.assertEqual(self.post(views.ChangePasswordVerifyCurrentView, body, self.me).status_code, 400)
        self.assertEqual(mail.outbox, [])

    def test_correct_current_password_starts_otp_flow_to_registered_email_only(self):
        response = self.post(views.ChangePasswordVerifyCurrentView,
                             {"current_password": OLD_PASSWORD, "email": "attacker@evil.test", "to": "attacker@evil.test"}, self.me)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["masked_email"], "b***@gmail.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["boss@gmail.com"])
        otp = last_otp()
        self.assertNotIn(otp, str(response.data))              # the OTP is never returned by the API
        self.assertNotIn("boss@gmail.com", str(response.data))  # nor the unmasked address
        stored = self.world.challenges.store[0]
        self.assertNotIn(otp, stored.otp_hash)                  # only a digest is stored
        self.assertEqual(stored.purpose, "change_password")
        self.assertEqual(stored.expires_at - stored.created_at, timedelta(seconds=300))

    def test_repeated_wrong_current_passwords_are_throttled_even_for_the_right_one(self):
        for _ in range(5):
            self.assertEqual(self.post(views.ChangePasswordVerifyCurrentView, {"current_password": "nope"}, self.me).status_code, 400)
        blocked = self.post(views.ChangePasswordVerifyCurrentView, {"current_password": OLD_PASSWORD}, self.me)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked["Retry-After"], "900")
        self.assertEqual(mail.outbox, [])

    def test_unauthenticated_user_cannot_use_change_flow(self):
        for view in (views.ChangePasswordVerifyCurrentView, views.ChangePasswordSendOtpView,
                     views.ChangePasswordVerifyOtpView, views.ChangePasswordConfirmView):
            self.assertEqual(self.post(view, {"current_password": OLD_PASSWORD}).status_code, 401, view.__name__)
        self.assertEqual(mail.outbox, [])

    def test_inactive_account_cannot_start_change_flow(self):
        self.assertEqual(self.post(views.ChangePasswordVerifyCurrentView, {"current_password": OLD_PASSWORD},
                                   principal("ADMIN", 1, role=None)).status_code, 403)

    def test_email_service_not_configured_never_claims_an_otp_was_sent(self):
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend"):
            response = self.post(views.ChangePasswordVerifyCurrentView, {"current_password": OLD_PASSWORD}, self.me)
        self.assertEqual((response.status_code, response.data["code"], response.data["success"]), (503, "email_not_configured", False))
        self.assertEqual(self.world.challenges.store, [])

    def test_smtp_failure_returns_safe_error_and_invalidates_the_challenge(self):
        with mock.patch.object(flow, "send_mail", side_effect=OSError("smtp exploded: secret-host")):
            response = self.post(views.ChangePasswordVerifyCurrentView, {"current_password": OLD_PASSWORD}, self.me)
        self.assertEqual((response.status_code, response.data["code"]), (503, "email_delivery_failed"))
        self.assertNotIn("smtp exploded", str(response.data))
        self.assertTrue(all(c.invalidated_at for c in self.world.challenges.store))

    def test_account_without_valid_registered_email_gets_clear_error(self):
        self.admin.email = "not-an-email"
        response = self.post(views.ChangePasswordVerifyCurrentView, {"current_password": OLD_PASSWORD}, self.me)
        self.assertEqual((response.status_code, response.data["code"]), (400, "no_registered_email"))

    def test_resend_requires_prior_current_password_verification(self):
        response = self.post(views.ChangePasswordSendOtpView, {}, self.me)
        self.assertEqual((response.status_code, response.data["code"]), (400, "current_password_required"))
        self.assertEqual(mail.outbox, [])

    def test_resend_has_cooldown_then_supersedes_the_previous_otp(self):
        self.start_change()
        first = last_otp()
        cooling = self.post(views.ChangePasswordSendOtpView, {}, self.me)
        self.assertEqual((cooling.status_code, cooling.data["code"]), (429, "resend_cooldown"))
        self.assertGreater(cooling.data["retry_after"], 0)
        self.age_latest_challenge(61)
        again = self.post(views.ChangePasswordSendOtpView, {}, self.me)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)
        second = last_otp()
        old = self.post(views.ChangePasswordVerifyOtpView, {"otp": first}, self.me) if first != second else None
        if old is not None:
            self.assertEqual(old.status_code, 400)  # the superseded OTP is dead
        self.assertEqual(self.post(views.ChangePasswordVerifyOtpView, {"otp": second}, self.me).status_code, 200)

    def test_resend_is_limited_per_hour(self):
        self.start_change()
        for _ in range(4):
            self.age_latest_challenge(61)
            self.assertEqual(self.post(views.ChangePasswordSendOtpView, {}, self.me).status_code, 200)
        self.age_latest_challenge(61)
        limited = self.post(views.ChangePasswordSendOtpView, {}, self.me)
        self.assertEqual((limited.status_code, limited.data["code"]), (429, "too_many_otp_requests"))

    def test_wrong_otp_is_rejected_and_attempts_are_limited(self):
        self.start_change()
        real = last_otp()
        wrong = "000000" if real != "000000" else "111111"
        for _ in range(5):
            r = self.post(views.ChangePasswordVerifyOtpView, {"otp": wrong}, self.me)
            self.assertEqual((r.status_code, r.data["code"]), (400, "invalid_or_expired_otp"))
        locked = self.post(views.ChangePasswordVerifyOtpView, {"otp": real}, self.me)  # right OTP, but locked out
        self.assertEqual((locked.status_code, locked.data["code"]), (400, "invalid_or_expired_otp"))

    def test_malformed_otp_is_rejected(self):
        self.start_change()
        for otp in ("", "12345", "1234567", "abcdef", ["123456"], None):
            self.assertEqual(self.post(views.ChangePasswordVerifyOtpView, {"otp": otp}, self.me).status_code, 400)

    def test_expired_otp_is_rejected(self):
        self.start_change()
        otp = last_otp()
        self.world.challenges.store[0].expires_at = timezone.now() - timedelta(seconds=1)
        self.assertEqual(self.post(views.ChangePasswordVerifyOtpView, {"otp": otp}, self.me).status_code, 400)

    def test_otp_cannot_be_reused(self):
        self.start_change()
        otp = last_otp()
        self.assertEqual(self.post(views.ChangePasswordVerifyOtpView, {"otp": otp}, self.me).status_code, 200)
        self.assertEqual(self.post(views.ChangePasswordVerifyOtpView, {"otp": otp}, self.me).status_code, 400)

    def test_otp_of_another_account_does_not_work(self):
        self.start_change()
        otp = last_otp()
        self.assertEqual(self.post(views.ChangePasswordVerifyOtpView, {"otp": otp}, principal("SALESMAN", 7, role="SALESMAN")).status_code, 400)

    def test_forgot_password_otp_cannot_be_used_for_change_password(self):
        self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        forgot_otp = last_otp()
        response = self.post(views.ChangePasswordVerifyOtpView, {"otp": forgot_otp}, self.me)
        self.assertEqual(response.status_code, 400)

    def test_change_password_otp_cannot_be_used_for_forgot_password(self):
        self.start_change()
        change_otp = last_otp()
        response = self.post(views.ForgotPasswordVerifyOtpView, {"email": "boss@gmail.com", "otp": change_otp})
        self.assertEqual(response.status_code, 400)

    def test_cannot_confirm_without_verifying_otp(self):
        self.start_change()  # OTP sent but never verified
        for token in ("", "made-up-token", "otp_verified"):
            r = self.post(views.ChangePasswordConfirmView, {"verification_token": token, "new_password": NEW_PASSWORD,
                                                            "confirm_password": NEW_PASSWORD, "otp_verified": True}, self.me)
            self.assertEqual(r.status_code, 400, token)
        self.assertTrue(verify_php_password(OLD_PASSWORD, self.admin.password))

    def test_verification_token_from_forgot_flow_does_not_work_for_change(self):
        token = self.forgot_token()
        r = self.post(views.ChangePasswordConfirmView, {"verification_token": token, "new_password": NEW_PASSWORD,
                                                        "confirm_password": NEW_PASSWORD}, self.me)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(verify_php_password(OLD_PASSWORD, self.admin.password))

    def test_verification_token_belongs_to_the_caller(self):
        token = self.verified_change_token()
        r = self.post(views.ChangePasswordConfirmView, {"verification_token": token, "new_password": NEW_PASSWORD,
                                                        "confirm_password": NEW_PASSWORD}, principal("SALESMAN", 7, role="SALESMAN"))
        self.assertEqual(r.status_code, 400)
        self.assertTrue(verify_php_password(OLD_PASSWORD, self.admin.password))
        self.assertTrue(verify_php_password(OLD_PASSWORD, self.other.password))

    def test_mismatched_weak_and_unchanged_passwords_are_rejected_without_consuming_the_token(self):
        token = self.verified_change_token()
        cases = [
            (NEW_PASSWORD, NEW_PASSWORD + "x", "password_mismatch"),
            ("12345678", "12345678", "weak_password"),
            ("short1!", "short1!", "weak_password"),
            ("password123", "password123", "weak_password"),
            (OLD_PASSWORD, OLD_PASSWORD, "password_unchanged"),
            ("", "", "validation_error"),
        ]
        for new, confirm, code in cases:
            r = self.post(views.ChangePasswordConfirmView, {"verification_token": token, "new_password": new, "confirm_password": confirm}, self.me)
            self.assertEqual((r.status_code, r.data["code"]), (400, code), (new, code))
        self.assertTrue(verify_php_password(OLD_PASSWORD, self.admin.password))
        ok = self.post(views.ChangePasswordConfirmView, {"verification_token": token, "new_password": NEW_PASSWORD,
                                                         "confirm_password": NEW_PASSWORD}, self.me)
        self.assertEqual(ok.status_code, 200)  # same token still works after validation errors

    def test_password_similar_to_username_is_allowed_by_configured_validators(self):
        token = self.verified_change_token()
        password = 'boss@gmail.com1'
        r = self.post(views.ChangePasswordConfirmView, {'verification_token': token, 'new_password': password,
                                                        'confirm_password': password}, self.me)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(verify_php_password(password, self.admin.password))

    def test_successful_change_updates_hash_consumes_challenge_and_returns_fresh_token(self):
        token = self.verified_change_token()
        r = self.post(views.ChangePasswordConfirmView, {"verification_token": token, "new_password": NEW_PASSWORD,
                                                        "confirm_password": NEW_PASSWORD}, self.me)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["success"])
        self.assertTrue(verify_php_password(NEW_PASSWORD, self.admin.password))
        self.assertFalse(verify_php_password(OLD_PASSWORD, self.admin.password))
        self.assertTrue(self.admin.password.startswith("$argon2"))  # hashed, never plaintext
        self.assertNotIn(NEW_PASSWORD, str(r.data))
        self.assertEqual(signing.loads(r.data["token"], salt="stokit.admin-token"), {"admin_id": 1})
        self.assertTrue(all(c.consumed_at or c.invalidated_at for c in self.world.challenges.store))
        replay = self.post(views.ChangePasswordConfirmView, {"verification_token": token, "new_password": NEW_PASSWORD + "z",
                                                             "confirm_password": NEW_PASSWORD + "z"}, self.me)
        self.assertEqual(replay.status_code, 400)  # challenge is single use
        self.assertTrue(verify_php_password(NEW_PASSWORD, self.admin.password))
        self.assertEqual(self.other.password, _OLD_HASH)  # nobody else's password changed

    def test_completion_token_expires(self):
        token = self.verified_change_token()
        self.world.challenges.store[-1].token_expires_at = timezone.now() - timedelta(seconds=1)
        r = self.post(views.ChangePasswordConfirmView, {"verification_token": token, "new_password": NEW_PASSWORD,
                                                        "confirm_password": NEW_PASSWORD}, self.me)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(verify_php_password(OLD_PASSWORD, self.admin.password))

    def test_new_otp_invalidates_an_already_verified_token(self):
        token = self.verified_change_token()
        self.age_latest_challenge(61)
        # verified challenge is "verified", so resend needs a fresh current-password check
        self.start_change()
        r = self.post(views.ChangePasswordConfirmView, {"verification_token": token, "new_password": NEW_PASSWORD,
                                                        "confirm_password": NEW_PASSWORD}, self.me)
        self.assertEqual(r.status_code, 400)

    def test_session_policy_other_tokens_revoked_current_session_continues(self):
        with mock.patch("time.time", return_value=1_800_000_000):
            old_token = flow.issue_admin_token("ADMIN", 1)
            token = self.verified_change_token()
        with mock.patch("time.time", return_value=1_800_000_100):
            r = self.post(views.ChangePasswordConfirmView, {"verification_token": token, "new_password": NEW_PASSWORD,
                                                            "confirm_password": NEW_PASSWORD}, self.me)
            self.assertEqual(r.status_code, 200)
            with self.assertRaises(AuthenticationFailed):
                self.authenticate(old_token)
            user, _ = self.authenticate(r.data["token"])  # the browser that changed the password stays signed in
            self.assertEqual(user.pk, 1)

    def authenticate(self, token):
        request = APIRequestFactory().get("/admin-api/profile", HTTP_AUTHORIZATION=f"Bearer {token}")
        return admin_auth.AdminTokenAuthentication().authenticate(request)

    def test_otp_and_passwords_never_reach_the_logs(self):
        with self.assertLogs("stokit", level="DEBUG") as logs:
            token = self.verified_change_token()
            otp = last_otp()
            self.post(views.ChangePasswordConfirmView, {"verification_token": token, "new_password": NEW_PASSWORD,
                                                        "confirm_password": NEW_PASSWORD}, self.me)
            logging_marker = logs.output  # at least the 'password changed' line is present
        joined = "\n".join(logging_marker)
        for secret in (otp, NEW_PASSWORD, OLD_PASSWORD, token, "boss@gmail.com"):
            self.assertNotIn(secret, joined)

    def test_ip_level_throttle_applies_to_the_flow(self):
        with override_settings(ADMIN_PASSWORD_FLOW_IP_LIMIT=3):
            codes = [self.post(views.ChangePasswordVerifyCurrentView, {"current_password": "x"}, self.me).status_code for _ in range(4)]
        self.assertEqual(codes, [400, 400, 400, 429])


# ----------------------------------------------------------------------------------------------- forgot password
class ForgotPasswordTests(FlowBase):
    def test_unknown_and_known_emails_get_the_same_generic_response(self):
        unknown = self.post(views.ForgotPasswordSendOtpView, {"email": "nobody@gmail.com"})
        self.assertEqual(mail.outbox, [])
        known = self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual((unknown.status_code, dict(unknown.data)), (known.status_code, dict(known.data)))
        self.assertEqual(unknown.data["message"], flow.FORGOT_GENERIC_MESSAGE)

    def test_invalid_email_syntax_is_rejected(self):
        for value in ("", "not-an-email", "a@b", "x" * 300 + "@gmail.com", ["a@b.co"], None):
            self.assertEqual(self.post(views.ForgotPasswordSendOtpView, {"email": value}).status_code, 400, value)
        self.assertEqual(mail.outbox, [])

    def test_eligible_email_receives_otp_and_email_is_case_insensitive(self):
        self.post(views.ForgotPasswordSendOtpView, {"email": "  Boss@Gmail.COM "})
        self.assertEqual(mail.outbox[0].to, ["boss@gmail.com"])
        self.assertEqual(self.world.challenges.store[0].purpose, "forgot_password")

    def test_account_without_active_role_is_not_eligible(self):
        self.world.roles.filter(principal_type="ADMIN", principal_id=1).update(active=False)
        r = self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        self.assertEqual((r.status_code, mail.outbox), (200, []))

    def test_ambiguous_email_is_not_eligible(self):
        self.world.add_account("DELIVERY_BOY", 3, username="dee", email="BOSS@gmail.com", role="DELIVERY_BOY")
        self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        self.assertEqual(mail.outbox, [])

    def test_unconfigured_email_service_answers_identically_for_every_address(self):
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend"):
            a = self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
            b = self.post(views.ForgotPasswordSendOtpView, {"email": "nobody@gmail.com"})
        self.assertEqual((a.status_code, a.data["code"]), (503, "email_not_configured"))
        self.assertEqual((a.status_code, dict(a.data)), (b.status_code, dict(b.data)))

    def test_smtp_failure_is_logged_not_revealed_and_nothing_claims_success_of_delivery(self):
        with mock.patch.object(flow, "send_mail", side_effect=OSError("boom")):
            r = self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        self.assertEqual((r.status_code, r.data["message"]), (200, flow.FORGOT_GENERIC_MESSAGE))
        self.assertTrue(all(c.invalidated_at for c in self.world.challenges.store))

    def test_cooldown_and_hourly_limits_do_not_leak_eligibility(self):
        self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        again = self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)  # no second e-mail inside the cooldown

    def test_resend_after_cooldown_supersedes_the_old_otp(self):
        self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        first = last_otp()
        self.age_latest_challenge(61)
        self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        second = last_otp()
        self.assertEqual(len(mail.outbox), 2)
        if first != second:
            self.assertEqual(self.post(views.ForgotPasswordVerifyOtpView, {"email": "boss@gmail.com", "otp": first}).status_code, 400)
        self.assertEqual(self.post(views.ForgotPasswordVerifyOtpView, {"email": "boss@gmail.com", "otp": second}).status_code, 200)

    def test_send_requests_are_rate_limited_per_ip(self):
        with override_settings(ADMIN_PASSWORD_FLOW_IP_LIMIT=2):
            codes = [self.post(views.ForgotPasswordSendOtpView, {"email": f"u{i}@gmail.com"}).status_code for i in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_send_requests_are_rate_limited_per_email_without_revealing_it(self):
        for _ in range(12):
            self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"}, ip="10.1.1.%d" % _)
        self.assertLessEqual(len(mail.outbox), 1)
        r = self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"}, ip="10.9.9.9")
        self.assertEqual((r.status_code, r.data["message"]), (200, flow.FORGOT_GENERIC_MESSAGE))

    def test_unknown_email_and_wrong_otp_fail_identically(self):
        self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        real = last_otp()
        wrong = "000000" if real != "000000" else "111111"
        a = self.post(views.ForgotPasswordVerifyOtpView, {"email": "boss@gmail.com", "otp": wrong})
        b = self.post(views.ForgotPasswordVerifyOtpView, {"email": "nobody@gmail.com", "otp": wrong})
        self.assertEqual((a.status_code, dict(a.data)), (b.status_code, dict(b.data)))

    def test_invalid_expired_and_locked_otps_are_rejected(self):
        self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        real = last_otp()
        wrong = "000000" if real != "000000" else "111111"
        for _ in range(5):
            self.assertEqual(self.post(views.ForgotPasswordVerifyOtpView, {"email": "boss@gmail.com", "otp": wrong}).status_code, 400)
        self.assertEqual(self.post(views.ForgotPasswordVerifyOtpView, {"email": "boss@gmail.com", "otp": real}).status_code, 400)
        self.post(views.ForgotPasswordSendOtpView, {"email": "sam@gmail.com"})
        sam_otp = last_otp()
        self.world.challenges.store[-1].expires_at = timezone.now() - timedelta(seconds=1)
        self.assertEqual(self.post(views.ForgotPasswordVerifyOtpView, {"email": "sam@gmail.com", "otp": sam_otp}).status_code, 400)

    def test_otp_for_one_account_cannot_verify_another_account(self):
        self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        otp = last_otp()
        self.assertEqual(self.post(views.ForgotPasswordVerifyOtpView, {"email": "sam@gmail.com", "otp": otp}).status_code, 400)

    def test_password_cannot_be_reset_before_otp_verification(self):
        self.post(views.ForgotPasswordSendOtpView, {"email": "boss@gmail.com"})
        for token in ("", "guess", last_otp()):
            r = self.post(views.ForgotPasswordResetView, {"reset_token": token, "email": "boss@gmail.com", "user_id": 1,
                                                          "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
            self.assertEqual(r.status_code, 400)
        self.assertTrue(verify_php_password(OLD_PASSWORD, self.admin.password))

    def test_reset_validates_match_and_strength_without_consuming_the_token(self):
        token = self.forgot_token()
        bad = [(NEW_PASSWORD, "different-Value-1!", "password_mismatch"), ("12345678", "12345678", "weak_password"), ("", "", "validation_error")]
        for new, confirm, code in bad:
            r = self.post(views.ForgotPasswordResetView, {"reset_token": token, "new_password": new, "confirm_password": confirm})
            self.assertEqual((r.status_code, r.data["code"]), (400, code))
        self.assertEqual(self.post(views.ForgotPasswordResetView, {"reset_token": token, "new_password": NEW_PASSWORD,
                                                                   "confirm_password": NEW_PASSWORD}).status_code, 200)

    def test_successful_reset_changes_password_consumes_challenge_and_does_not_sign_in(self):
        token = self.forgot_token()
        r = self.post(views.ForgotPasswordResetView, {"reset_token": token, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
        self.assertEqual((r.status_code, r.data["message"]), (200, "Password reset successfully."))
        self.assertNotIn("token", r.data)
        self.assertTrue(verify_php_password(NEW_PASSWORD, self.admin.password))
        replay = self.post(views.ForgotPasswordResetView, {"reset_token": token, "new_password": NEW_PASSWORD + "9", "confirm_password": NEW_PASSWORD + "9"})
        self.assertEqual(replay.status_code, 400)
        self.assertTrue(verify_php_password(NEW_PASSWORD, self.admin.password))
        self.assertTrue(all(c.consumed_at or c.invalidated_at for c in self.world.challenges.store))

    def test_reset_token_not_the_request_body_identifies_the_account(self):
        token = self.forgot_token("boss@gmail.com")
        self.post(views.ForgotPasswordResetView, {"reset_token": token, "email": "sam@gmail.com", "username": "sam", "user_id": 7,
                                                  "principal_id": 7, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
        self.assertTrue(verify_php_password(NEW_PASSWORD, self.admin.password))
        self.assertEqual(self.other.password, _OLD_HASH)

    def test_reset_token_expires(self):
        token = self.forgot_token()
        self.world.challenges.store[-1].token_expires_at = timezone.now() - timedelta(seconds=1)
        r = self.post(views.ForgotPasswordResetView, {"reset_token": token, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 400)

    def test_reset_is_refused_if_the_account_was_deactivated_meanwhile(self):
        token = self.forgot_token()
        self.world.roles.filter(principal_type="ADMIN", principal_id=1).update(active=False)
        r = self.post(views.ForgotPasswordResetView, {"reset_token": token, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(verify_php_password(OLD_PASSWORD, self.admin.password))

    def test_reset_revokes_existing_tokens(self):
        with mock.patch("time.time", return_value=1_800_000_000):
            old = flow.issue_admin_token("ADMIN", 1)
            token = self.forgot_token()
        with mock.patch("time.time", return_value=1_800_000_050):
            self.post(views.ForgotPasswordResetView, {"reset_token": token, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
            request = APIRequestFactory().get("/x", HTTP_AUTHORIZATION=f"Bearer {old}")
            with self.assertRaises(AuthenticationFailed):
                admin_auth.AdminTokenAuthentication().authenticate(request)
            fresh = APIRequestFactory().get("/x", HTTP_AUTHORIZATION=f"Bearer {flow.issue_admin_token('ADMIN', 1)}")
            self.assertEqual(admin_auth.AdminTokenAuthentication().authenticate(fresh)[0].pk, 1)

    def test_salesman_and_delivery_accounts_can_reset_too(self):
        token = self.forgot_token("sam@gmail.com")
        self.post(views.ForgotPasswordResetView, {"reset_token": token, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
        self.assertTrue(verify_php_password(NEW_PASSWORD, self.other.password))
        self.assertEqual(self.admin.password, _OLD_HASH)

    def test_malformed_bodies_never_crash(self):
        for view in (views.ForgotPasswordSendOtpView, views.ForgotPasswordVerifyOtpView, views.ForgotPasswordResetView):
            request = APIRequestFactory().post("/x", [1, 2], format="json")
            self.assertEqual(view.as_view()(request).status_code, 400)


# ----------------------------------------------------------------------------------------------- misc
class TokenRevocationAuthTests(FlowBase):
    def auth(self, token):
        request = APIRequestFactory().get("/x", HTTP_AUTHORIZATION=f"Bearer {token}")
        return admin_auth.AdminTokenAuthentication().authenticate(request)

    def test_existing_tokens_keep_working_when_nothing_was_revoked(self):
        self.assertEqual(self.auth(flow.issue_admin_token("ADMIN", 1))[0].role, "SUPER_ADMIN")

    def test_revocation_marker_is_per_principal(self):
        with mock.patch("time.time", return_value=1_800_000_000):
            admin_token, sam_token = flow.issue_admin_token("ADMIN", 1), flow.issue_admin_token("SALESMAN", 7)
        with mock.patch("time.time", return_value=1_800_000_100):
            flow.revoke_tokens("ADMIN", 1)
            with self.assertRaises(AuthenticationFailed):
                self.auth(admin_token)
            self.assertEqual(self.auth(sam_token)[0].pk, 7)

    def test_missing_revocation_table_does_not_lock_everyone_out(self):
        with mock.patch.object(AdminCredentialState, "objects") as objects:
            objects.filter.side_effect = ProgrammingError('relation "accounts_admincredentialstate" does not exist')
            self.assertEqual(self.auth(flow.issue_admin_token("ADMIN", 1))[0].pk, 1)

    def test_deactivated_account_loses_access_through_the_role_check(self):
        self.world.roles.filter(principal_type="ADMIN", principal_id=1).update(active=False)
        user, _ = self.auth(flow.issue_admin_token("ADMIN", 1))
        self.assertIsNone(user.role)  # AdminRolePermission denies every protected view for a role-less principal


class HelperAndMigrationTests(SimpleTestCase):
    def test_mask_email(self):
        self.assertEqual(flow.mask_email("alice@gmail.com"), "a***@gmail.com")
        self.assertEqual(flow.mask_email("bad"), "")

    def test_otp_digests_are_bound_to_purpose_and_principal(self):
        base = flow.otp_digest("change_password", "ADMIN", 1, "123456")
        self.assertNotEqual(base, flow.otp_digest("forgot_password", "ADMIN", 1, "123456"))
        self.assertNotEqual(base, flow.otp_digest("change_password", "ADMIN", 2, "123456"))
        self.assertNotEqual(base, flow.otp_digest("change_password", "SALESMAN", 1, "123456"))
        self.assertNotEqual(flow.token_digest("change_password", "t"), flow.token_digest("forgot_password", "t"))

    def test_configured_password_validators_exist(self):
        from django.conf import settings
        names = " ".join(v["NAME"] for v in settings.AUTH_PASSWORD_VALIDATORS)
        for expected in ('MinimumLength', 'CommonPassword', 'NumericPassword'):
            self.assertIn(expected, names)

    def test_validators_accept_the_real_legacy_models(self):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError
        for model in (AdminAccount, Salesman, DeliveryBoy):
            account = model(username="boss", email="boss@gmail.com", fullname="Boss Admin")
            # Similarity to account attributes is not a password rule; other validators remain active.
            validate_password('boss@gmail.com1', user=account)
            validate_password('Boss Admin 2026', user=account)
            validate_password(NEW_PASSWORD, user=account)  # a strong password passes

    def test_migration_only_creates_the_three_new_tables(self):
        module = importlib.import_module("accounts.migrations.0003_admin_password_flow")
        ops = module.Migration.operations
        self.assertEqual(module.Migration.dependencies, [("accounts", "0002_adminroleassignment_checkoutpayload")])
        self.assertEqual(len(ops), 3)
        self.assertTrue(all(isinstance(op, migrations.CreateModel) for op in ops))
        self.assertEqual({op.name for op in ops}, {"AdminAuthRateEvent", "AdminCredentialState", "AdminOTPChallenge"})

    def test_routes_follow_the_existing_admin_api_conventions(self):
        from django.urls import resolve
        for path in ("profile", "password/change/verify-current", "password/change/send-otp", "password/change/verify-otp",
                     "password/change/confirm", "password/forgot/send-otp", "password/forgot/verify-otp", "password/forgot/reset"):
            self.assertTrue(resolve("/admin-api/" + path).url_name.startswith("admin-"), path)
