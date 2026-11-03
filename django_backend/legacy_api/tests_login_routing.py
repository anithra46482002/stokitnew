"""Database-FREE regression tests for POST /api/login routing (customer login HTTP 405 investigation).

The real URL configuration and the real view run through Django's test client; only the SQL helper and the
session creator are replaced. They prove: the exact route /api/login resolves for POST (JSON and form) and
never produces a Django-level 405 for any method; a trailing-slash path is a 404 (APPEND_SLASH is off, so
it is never a 405); and the response contract/other login routes are unchanged. A 405 seen in production
therefore cannot be produced by this Django route and must come from a layer in front of it (see the report).
"""
import json
from unittest import mock

from django.test import Client, SimpleTestCase
from django.urls import resolve

from accounts.hashers import hash_password
from legacy_api import views_auth

PASSWORD = "correct-horse-battery-1"
TOKEN = "b" * 64
STORED = {"id": 7, "username": "alice", "email": "alice@example.invalid", "fullname": "Alice",
          "password": hash_password(PASSWORD)}


class LoginRoutingTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    def _post_json(self, body, path="/api/login", fetch=STORED):
        with mock.patch.object(views_auth, "fetchone", return_value=fetch), \
                mock.patch("accounts.services.create_session", return_value=TOKEN):
            return self.client.post(path, data=json.dumps(body), content_type="application/json")

    def test_route_resolves_to_the_customer_login_view(self):
        match = resolve("/api/login")
        self.assertEqual(match.url_name, "legacy-login")
        self.assertIs(match.func, views_auth.login)

    def test_post_json_login_succeeds_with_the_exact_contract(self):
        response = self._post_json({"username": "alice", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"success", "message", "token", "user"})
        self.assertIs(body["success"], True)
        self.assertEqual(body["token"], TOKEN)
        self.assertEqual(set(body["user"]), {"id", "username", "email", "fullname"})

    def test_post_form_login_succeeds(self):
        with mock.patch.object(views_auth, "fetchone", return_value=STORED), \
                mock.patch("accounts.services.create_session", return_value=TOKEN):
            response = self.client.post("/api/login", {"username": "alice", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["success"], True)

    def test_invalid_credentials_are_rejected_with_the_legacy_failure_shape(self):
        response = self._post_json({"username": "alice", "password": "wrong-password-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": False, "message": "Invalid username or password"})
        self.assertNotIn("token", response.json())

    def test_missing_fields_are_rejected_without_a_database_lookup(self):
        with mock.patch.object(views_auth, "fetchone") as fetchone:
            for body in ({}, {"username": "alice"}, {"password": PASSWORD}):
                with self.subTest(body=body):
                    response = self.client.post("/api/login", data=json.dumps(body), content_type="application/json")
                    self.assertEqual(response.status_code, 200)
                    self.assertIs(response.json()["success"], False)
            fetchone.assert_not_called()

    def test_no_method_is_ever_answered_with_405_by_django(self):
        """Documented legacy (PHP-parity) behaviour: the view is method-agnostic, so Django never returns 405.
        Credentials only travel in the body, so GET/DELETE cannot authenticate anyone."""
        with mock.patch.object(views_auth, "fetchone") as fetchone:
            for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                with self.subTest(method=method):
                    response = self.client.generic(method, "/api/login")
                    self.assertNotEqual(response.status_code, 405)
                    self.assertNotIn("Allow", response.headers)
            fetchone.assert_not_called()

    def test_trailing_slash_is_404_not_405(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                self.assertEqual(self.client.generic(method, "/api/login/").status_code, 404)

    def test_admin_and_delivery_login_routes_are_separate_and_unchanged(self):
        self.assertEqual(resolve("/api/delivery/auth/login/").url_name, "delivery-auth-login")
        self.assertEqual(resolve("/admin-api/delivery/auth/login").url_name, "admin-delivery-auth-login")
        self.assertNotEqual(resolve("/api/login").url_name, resolve("/api/delivery/auth/login/").url_name)
