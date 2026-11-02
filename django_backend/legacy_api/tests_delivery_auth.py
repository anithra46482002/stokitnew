"""Focused database-free security tests for Delivery Boy authentication."""
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
import uuid
from unittest import mock

from django.core import signing
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from accounts.models import AdminOTPChallenge, DeliveryBoyAuthSession
from adminapi.models import DeliveryBoyAdminMetadata
from legacy_api import delivery_auth as auth
from legacydb.models import DeliveryBoy


class Query:
    def __init__(self, rows): self.rows = list(rows)
    @staticmethod
    def _matches(row, criteria):
        for key, expected in criteria.items():
            field, _, op = key.partition("__")
            actual = getattr(row, field, None)
            if op == "isnull":
                if (actual is None) != expected: return False
            elif op == "gt":
                if actual is None or actual <= expected: return False
            elif actual != expected:
                return False
        return True
    def filter(self, **criteria): return Query([r for r in self.rows if self._matches(r, criteria)])
    def exclude(self, **criteria): return Query([r for r in self.rows if not self._matches(r, criteria)])
    def first(self): return self.rows[0] if self.rows else None
    def __getitem__(self, item): return self.rows[item]
    def __iter__(self): return iter(self.rows)
    def __len__(self): return len(self.rows)
    def count(self): return len(self.rows)
    def select_for_update(self): return self
    def order_by(self, field):
        key = field.lstrip("-")
        self.rows.sort(key=lambda r: getattr(r, key), reverse=field.startswith("-"))
        return self
    def update(self, **values):
        for row in self.rows:
            for key, value in values.items(): setattr(row, key, value)
        return len(self.rows)


class SessionStore:
    def __init__(self): self.rows = []; self.next_id = 1
    def select_for_update(self): return self
    def filter(self, **criteria): return Query(self.rows).filter(**criteria)
    def create(self, **values):
        values.setdefault("pk", self.next_id)
        values.setdefault("id", self.next_id)
        self.next_id += 1
        row = SimpleNamespace(**values, save=lambda update_fields=None: None)
        self.rows.append(row)
        return row


