"""Database-free tests for the Super Admin username / email verification flow (account_identity views).

Same approach as tests_profile_password_flow: an in-memory queryset replaces the ORM so the REAL view and challenge
logic runs end to end, and e-mail delivery is mocked (real SMTP delivery is verified separately, outside unit tests).
"""
import re
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import AdminAccountChangeChallenge
from adminapi import account_identity as identity
from adminapi.tests_profile_password_flow import QS, Manager, principal
from legacydb.models import AdminAccount

CURRENT_EMAIL = "boss@gmail.com"
NEW_EMAIL = "newboss@example.com"


class _LockingQS(QS):
    """In-memory queryset that also understands pk__in (used by the Save step's row lock)."""

    def filter(self, **kw):
        pks = kw.pop("pk__in", None)
        rows = QS.filter(self, **kw).rows
        if pks is not None:
            rows = [r for r in rows if r.pk in pks]
        return _LockingQS(self.manager, rows)


class ChallengeManager(Manager):
    def select_for_update(self):
        return _LockingQS(self, self.store)


class IdentityBase(SimpleTestCase):
    def setUp(self):
        self.challenges = ChallengeManager({"attempts": 0, "verified_at": None, "consumed_at": None, "invalidated_at": None, "created_at": timezone.now})
        self.accounts = Manager(does_not_exist=AdminAccount.DoesNotExist)
        self.admin = self.accounts.create(id=1, username="superadmin", email=CURRENT_EMAIL)
        self.accounts.create(id=2, username="taken", email="taken@example.com")
        self.sent = []  # (recipient, otp, purpose) - the mocked mail server
        patchers = [
            mock.patch.object(AdminAccountChangeChallenge, "objects", self.challenges),
            mock.patch.object(AdminAccount, "objects", self.accounts),
            mock.patch.object(identity, "transaction", SimpleNamespace(atomic=lambda: nullcontext())),
            mock.patch.object(identity, "_smtp_send", side_effect=lambda recipient, otp, purpose: self.sent.append((recipient, otp, purpose))),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self.me = principal("ADMIN", 1)

    # -- helpers
    def post(self, view, data, user=None):
        request = APIRequestFactory().post("/admin-api/x", data, format="json")
        force_authenticate(request, user=self.me if user is None else user)
        return view.as_view()(request)

    def last_otp(self):
        return self.sent[-1][1]

    def send_current(self, email=NEW_EMAIL):
        return self.post(identity.EmailCurrentOtpView, {"email": email})

    def send_new(self, email=NEW_EMAIL):
        return self.post(identity.EmailNewOtpView, {"email": email})

    def verify(self, stage, otp, email=NEW_EMAIL):
        return self.post(identity.EmailVerifyView, {"email": email, "otp": otp, "stage": stage})

    def complete(self, email=NEW_EMAIL):
        return self.post(identity.EmailCompleteView, {"email": email})

    def age_cooldown(self):
        for row in self.challenges.store:
            row.created_at -= timedelta(seconds=identity.OTP_RESEND_SECONDS + 1)

    def verify_current(self):
        self.assertEqual(self.send_current().status_code, 200)
        response = self.verify("current", self.last_otp())
        self.assertEqual(response.status_code, 200, response.data)

    def verify_both(self):
        self.verify_current()
        self.assertEqual(self.send_new().status_code, 200)
        response = self.verify("new", self.last_otp())
        self.assertEqual(response.status_code, 200, response.data)


class UsernameFlowTests(IdentityBase):
    def test_code_goes_to_registered_email_and_api_reports_expiry_and_cooldown(self):
        response = self.post(identity.UsernameOtpView, {"username": "newname"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sent[0][0], CURRENT_EMAIL)
        self.assertEqual((response.data["expires_in"], response.data["resend_after"]), (300, 120))
        self.assertNotIn(self.last_otp(), str(response.data))

    def test_valid_code_is_accepted_immediately_and_updates_username_once(self):
        self.post(identity.UsernameOtpView, {"username": "newname"})
        otp = self.last_otp()
        ok = self.post(identity.UsernameVerifyView, {"username": "newname", "otp": otp})
        self.assertEqual((ok.status_code, ok.data["username"]), (200, "newname"))
        self.assertEqual(self.admin.username, "newname")
        replay = self.post(identity.UsernameVerifyView, {"username": "newname", "otp": otp})
        self.assertEqual(replay.status_code, 400)

    def test_wrong_code_does_not_change_the_username(self):
        self.post(identity.UsernameOtpView, {"username": "newname"})
        wrong = "000000" if self.last_otp() != "000000" else "111111"
        self.assertEqual(self.post(identity.UsernameVerifyView, {"username": "newname", "otp": wrong}).status_code, 400)
        self.assertEqual(self.admin.username, "superadmin")

    def test_resend_cooldown_is_enforced_with_retry_after(self):
        self.post(identity.UsernameOtpView, {"username": "newname"})
        again = self.post(identity.UsernameOtpView, {"username": "newname"})
        self.assertEqual((again.status_code, again.data["code"]), (429, "resend_cooldown"))
        self.assertTrue(0 < again.data["retry_after"] <= 120)
        self.assertEqual(len(self.sent), 1)

    def test_code_for_a_different_username_is_rejected(self):
        self.post(identity.UsernameOtpView, {"username": "newname"})
        self.assertEqual(self.post(identity.UsernameVerifyView, {"username": "othername", "otp": self.last_otp()}).status_code, 400)

    def test_failed_delivery_is_reported_truthfully(self):
        with mock.patch.object(identity, "_smtp_send", side_effect=OSError("down")), self.assertLogs(identity.logger, level="ERROR"):
            response = self.post(identity.UsernameOtpView, {"username": "newname"})
        self.assertEqual((response.status_code, response.data["code"], response.data["success"]), (503, "email_delivery_failed", False))
        self.assertTrue(all(c.invalidated_at for c in self.challenges.store))

    def test_expired_username_code_is_rejected_and_the_username_is_unchanged(self):
        self.post(identity.UsernameOtpView, {"username": "newname"})
        for c in self.challenges.store:
            c.expires_at = timezone.now() - timedelta(seconds=1)
        response = self.post(identity.UsernameVerifyView, {"username": "newname", "otp": self.last_otp()})
        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.data["message"])
        self.assertEqual(self.admin.username, "superadmin")

    def test_a_resent_username_code_supersedes_the_old_one_and_the_new_one_works_at_once(self):
        self.post(identity.UsernameOtpView, {"username": "newname"})
        first = self.last_otp()
        self.age_cooldown()
        self.assertEqual(self.post(identity.UsernameOtpView, {"username": "newname"}).status_code, 200)
        second = self.last_otp()
        if first != second:
            self.assertEqual(self.post(identity.UsernameVerifyView, {"username": "newname", "otp": first}).status_code, 400)
        ok = self.post(identity.UsernameVerifyView, {"username": "newname", "otp": second})
        self.assertEqual((ok.status_code, ok.data["username"]), (200, "newname"))

    def test_username_hourly_limit_still_applies(self):
        for _ in range(identity.OTP_MAX_PER_HOUR):
            self.assertEqual(self.post(identity.UsernameOtpView, {"username": "newname"}).status_code, 200)
            self.age_cooldown()
        blocked = self.post(identity.UsernameOtpView, {"username": "newname"})
        self.assertEqual((blocked.status_code, blocked.data["code"]), (429, "rate_limited"))
        self.assertEqual(len(self.sent), identity.OTP_MAX_PER_HOUR)

    def test_only_the_super_admin_may_use_the_flow(self):
        response = self.post(identity.UsernameOtpView, {"username": "newname"}, user=principal("ADMIN", 1, role="MANAGER"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.sent, [])


class EmailFlowTests(IdentityBase):
    def test_current_code_goes_to_current_email_and_new_code_to_the_proposed_email(self):
        current = self.send_current()
        self.assertEqual((current.status_code, self.sent[-1][0], self.sent[-1][2]), (200, CURRENT_EMAIL, identity.PURPOSE_EMAIL_CURRENT))
        self.assertEqual((current.data["expires_in"], current.data["resend_after"]), (300, 120))
        self.verify("current", self.last_otp())
        new = self.send_new()
        self.assertEqual((new.status_code, self.sent[-1][0], self.sent[-1][2]), (200, NEW_EMAIL, identity.PURPOSE_EMAIL_NEW))

    def test_new_email_code_cannot_be_requested_before_current_email_is_verified(self):
        self.send_current()
        response = self.send_new()
        self.assertEqual((response.status_code, response.data["code"]), (409, "current_email_not_verified"))
        self.assertEqual(len(self.sent), 1)

    def test_new_email_code_cannot_be_requested_for_a_different_address_than_the_verified_one(self):
        self.verify_current()
        self.assertEqual(self.send_new("someone.else@example.com").status_code, 409)

    def test_full_flow_updates_the_email_and_consumes_both_challenges(self):
        self.verify_both()
        response = self.complete()
        self.assertEqual((response.status_code, response.data["email"]), (200, NEW_EMAIL))
        self.assertEqual(self.admin.email, NEW_EMAIL)
        self.assertTrue(all(c.consumed_at for c in self.challenges.store if c.verified_at))
        replay = self.complete()
        self.assertEqual(replay.status_code, 400)

    def test_save_rejected_when_nothing_is_verified(self):
        response = self.complete()
        self.assertEqual((response.status_code, response.data["code"]), (400, "verification_required"))
        self.assertEqual(self.admin.email, CURRENT_EMAIL)

    def test_save_rejected_when_only_the_current_email_is_verified(self):
        self.verify_current()
        response = self.complete()
        self.assertEqual((response.status_code, response.data["code"]), (400, "verification_required"))
        self.assertEqual(self.admin.email, CURRENT_EMAIL)

    def test_save_rejected_when_the_new_email_code_was_issued_but_not_verified(self):
        self.verify_current()
        self.send_new()
        response = self.complete()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.admin.email, CURRENT_EMAIL)

    def test_save_rejected_when_the_proposed_email_changes_after_verification(self):
        self.verify_both()
        response = self.complete("changed@example.com")
        self.assertEqual((response.status_code, response.data["code"]), (400, "verification_required"))
        self.assertEqual(self.admin.email, CURRENT_EMAIL)

    def test_requesting_a_code_does_not_mark_anything_verified(self):
        self.send_current()
        self.assertTrue(all(c.verified_at is None for c in self.challenges.store))

    def test_wrong_stage_codes_are_rejected(self):
        self.verify_current()
        self.send_new()
        new_otp = self.last_otp()
        self.assertEqual(self.verify("current", new_otp).status_code, 400)  # new-stage code on the current stage
        self.assertEqual(self.verify("bogus", new_otp).status_code, 400)

    def test_code_for_a_different_proposed_email_is_rejected(self):
        self.send_current()
        self.assertEqual(self.verify("current", self.last_otp(), email="other@example.com").status_code, 400)

    def test_incorrect_code_is_rejected_and_attempts_are_limited(self):
        self.send_current()
        good = self.last_otp()
        wrong = "000000" if good != "000000" else "111111"
        for _ in range(identity.OTP_MAX_ATTEMPTS):
            self.assertEqual(self.verify("current", wrong).status_code, 400)
        self.assertEqual(self.verify("current", good).status_code, 400)  # locked out: needs a newly issued code

    def test_expired_code_is_rejected(self):
        self.send_current()
        for c in self.challenges.store:
            c.expires_at = timezone.now() - timedelta(seconds=1)
        response = self.verify("current", self.last_otp())
        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.data["message"])

    def test_consumed_code_cannot_be_reused(self):
        self.verify_both()
        stale = self.sent[0][1]
        self.complete()
        self.assertEqual(self.verify("current", stale).status_code, 400)

    def test_resend_cooldown_then_new_code_supersedes_the_old_one(self):
        self.send_current()
        old = self.last_otp()
        blocked = self.send_current()
        self.assertEqual((blocked.status_code, blocked.data["code"]), (429, "resend_cooldown"))
        self.age_cooldown()
        self.assertEqual(self.send_current().status_code, 200)
        fresh = self.last_otp()
        if fresh != old:
            self.assertEqual(self.verify("current", old).status_code, 400)
        self.assertEqual(self.verify("current", fresh).status_code, 200)

    def test_verifying_is_allowed_while_the_resend_cooldown_is_still_running(self):
        self.send_current()
        self.assertEqual(self.send_current().status_code, 429)  # cooldown active
        self.assertEqual(self.verify("current", self.last_otp()).status_code, 200)  # entry/verification is not blocked

    def test_hourly_limit_still_applies(self):
        for _ in range(identity.OTP_MAX_PER_HOUR):
            self.assertEqual(self.send_current().status_code, 200)
            self.age_cooldown()
        response = self.send_current()
        self.assertEqual((response.status_code, response.data["code"]), (429, "rate_limited"))

    def test_codes_are_exactly_six_digits_and_leading_zeros_survive_as_text(self):
        with mock.patch.object(identity.secrets, "randbelow", return_value=42):
            self.assertEqual(self.send_current().status_code, 200)
        self.assertEqual(self.last_otp(), "000042")
        self.assertRegex(self.last_otp(), r"^\d{6}$")
        response = self.verify("current", "000042")
        self.assertEqual(response.status_code, 200, response.data)

    def test_codes_that_are_not_six_digits_are_rejected_without_using_an_attempt(self):
        self.send_current()
        good = self.last_otp()
        for bad in ("12345", "1234567", "12345a", "12 345", ""):
            with self.subTest(code=bad):
                response = self.verify("current", bad)
                self.assertEqual(response.status_code, 400)
                self.assertIn("6-digit", response.data["message"])
        self.assertEqual([c.attempts for c in self.challenges.store], [0])
        self.assertEqual(self.verify("current", good).status_code, 200)

    def test_new_email_issue_response_reports_the_same_expiry_and_cooldown(self):
        self.verify_current()
        response = self.send_new()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data["expires_in"], response.data["resend_after"]), (300, 120))

    def test_resend_is_refused_until_the_full_120_seconds_have_passed(self):
        self.assertEqual(self.send_current().status_code, 200)
        for row in self.challenges.store:
            row.created_at -= timedelta(seconds=119)
        early = self.send_current()
        self.assertEqual((early.status_code, early.data["code"], early.data["retry_after"]), (429, "resend_cooldown", 1))
        self.assertEqual(len(self.sent), 1)
        for row in self.challenges.store:
            row.created_at -= timedelta(seconds=1)
        self.assertEqual(self.send_current().status_code, 200)
        self.assertEqual(len(self.sent), 2)

    def test_the_new_email_code_cannot_be_verified_before_the_current_email_is_verified(self):
        self.assertEqual(self.send_current().status_code, 200)
        self.assertEqual(self.send_new().status_code, 409)  # no new-email code exists to verify
        self.assertEqual(self.verify("new", self.last_otp()).status_code, 400)

    def test_verified_stage_outlives_the_original_five_minute_code_expiry(self):
        # Regression for "Verify both the current and new email addresses before saving." after a slow second stage.
        self.send_current()
        for c in self.challenges.store:  # the code is entered 4m50s after it was issued
            c.created_at -= timedelta(seconds=290)
            c.expires_at -= timedelta(seconds=290)
        verified = self.verify("current", self.last_otp())
        self.assertEqual((verified.status_code, verified.data["verified_expires_in"]), (200, identity.VERIFIED_WINDOW_SECONDS))
        challenge = self.challenges.store[0]
        self.assertGreater(challenge.expires_at, timezone.now() + timedelta(seconds=identity.VERIFIED_WINDOW_SECONDS - 5))
        self.send_new()
        self.verify("new", self.last_otp())
        # Time passes beyond the ORIGINAL code expiry (but inside the verified window): save still works.
        for c in self.challenges.store:
            c.created_at -= timedelta(seconds=120)
        self.assertEqual(self.complete().status_code, 200)
        self.assertEqual(self.admin.email, NEW_EMAIL)

    def test_verified_state_expires_after_the_verified_window(self):
        self.verify_both()
        for c in self.challenges.store:
            c.expires_at = timezone.now() - timedelta(seconds=1)
        response = self.complete()
        self.assertEqual((response.status_code, response.data["code"]), (400, "verification_expired"))
        self.assertEqual(self.admin.email, CURRENT_EMAIL)

    def test_invalidated_verification_cannot_be_saved(self):
        self.verify_both()
        self.challenges.store[0].invalidated_at = timezone.now()
        self.assertEqual(self.complete().status_code, 400)
        self.assertEqual(self.admin.email, CURRENT_EMAIL)

    def test_address_already_in_use_is_rejected(self):
        self.assertEqual(self.send_current("taken@example.com").status_code, 409)

    def test_failed_delivery_is_reported_truthfully_and_issues_no_usable_code(self):
        with mock.patch.object(identity, "_smtp_send", side_effect=OSError("down")), self.assertLogs(identity.logger, level="ERROR"):
            response = self.send_current()
        self.assertEqual((response.status_code, response.data["success"]), (503, False))
        self.assertTrue(all(c.invalidated_at for c in self.challenges.store))

    def test_codes_never_appear_in_api_responses(self):
        response = self.send_current()
        self.assertIsNone(re.search(r"\b" + self.last_otp() + r"\b", str(response.data)))

class ProtectionConstantsTests(SimpleTestCase):
    """The security limits that the UI and the protocol rely on, pinned so a change is always deliberate."""

    def test_limits(self):
        self.assertEqual(identity.OTP_TTL_SECONDS, 300)
        self.assertEqual(identity.OTP_RESEND_SECONDS, 120)
        self.assertEqual(identity.OTP_MAX_PER_HOUR, 5)
        self.assertEqual(identity.OTP_MAX_ATTEMPTS, 5)
        self.assertEqual(identity.VERIFIED_WINDOW_SECONDS, 600)
