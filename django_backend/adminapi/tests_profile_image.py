"""Database-free tests for additive Super Admin profile-image persistence."""
import importlib
from io import BytesIO
from PIL import Image
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import ProgrammingError
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from legacydb.models import AdminAccount
from adminapi.profile_image import AdminProfileImageView
from adminapi.profile import AdminProfileView
from adminapi import profile_image_storage as storage
from adminapi.profile_image_storage import get_admin_profile_image_path, save_admin_profile_image_path

buffer = BytesIO()
Image.new("RGB", (1, 1), (0, 128, 0)).save(buffer, format="PNG")
PNG_BYTES = buffer.getvalue()


class FakeAccount:
    def __init__(self, pk=10, profile_image=""):
        self.pk = pk
        self.profile_image = profile_image
        self.saved_fields = None

    def save(self, update_fields=None):
        self.saved_fields = update_fields


class ProfileImageStorageTests(SimpleTestCase):
    def test_read_returns_saved_path(self):
        cursor = mock.MagicMock()
        cursor.fetchone.return_value = ("uploads/admin_profiles/avatar.png",)
        context = mock.MagicMock()
        context.__enter__.return_value = cursor
        with mock.patch.object(storage, "connection", SimpleNamespace(cursor=mock.Mock(return_value=context))):
            self.assertEqual(get_admin_profile_image_path("ADMIN", 10), "uploads/admin_profiles/avatar.png")
        cursor.execute.assert_called_once_with('SELECT "profile_image" FROM "admins" WHERE "id" = %s', [10])

    def test_missing_unapplied_column_does_not_break_existing_profile_reads(self):
        cursor = mock.MagicMock()
        cursor.execute.side_effect = ProgrammingError('column "profile_image" does not exist')
        context = mock.MagicMock()
        context.__enter__.return_value = cursor
        with mock.patch.object(storage, "connection", SimpleNamespace(cursor=mock.Mock(return_value=context))):
            self.assertEqual(get_admin_profile_image_path("ADMIN", 10), "")

    def test_profile_endpoint_returns_url_for_saved_admin_image(self):
        user = SimpleNamespace(principal_type="ADMIN", pk=10, role="SUPER_ADMIN", is_authenticated=True)
        account = SimpleNamespace(pk=10, fullname="Example Admin", username="example", email="admin@example.test", last_login=None)
        request = APIRequestFactory().get("/admin-api/profile")
        force_authenticate(request, user=user)
        with mock.patch("adminapi.profile.flow.load_account", return_value=account), \
             mock.patch("adminapi.profile.flow.role_assignment", return_value=SimpleNamespace(active=True)), \
             mock.patch("adminapi.profile.get_admin_profile_image_path", return_value="uploads/admin_profiles/avatar.png"), \
             mock.patch("adminapi.profile.image_url", return_value="https://example.test/admin/manage/uploads/admin_profiles/avatar.png"):
            response = AdminProfileView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["profile"]["profile_image_url"], "https://example.test/admin/manage/uploads/admin_profiles/avatar.png")
    def test_save_updates_only_the_authenticated_admin_row(self):
        cursor = mock.MagicMock()
        cursor.rowcount = 1
        context = mock.MagicMock()
        context.__enter__.return_value = cursor
        with mock.patch.object(storage, "connection", SimpleNamespace(cursor=mock.Mock(return_value=context))):
            self.assertTrue(save_admin_profile_image_path(10, "uploads/admin_profiles/avatar.png"))
        cursor.execute.assert_called_once_with(
            'UPDATE "admins" SET "profile_image" = %s WHERE "id" = %s',
            ["uploads/admin_profiles/avatar.png", 10],
        )