class DeliveryTokenTests(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(pk=71, username="driver71", email="driver71@example.test",
            fullname="Driver 71", phone="9876543210", address="Kochi", password="unused")
        self.metadata = SimpleNamespace(is_active=True, joining_date=None, profile_image=None, profile_image_mime="")
        self.accounts = {71: self.account}
        self.sessions = SessionStore()
        self.factory = APIRequestFactory()
        patches = [
            mock.patch.object(DeliveryBoyAuthSession, "objects", self.sessions),
            mock.patch.object(auth, "active_delivery_boy", side_effect=lambda pk: (self.accounts[pk], self.metadata) if pk in self.accounts else None),
            mock.patch.object(auth, "transaction", SimpleNamespace(atomic=lambda: nullcontext())),
        ]
        for patcher in patches: patcher.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])

    def test_delivery_token_lifetimes_are_30_minutes_and_7_days(self):
        self.assertEqual(auth.settings.DELIVERY_ACCESS_TOKEN_TTL_SECONDS, 1800)
        self.assertEqual(auth.settings.DELIVERY_REFRESH_TOKEN_TTL_SECONDS, 604800)
        _access, _refresh, session_id = self.login()
        session = next(row for row in self.sessions.rows if row.session_id == session_id)
        self.assertEqual((session.access_expires_at - session.created_at).total_seconds(), 1800)
        self.assertEqual((session.refresh_expires_at - session.created_at).total_seconds(), 604800)

    def login(self, account=None, device="Android"):
        account = account or self.account
        self.accounts[account.pk] = account
        request = SimpleNamespace(META={"REMOTE_ADDR": "127.0.0.1", "HTTP_USER_AGENT": "test-agent"})
        data = {"device_name": device, "device_platform": device}
        return auth._create_session(account, request, data)

    def validate_access(self, token):
        return auth._validate_token(token, "access", auth.settings.DELIVERY_ACCESS_TOKEN_TTL_SECONDS)

    def test_login_endpoint_creates_a_persisted_device_session(self):
        delivery_manager = SimpleNamespace(
            select_for_update=lambda: delivery_manager,
            filter=lambda *args, **kwargs: Query([self.account]),
        )
        patches = [
            mock.patch.object(DeliveryBoy, "objects", delivery_manager),
            mock.patch.object(auth, "verify_php_password", return_value=True),
            mock.patch.object(auth, "_check_ip"),
        ]
        for patcher in patches: patcher.start()
        try:
            request = self.factory.post("/api/delivery/auth/login/",
                {"username": "driver71", "password": "not-logged", "device_name": "Pixel 9",
                 "device_platform": "Android", "device_identifier": "android-install-1"}, format="json")
            response = auth.DeliveryBoyLoginView.as_view()(request)
            request_b = self.factory.post("/api/delivery/auth/login/",
                {"username": "driver71", "password": "not-logged", "device_name": "iPhone 16",
                 "device_platform": "iOS", "device_identifier": "ios-install-2"}, format="json")
            response_b = auth.DeliveryBoyLoginView.as_view()(request_b)
        finally:
            for patcher in reversed(patches): patcher.stop()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["expires_in"], 1800)
        self.assertEqual(response.data["refresh_expires_in"], 604800)
        self.assertEqual(response_b.status_code, 200)
        self.assertNotEqual(response.data["session_id"], response_b.data["session_id"])
        self.assertEqual(len(self.sessions.rows), 2)
        row = self.sessions.rows[0]
        self.assertEqual(row.device_name, "Pixel 9")
        self.assertEqual(row.device_platform, "Android")
        self.assertEqual(row.device_identifier, "android-install-1")
        self.assertEqual(row.refresh_token_hash, auth._token_hash(response.data["refresh"]))
        self.assertNotIn(response.data["refresh"], row.__dict__.values())

    def test_same_delivery_boy_can_login_on_three_devices_independently(self):
        a = self.login(device="Android 1")
        b = self.login(device="Android 2")
        c = self.login(device="iPhone")
        self.assertEqual(len({str(a[2]), str(b[2]), str(c[2])}), 3)
        self.assertNotEqual(a[0], b[0])
        self.assertNotEqual(a[1], b[1])
        for access, _refresh, _sid in (a, b, c):
            account, session, payload = self.validate_access(access)
            self.assertEqual(account.pk, 71)
            self.assertEqual(session.delivery_boy_id, 71)
            self.assertEqual(payload["jti"], str(session.session_id))

    def test_logout_revokes_only_current_device_session(self):
        a = self.login(device="Android A")
        b = self.login(device="iPhone B")
        request = self.factory.post("/api/delivery/auth/logout/", {}, format="json",
            HTTP_AUTHORIZATION=f"Bearer {a[0]}")
        response = auth.DeliveryBoyLogoutView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        with self.assertRaises(AuthenticationFailed): self.validate_access(a[0])
        with self.assertRaises(AuthenticationFailed): auth._validate_token(a[1], "refresh", auth.settings.DELIVERY_REFRESH_TOKEN_TTL_SECONDS)
        self.assertEqual(self.validate_access(b[0])[0].pk, 71)
        self.assertEqual(auth._validate_token(b[1], "refresh", auth.settings.DELIVERY_REFRESH_TOKEN_TTL_SECONDS)[0].pk, 71)
        self.assertFalse(next(row for row in self.sessions.rows if row.session_id == a[2]).is_active)
        self.assertTrue(next(row for row in self.sessions.rows if row.session_id == b[2]).is_active)

    def test_refresh_rotation_is_independent_per_device(self):
        a = self.login(device="Android A")
        b = self.login(device="iPhone B")
        session_a = next(row for row in self.sessions.rows if row.session_id == a[2])
        original_refresh_expiry = session_a.refresh_expires_at
        request = self.factory.post("/api/delivery/auth/refresh/", {"refresh": a[1]}, format="json")
        response = auth.DeliveryBoyRefreshView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["session_id"], str(a[2]))
        self.assertEqual(response.data["expires_in"], 1800)
        self.assertEqual(session_a.refresh_expires_at, original_refresh_expiry)
        self.assertAlmostEqual((session_a.access_expires_at - timezone.now()).total_seconds(), 1800, delta=1)
        rotated_a = response.data["refresh"]
        self.assertNotEqual(rotated_a, a[1])
        with self.assertRaises(AuthenticationFailed):
            auth._validate_token(a[1], "refresh", auth.settings.DELIVERY_REFRESH_TOKEN_TTL_SECONDS)
        self.assertEqual(auth._validate_token(rotated_a, "refresh", auth.settings.DELIVERY_REFRESH_TOKEN_TTL_SECONDS)[0].pk, 71)
        self.assertEqual(auth._validate_token(b[1], "refresh", auth.settings.DELIVERY_REFRESH_TOKEN_TTL_SECONDS)[0].pk, 71)

    def test_expired_access_and_refresh_tokens_are_rejected(self):
        access, refresh, sid = self.login()
        session = next(row for row in self.sessions.rows if row.session_id == sid)
        session.access_expires_at = timezone.now() - timedelta(seconds=1)
        with self.assertRaises(AuthenticationFailed): self.validate_access(access)
        session.refresh_expires_at = timezone.now() - timedelta(seconds=1)
        with self.assertRaises(AuthenticationFailed): auth._validate_token(refresh, "refresh", 2592000)

    def test_invalid_or_missing_session_jti_is_rejected(self):
        token = signing.dumps({"principal_type": "DELIVERY_BOY", "role": "DELIVERY_BOY",
            "delivery_boy_id": 71, "token_type": "access", "jti": str(uuid.uuid4()),
            "iat": int(timezone.now().timestamp()), "exp": int((timezone.now()+timedelta(hours=1)).timestamp())}, salt=auth.TOKEN_SALT)
        with self.assertRaises(AuthenticationFailed): self.validate_access(token)

    def test_inactive_delivery_boy_cannot_authenticate(self):
        access, _refresh, _sid = self.login()
        with mock.patch.object(auth, "active_delivery_boy", return_value=None):
            with self.assertRaises(AuthenticationFailed): self.validate_access(access)

    def test_admin_and_customer_tokens_are_rejected(self):
        tokens = (signing.dumps({"admin_id": 9}, salt="stokit.admin-token"), "customer-session-token")
        for token in tokens:
            request = self.factory.get("/api/delivery/auth/profile/", HTTP_AUTHORIZATION=f"Bearer {token}")
            with self.assertRaises(AuthenticationFailed): auth.DeliveryBoyTokenAuthentication().authenticate(request)

    def test_wrong_token_type_is_rejected(self):
        _access, refresh, _sid = self.login()
        with self.assertRaises(AuthenticationFailed): self.validate_access(refresh)

    def test_session_list_contains_only_authenticated_delivery_boy_sessions(self):
        first = self.login(device="Android")
        self.login(device="iPhone")
        other = SimpleNamespace(pk=72, username="driver72", email="driver72@example.test")
        self.login(account=other, device="Other account")
        request = self.factory.get("/api/delivery/auth/sessions/", HTTP_AUTHORIZATION=f"Bearer {first[0]}")
        response = auth.DeliveryBoySessionsView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["sessions"]), 2)
        self.assertTrue(all(row["session_id"] in {str(first[2]), str(self.sessions.rows[1].session_id)}
            for row in response.data["sessions"]))

    def test_delivery_boy_cannot_revoke_another_accounts_session(self):
        own = self.login(device="Android")
        other = SimpleNamespace(pk=72, username="driver72", email="driver72@example.test")
        other_session = self.login(account=other, device="Other account")
        request = self.factory.post("/api/delivery/auth/sessions/revoke/", {"session_id": str(other_session[2])},
            format="json", HTTP_AUTHORIZATION=f"Bearer {own[0]}")
        response = auth.DeliveryBoyRevokeSessionView.as_view()(request)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(next(row for row in self.sessions.rows if row.session_id == other_session[2]).is_active)

    def test_delivery_boy_can_revoke_one_of_their_other_sessions(self):
        current = self.login(device="Android A")
        target = self.login(device="iPhone B")
        request = self.factory.post("/api/delivery/auth/sessions/revoke/",
            {"session_id": str(target[2])}, format="json",
            HTTP_AUTHORIZATION=f"Bearer {current[0]}")
        response = auth.DeliveryBoyRevokeSessionView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.validate_access(current[0])[0].pk, 71)
        with self.assertRaises(AuthenticationFailed): self.validate_access(target[0])

    def _complete_password_and_assert_all_sessions_revoked(self, purpose):
        own_a = self.login(device="Android A")
        own_b = self.login(device="iPhone B")
        other = SimpleNamespace(pk=72, username="driver72", email="driver72@example.test")
        other_session = self.login(account=other, device="Other account")
        challenge = SimpleNamespace(pk=1, principal_type="DELIVERY_BOY", principal_id=71, purpose=purpose,
            token_hash=auth.shared_flow.token_digest(purpose, "verification-token"),
            verified_at=timezone.now(), token_expires_at=timezone.now()+timedelta(minutes=5),
            invalidated_at=None, consumed_at=None)
        manager = ChallengeManager([challenge])
        delivery_manager = SimpleNamespace(
            select_for_update=lambda: SimpleNamespace(filter=lambda **kw: Query([self.account])),
            filter=lambda **kw: Query([self.account]))
        patches = [
            mock.patch.object(AdminOTPChallenge, "objects", manager),
            mock.patch.object(DeliveryBoy, "objects", delivery_manager),
            mock.patch.object(auth, "hash_password", return_value="new-secure-hash"),
            mock.patch.object(auth.shared_flow, "validate_new_password"),
        ]
        for patcher in patches: patcher.start()
        try:
            auth._complete_password(purpose, "verification-token", "Strong-New-Pass#123", "Strong-New-Pass#123",
                delivery_boy_id=71 if purpose == auth.CHANGE else None)
        finally:
            for patcher in reversed(patches): patcher.stop()
        for access, _refresh, _sid in (own_a, own_b):
            with self.assertRaises(AuthenticationFailed): self.validate_access(access)
        self.assertEqual(self.validate_access(other_session[0])[0].pk, 72)

    def test_password_change_revokes_all_sessions_but_not_other_accounts(self):
        self._complete_password_and_assert_all_sessions_revoked(auth.CHANGE)

    def test_password_reset_revokes_all_sessions_but_not_other_accounts(self):
        self._complete_password_and_assert_all_sessions_revoked(auth.FORGOT)

