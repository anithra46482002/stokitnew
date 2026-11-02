"""Tests for serving the built React admin app through Django (config/spa.py). No database is touched."""
import shutil
import sys
import tempfile
import types
from pathlib import Path

from django.test import SimpleTestCase, override_settings
from django.urls import resolve

from config import urls as project_urls
from config.spa import spa_urlpatterns

INDEX = '<!doctype html><html><body><div id="root"></div><script type="module" src="/assets/app-abc123.js"></script></body></html>'


def _make_urlconf(name, patterns):
    module = types.ModuleType(name)
    module.urlpatterns = patterns
    sys.modules[name] = module
    return name


# The project's own routes without any SPA route (so the result does not depend on the SERVE_ADMIN_FRONTEND env flag).
BASE_PATTERNS = [p for p in project_urls.urlpatterns if not str(getattr(p, "name", "") or "").startswith("admin-spa-")]
WITH_SPA = _make_urlconf("config._tests_spa_on", BASE_PATTERNS + spa_urlpatterns())
WITHOUT_SPA = _make_urlconf("config._tests_spa_off", list(BASE_PATTERNS))


class ServeAdminFrontendTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = Path(tempfile.mkdtemp(prefix="spa_dist_"))
        cls.dist = cls.tmp / "dist"
        (cls.dist / "assets").mkdir(parents=True)
        (cls.dist / "index.html").write_text(INDEX, encoding="utf-8")
        (cls.dist / "assets" / "app-abc123.js").write_text("console.log('app');", encoding="utf-8")
        (cls.dist / "assets" / "app-abc123.css").write_text("body{margin:0}", encoding="utf-8")
        (cls.dist / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        (cls.tmp / "secret.txt").write_text("TOP SECRET", encoding="utf-8")  # outside the build folder
        cls.override = override_settings(ROOT_URLCONF=WITH_SPA, ADMIN_FRONTEND_DIST=cls.dist)
        cls.override.enable()

    @classmethod
    def tearDownClass(cls):
        cls.override.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def body(self, response):
        return b"".join(response.streaming_content) if response.streaming else response.content

    def test_homepage_serves_the_react_index(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/html"))
        self.assertIn(b'<div id="root">', self.body(response))
        self.assertEqual(response["Cache-Control"], "no-cache")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_react_routes_return_the_app_on_refresh(self):
        for path in ("/orders/12", "/login", "/advance-payments/new", "/customers", "/orders"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(b'<div id="root">', self.body(response), path)

    def test_assets_have_correct_types_and_cache_headers(self):
        js = self.client.get("/assets/app-abc123.js")
        self.assertEqual((js.status_code, js["Content-Type"].split(";")[0]), (200, "text/javascript"))
        self.assertIn("immutable", js["Cache-Control"])
        css = self.client.get("/assets/app-abc123.css")
        self.assertEqual((css.status_code, css["Content-Type"].split(";")[0]), (200, "text/css"))
        icon = self.client.get("/favicon.svg")
        self.assertEqual((icon.status_code, icon["Content-Type"].split(";")[0]), (200, "image/svg+xml"))

    def test_missing_asset_is_404_not_the_react_page(self):
        for path in ("/assets/missing.js", "/assets/", "/nope.svg"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)
            self.assertNotIn(b'id="root"', self.body(response), path)

    def test_path_traversal_cannot_leave_the_build_folder(self):
        for path in ("/assets/../../secret.txt", "/assets/..%2f..%2fsecret.txt", "/assets/..\\..\\secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt"):
            response = self.client.get(path)
            self.assertNotEqual(response.status_code, 200, path)
            self.assertNotIn(b"TOP SECRET", self.body(response), path)

    def test_django_prefixes_are_never_intercepted(self):
        for path in ("/api/nope", "/api", "/admin-api/nope", "/admin-api", "/admin", "/admin/", "/admin/manage/other", "/admin/manage/uploads/missing.png"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)
            self.assertNotIn(b'id="root"', self.body(response), path)

    def test_existing_routes_still_resolve_to_their_views(self):
        from adminapi import order_pages
        from legacy_api import views_commerce, views_orders
        self.assertIs(resolve("/admin-api/orders/manage").func.view_class, order_pages.OrderManageView)
        self.assertEqual(resolve("/admin-api/orders/12/detail").func.view_class, order_pages.OrderDetailView)
        self.assertIs(resolve("/api/order_post").func, views_orders.order_post)
        self.assertIs(resolve("/api/cart_api_test").func, views_commerce.cart_api_test)
        self.assertEqual(resolve("/orders/12").url_name, "admin-spa-index")
        self.assertEqual(resolve("/admin/manage/uploads/a.png").url_name, "local-upload-media")

    def test_only_safe_methods_are_allowed(self):
        self.assertEqual(self.client.post("/orders/12", {}).status_code, 405)
        self.assertEqual(self.client.head("/").status_code, 200)

    def test_missing_build_returns_404(self):
        empty = Path(tempfile.mkdtemp(prefix="spa_empty_"))
        try:
            with override_settings(ADMIN_FRONTEND_DIST=empty):
                self.assertEqual(self.client.get("/").status_code, 404)
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class FlagOffTests(SimpleTestCase):
    def test_without_the_spa_routes_nothing_is_served_at_the_root(self):
        with override_settings(ROOT_URLCONF=WITHOUT_SPA):
            self.assertEqual(self.client.get("/").status_code, 404)
            self.assertEqual(self.client.get("/orders/12").status_code, 404)
