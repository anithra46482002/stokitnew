from django.test import SimpleTestCase
from django.urls import resolve

from legacy_api import delivery_auth


class DeliveryBoyAuthenticationRouteAliasTests(SimpleTestCase):
    """Both public namespaces must resolve to the same Delivery Boy auth views."""

    ROUTES = (
        ("POST", "/api/delivery/auth/login/", "/admin-api/delivery/auth/login", delivery_auth.DeliveryBoyLoginView),
        ("POST", "/api/delivery/auth/refresh/", "/admin-api/delivery/auth/refresh", delivery_auth.DeliveryBoyRefreshView),
        ("POST", "/api/delivery/auth/logout/", "/admin-api/delivery/auth/logout", delivery_auth.DeliveryBoyLogoutView),
        ("POST", "/api/delivery/auth/forgot-password/request-otp/", "/admin-api/delivery/auth/forgot-password/request-otp", delivery_auth.DeliveryBoyForgotRequestView),
        ("POST", "/api/delivery/auth/forgot-password/verify-otp/", "/admin-api/delivery/auth/forgot-password/verify-otp", delivery_auth.DeliveryBoyForgotVerifyView),
        ("POST", "/api/delivery/auth/forgot-password/reset-password/", "/admin-api/delivery/auth/forgot-password/reset-password", delivery_auth.DeliveryBoyForgotResetView),
        ("POST", "/api/delivery/auth/change-password/request-otp/", "/admin-api/delivery/auth/change-password/request-otp", delivery_auth.DeliveryBoyChangeRequestView),
        ("POST", "/api/delivery/auth/change-password/verify-otp/", "/admin-api/delivery/auth/change-password/verify-otp", delivery_auth.DeliveryBoyChangeVerifyView),
        ("POST", "/api/delivery/auth/change-password/", "/admin-api/delivery/auth/change-password", delivery_auth.DeliveryBoyChangePasswordView),
        ("GET", "/api/delivery/auth/profile/", "/admin-api/delivery/auth/profile", delivery_auth.DeliveryBoyProfileView),
        ("GET", "/api/delivery/auth/sessions/", "/admin-api/delivery/auth/sessions", delivery_auth.DeliveryBoySessionsView),
        ("POST", "/api/delivery/auth/sessions/revoke/", "/admin-api/delivery/auth/sessions/revoke", delivery_auth.DeliveryBoyRevokeSessionView),
        ("GET", "/api/delivery/profile/image/", "/admin-api/delivery/profile/image", delivery_auth.DeliveryBoyProfileImageView),
    )

    def test_both_namespaces_resolve_to_the_same_view_classes(self):
        for method, api_path, admin_path, expected_view in self.ROUTES:
            with self.subTest(method=method, path=api_path):
                api_match = resolve(api_path)
                admin_match = resolve(admin_path)
                self.assertIs(api_match.func.view_class, expected_view)
                self.assertIs(admin_match.func.view_class, expected_view)
                self.assertTrue(hasattr(api_match.func, "view_class"))
                self.assertTrue(hasattr(admin_match.func, "view_class"))

    def test_admin_aliases_expose_the_expected_http_method(self):
        for method, _api_path, admin_path, _expected_view in self.ROUTES:
            with self.subTest(method=method, path=admin_path):
                match = resolve(admin_path)
                self.assertTrue(callable(getattr(match.func.view_class, method.lower(), None)))