class ChallengeQuery:
    def __init__(self, rows): self.rows = list(rows)
    @staticmethod
    def _matches(row, criteria):
        for key, expected in criteria.items():
            field, _, op = key.partition("__")
            actual = getattr(row, field, None)
            if op == "isnull":
                if (actual is None) != expected: return False
            elif op == "gte":
                if actual is None or actual < expected: return False
            elif actual != expected:
                return False
        return True
    def filter(self, **criteria):
        return ChallengeQuery([row for row in self.rows if self._matches(row, criteria)])
    def exclude(self, **criteria):
        return ChallengeQuery([row for row in self.rows if not self._matches(row, criteria)])
    def select_for_update(self): return self
    def order_by(self, field):
        key = field.lstrip("-")
        self.rows.sort(key=lambda row: getattr(row, key), reverse=field.startswith("-"))
        return self
    def first(self): return self.rows[0] if self.rows else None
    def count(self): return len(self.rows)
    def update(self, **values):
        for row in self.rows:
            for key, value in values.items(): setattr(row, key, value)
        return len(self.rows)


class ChallengeManager:
    def __init__(self, rows=None): self.rows = list(rows or []); self.next_id = 1
    def filter(self, **criteria): return ChallengeQuery(self.rows).filter(**criteria)
    def create(self, **values):
        values.setdefault("id", self.next_id)
        values.setdefault("pk", values["id"])
        self.next_id += 1
        values.setdefault("attempts", 0)
        values.setdefault("verified_at", None)
        values.setdefault("token_hash", "")
        values.setdefault("token_expires_at", None)
        values.setdefault("invalidated_at", None)
        values.setdefault("consumed_at", None)
        values.setdefault("save", lambda update_fields=None: None)
        row = SimpleNamespace(**values)
        self.rows.append(row)
        return row


