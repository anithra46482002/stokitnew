import importlib
from unittest import mock
from django.test import SimpleTestCase, override_settings
from django.urls import resolve

from adminapi import account_identity as identity


class AccountIdentityHelperTests(SimpleTestCase):
    def test_otp_digest_is_bound_to_purpose_principal_target_destination_and_code(self):
        base = identity._otp_hash("change_username", 4, "newadmin", "admin@example.com", "123456")
        self.assertNotEqual(base, identity._otp_hash("change_email_current", 4, "newadmin", "admin@example.com", "123456"))
        self.assertNotEqual(base, identity._otp_hash("change_username", 5, "newadmin", "admin@example.com", "123456"))
        self.assertNotEqual(base, identity._otp_hash("change_username", 4, "othername", "admin@example.com", "123456"))
        self.assertNotEqual(base, identity._otp_hash("change_username", 4, "newadmin", "other@example.com", "123456"))
        self.assertNotEqual(base, identity._otp_hash("change_username", 4, "newadmin", "admin@example.com", "654321"))

    @override_settings(EMAIL_HOST="smtp.example.test", EMAIL_PORT=587, EMAIL_HOST_USER="sender@example.test", EMAIL_HOST_PASSWORD="not-a-real-secret", DEFAULT_FROM_EMAIL="sender@example.test", EMAIL_USE_SSL=False, EMAIL_USE_TLS=True, EMAIL_TIMEOUT=15)
    def test_smtp_uses_starttls_and_never_returns_the_otp(self):
        smtp = mock.MagicMock()
        smtp_factory = mock.MagicMock()
        smtp_factory.return_value.__enter__.return_value = smtp
        with mock.patch.object(identity.smtplib, "SMTP", smtp_factory):
            identity._smtp_send("admin@example.test", "123456", identity.PURPOSE_USERNAME)
        smtp.ehlo.assert_called()
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("sender@example.test", "not-a-real-secret")
        self.assertEqual(smtp.send_message.call_args.args[0]["To"], "admin@example.test")
        self.assertIn("123456", smtp.send_message.call_args.args[0].get_content())

    @override_settings(EMAIL_HOST="", EMAIL_PORT=587, EMAIL_HOST_USER="", EMAIL_HOST_PASSWORD="", DEFAULT_FROM_EMAIL="no-reply@localhost", EMAIL_USE_SSL=False, EMAIL_USE_TLS=True)
    def test_smtp_fails_closed_when_configuration_is_missing(self):
        with self.assertRaises(ValueError):
            identity._smtp_send("admin@example.test", "123456", identity.PURPOSE_USERNAME)

    def test_migration_adds_only_dedicated_account_change_challenge_table(self):
        migration = importlib.import_module("accounts.migrations.0004_admin_account_change_challenge").Migration
        self.assertEqual(migration.dependencies, [("accounts", "0003_admin_password_flow")])
        self.assertEqual(len(migration.operations), 1)
        operation = migration.operations[0]
        self.assertEqual(operation.name, "AdminAccountChangeChallenge")
        self.assertEqual(operation.options["db_table"], "accounts_adminaccountchangechallenge")

    def test_account_identity_routes_are_registered(self):
        paths = [
            "profile/username/send-otp", "profile/username/verify-otp",
            "profile/email/send-current-otp", "profile/email/send-new-otp",
            "profile/email/verify-otp", "profile/email/complete",
        ]
        for path in paths:
            self.assertTrue(resolve("/admin-api/" + path).url_name.startswith("admin-"), path)


class AccountIdentityDeliveryDiagnosticsTests(SimpleTestCase):
    @override_settings(EMAIL_HOST="smtp.example.test", EMAIL_PORT=465, EMAIL_HOST_USER="sender@example.test", EMAIL_HOST_PASSWORD="not-a-real-secret", DEFAULT_FROM_EMAIL="sender@example.test", EMAIL_USE_SSL=True, EMAIL_USE_TLS=False, EMAIL_TIMEOUT=20)
    def test_ssl_mode_uses_implicit_tls_with_verified_context(self):
        smtp = mock.MagicMock()
        factory = mock.MagicMock()
        factory.return_value.__enter__.return_value = smtp
        with mock.patch.object(identity.smtplib, "SMTP_SSL", factory):
            identity._smtp_send("admin@example.test", "123456", identity.PURPOSE_USERNAME)
        args, kwargs = factory.call_args
        self.assertEqual(args, ("smtp.example.test", 465))
        self.assertTrue(kwargs["context"].check_hostname)
        smtp.login.assert_called_once_with("sender@example.test", "not-a-real-secret")
        smtp.send_message.assert_called_once()

    def test_failure_is_reported_truthfully_and_logged_with_stage_but_without_secrets(self):
        error = identity.smtplib.SMTPAuthenticationError(535, b"5.7.8 rejected")
        with mock.patch.object(identity, "_smtp_send", side_effect=error), self.assertLogs(identity.logger, level="ERROR") as captured:
            with self.assertRaises(ValueError) as ctx:
                identity._safe_send("admin@example.test", "654321", identity.PURPOSE_USERNAME)
        logged = "\n".join(captured.output)
        self.assertIn("stage=auth", logged)
        self.assertIn("smtp_code=535", logged)
        self.assertNotIn("654321", logged)
        self.assertNotIn("admin@example.test", logged)
        self.assertIn("could not be sent", str(ctx.exception))

    def test_failure_stage_classification(self):
        stage = identity._failure_stage
        self.assertEqual(stage(ValueError("x")), "config")
        self.assertEqual(stage(identity.ssl.SSLError()), "tls")
        self.assertEqual(stage(identity.smtplib.SMTPRecipientsRefused({})), "send")
        self.assertEqual(stage(TimeoutError()), "connect")
        self.assertEqual(stage(identity.smtplib.SMTPServerDisconnected()), "smtp")