class ProfileImageUploadTests(SimpleTestCase):
    def request(self, *, user=None, file=None):
        data = {"file": file} if file is not None else {}
        request = APIRequestFactory().post("/admin-api/profile/image", data, format="multipart")
        if user is not None:
            force_authenticate(request, user=user)
        return AdminProfileImageView.as_view()(request)

    @staticmethod
    def super_admin():
        return SimpleNamespace(principal_type="ADMIN", pk=10, role="SUPER_ADMIN", is_authenticated=True)

    def test_super_admin_image_is_saved_to_media_and_database_path(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root), SERVE_LOCAL_UPLOADS=True):
            account = FakeAccount()
            with mock.patch.object(AdminAccount.objects, "filter", return_value=SimpleNamespace(first=lambda: account)), \
                 mock.patch("adminapi.profile_image.save_admin_profile_image_path", side_effect=lambda pk, path: (setattr(account, "profile_image", path) or True)):
                response = self.request(user=self.super_admin(), file=SimpleUploadedFile("avatar.png", PNG_BYTES, content_type="image/png"))
            self.assertEqual(response.status_code, 200, response.data)
            self.assertTrue(response.data["success"])
            self.assertTrue(account.profile_image.startswith("uploads/admin_profiles/"))
            stored_file = Path(root) / account.profile_image
            self.assertTrue(stored_file.is_file())
            self.assertIn("/admin/manage/uploads/admin_profiles/", response.data["profile_image_url"])

    def test_non_super_admin_cannot_upload_profile_image(self):
        other = SimpleNamespace(principal_type="SALESMAN", pk=7, role="SALESMAN", is_authenticated=True)
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root), SERVE_LOCAL_UPLOADS=True):
            response = self.request(user=other, file=SimpleUploadedFile("avatar.png", PNG_BYTES, content_type="image/png"))
            self.assertEqual(response.status_code, 403)
            self.assertFalse(list(Path(root).rglob("*")))

    def test_invalid_image_signature_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root), SERVE_LOCAL_UPLOADS=True):
            response = self.request(user=self.super_admin(), file=SimpleUploadedFile("avatar.png", b"not-an-image", content_type="image/png"))
            self.assertEqual(response.status_code, 400)
            self.assertFalse(list(Path(root).rglob("*")))

    def test_missing_file_is_rejected(self):
        response = self.request(user=self.super_admin())
        self.assertEqual(response.status_code, 400)

    def test_unsupported_extension_is_rejected(self):
        response = self.request(user=self.super_admin(), file=SimpleUploadedFile("avatar.bmp", PNG_BYTES, content_type="image/png"))
        self.assertEqual(response.status_code, 400)

    def test_valid_png_signature_with_corrupt_payload_is_rejected(self):
        corrupt = bytes([137]) + b"PNG" + bytes([13, 10, 26, 10]) + b"not-a-real-png"
        response = self.request(user=self.super_admin(), file=SimpleUploadedFile("avatar.png", corrupt, content_type="image/png"))
        self.assertEqual(response.status_code, 400)

    def test_oversized_upload_is_rejected(self):
        from adminapi.profile_image import MAX_PROFILE_IMAGE_BYTES
        oversized = SimpleUploadedFile("avatar.png", b"x" * (MAX_PROFILE_IMAGE_BYTES + 1), content_type="image/png")
        response = self.request(user=self.super_admin(), file=oversized)
        self.assertEqual(response.status_code, 400)
    def test_upload_is_disabled_when_local_media_serving_is_disabled(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root), SERVE_LOCAL_UPLOADS=False):
            response = self.request(user=self.super_admin(), file=SimpleUploadedFile("avatar.png", PNG_BYTES, content_type="image/png"))
            self.assertEqual(response.status_code, 503)
            self.assertFalse(list(Path(root).rglob("*")))

    def test_database_schema_failure_removes_new_file_and_returns_safe_error(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root), SERVE_LOCAL_UPLOADS=True):
            account = FakeAccount()
            with mock.patch.object(AdminAccount.objects, "filter", return_value=SimpleNamespace(first=lambda: account)), \
                 mock.patch("adminapi.profile_image.save_admin_profile_image_path", side_effect=ProgrammingError('column "profile_image" does not exist')):
                response = self.request(user=self.super_admin(), file=SimpleUploadedFile("avatar.png", PNG_BYTES, content_type="image/png"))
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("missing profile_image column", str(response.data))
            self.assertFalse(list(Path(root).rglob("*.png")))

    def test_migration_is_additive_reversible_and_targets_only_admins_profile_image(self):
        migration = importlib.import_module("adminapi.migrations.0002_admin_profile_image").Migration
        operation = migration.operations[0]
        self.assertEqual(migration.dependencies, [("adminapi", "0001_cart_discount_settings"), ("accounts", "0003_admin_password_flow")])
        self.assertIn('ALTER TABLE "admins" ADD COLUMN "profile_image" varchar(500) NULL', operation.sql)
        self.assertIn('ALTER TABLE "admins" DROP COLUMN "profile_image"', operation.reverse_sql)
        self.assertNotIn("users", operation.sql)
        self.assertNotIn("DROP TABLE", operation.reverse_sql.upper())