class IssueOtpTests(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(pk=71, email="driver71@example.test")
        self.metadata = SimpleNamespace(is_active=True)
        self.challenge_manager = ChallengeManager()
        delivery_manager = SimpleNamespace(select_for_update=lambda: SimpleNamespace(
            filter=lambda **kw: Query([self.account])))
        patches = [
            mock.patch.object(auth, "transaction", SimpleNamespace(atomic=lambda: nullcontext())),
            mock.patch.object(DeliveryBoy, "objects", delivery_manager),
            mock.patch.object(auth, "active_delivery_boy", return_value=(self.account, self.metadata)),
            mock.patch.object(auth.shared_flow, "ensure_email_configured"),
            mock.patch.object(AdminOTPChallenge, "objects", self.challenge_manager),
            mock.patch.object(auth, "send_mail", return_value=1),
        ]
        for patcher in patches: patcher.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])

    def test_issue_otp_stores_hmac_and_sends_to_registered_email(self):
        with mock.patch.object(auth.secrets, "randbelow", return_value=123456):
            challenge = auth._issue_otp(self.account, auth.FORGOT)
        self.assertEqual(challenge.purpose, auth.FORGOT)
        self.assertEqual(challenge.otp_hash, auth.shared_flow.otp_digest(auth.FORGOT, "DELIVERY_BOY", 71, "123456"))
        self.assertNotEqual(challenge.otp_hash, "123456")
        self.assertEqual(challenge.expires_at - challenge.created_at, timedelta(seconds=300))
        auth.send_mail.assert_called_once()
        args = auth.send_mail.call_args.args
        self.assertEqual(args[0], "StokIt Delivery Boy verification code")
        self.assertEqual(args[3], [self.account.email])
        self.assertIn("123456", args[1])
        self.assertIn("Never share", args[1])

    def test_resend_cooldown_is_120_seconds(self):
        self.challenge_manager.create(principal_type="DELIVERY_BOY", principal_id=71, purpose=auth.FORGOT,
            otp_hash="digest", created_at=timezone.now() - timedelta(seconds=30),
            expires_at=timezone.now() + timedelta(minutes=4))
        with self.assertRaises(auth.DeliveryAuthError) as caught:
            auth._issue_otp(self.account, auth.FORGOT)
        self.assertEqual(caught.exception.status, 429)
        auth.send_mail.assert_not_called()

    def test_smtp_failure_invalidates_challenge_and_returns_safe_error(self):
        auth.send_mail.side_effect = OSError("smtp unavailable")
        with self.assertRaises(auth.DeliveryAuthError) as caught:
            auth._issue_otp(self.account, auth.CHANGE)
        self.assertEqual(caught.exception.code, "email_delivery_failed")
        self.assertTrue(self.challenge_manager.rows[0].invalidated_at)


class ChallengeVerificationTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch.object(auth, "transaction", SimpleNamespace(atomic=lambda: nullcontext()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _challenge(self, purpose, otp, *, expires=None, attempts=0):
        now = timezone.now()
        return SimpleNamespace(principal_type="DELIVERY_BOY", principal_id=71, purpose=purpose,
            otp_hash=auth.shared_flow.otp_digest(purpose, "DELIVERY_BOY", 71, otp),
            created_at=now, expires_at=expires or now + timedelta(minutes=5), attempts=attempts,
            verified_at=None, token_hash="", token_expires_at=None, invalidated_at=None,
            consumed_at=None, save=lambda update_fields=None: None)

    @override_settings(DELIVERY_OTP_MAX_ATTEMPTS=5, DELIVERY_PASSWORD_TOKEN_TTL_SECONDS=600)
    def test_correct_otp_returns_single_use_token_and_stores_only_hash(self):
        row = self._challenge(auth.FORGOT, "123456")
        manager = SimpleNamespace(filter=lambda **kw: ChallengeQuery([row]).filter(**kw))
        with mock.patch.object(AdminOTPChallenge, "objects", manager):
            token = auth._verify_otp(71, auth.FORGOT, "123456")
            with self.assertRaises(auth.DeliveryAuthError):
                auth._verify_otp(71, auth.FORGOT, "123456")
        self.assertTrue(token)
        self.assertIsNotNone(row.verified_at)
        self.assertEqual(row.token_hash, auth.shared_flow.token_digest(auth.FORGOT, token))
        self.assertNotEqual(row.otp_hash, "123456")

    @override_settings(DELIVERY_OTP_MAX_ATTEMPTS=2)
    def test_wrong_otp_increments_attempts_and_locks_after_limit(self):
        row = self._challenge(auth.FORGOT, "123456")
        manager = SimpleNamespace(filter=lambda **kw: ChallengeQuery([row]).filter(**kw))
        with mock.patch.object(AdminOTPChallenge, "objects", manager):
            for _ in range(2):
                with self.assertRaises(auth.DeliveryAuthError):
                    auth._verify_otp(71, auth.FORGOT, "000000")
        self.assertEqual(row.attempts, 2)
        self.assertIsNotNone(row.invalidated_at)

    def test_expired_and_wrong_purpose_challenges_are_rejected(self):
        expired = self._challenge(auth.FORGOT, "123456", expires=timezone.now() - timedelta(seconds=1))
        manager = SimpleNamespace(filter=lambda **kw: ChallengeQuery([expired]).filter(**kw))
        with mock.patch.object(AdminOTPChallenge, "objects", manager):
            with self.assertRaises(auth.DeliveryAuthError):
                auth._verify_otp(71, auth.FORGOT, "123456")
            with self.assertRaises(auth.DeliveryAuthError):
                auth._verify_otp(71, auth.CHANGE, "123456")
            with self.assertRaises(auth.DeliveryAuthError):
                auth._verify_otp(71, AdminOTPChallenge.PURPOSE_FORGOT, "123456")

    def test_otp_must_be_exactly_six_digits(self):
        for value in ("", "12345", "1234567", "12a456"):
            with self.assertRaises(auth.DeliveryAuthError):
                auth._verify_otp(71, auth.FORGOT, value)
