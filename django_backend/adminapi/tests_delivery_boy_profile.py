"""Delivery Boy profile: Joining Date, profile image (bytes in PostgreSQL), password confirmation, authorization.

Database-free (SimpleTestCase + mocks), like tests_delivery_boy_management. The real BYTEA round trip is exercised by the
separate live check described in the delivery report, not here.
"""
import importlib
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone as dt_timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, connection, models
from django.test import SimpleTestCase
from django.utils import timezone
from PIL import Image

from adminapi.delivery_boy_profile import DeliveryBoyImageView, ProfileImageError, parse_joining_date, read_profile_image
from adminapi.operations import StaffAdminView
from adminapi.permissions import AdminRolePermission


def image_bytes(fmt, color=(200, 30, 30)):
    buffer = BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, fmt)
    return buffer.getvalue()


PNG, JPEG, WEBP = image_bytes("PNG"), image_bytes("JPEG"), image_bytes("WEBP")
NOW = datetime(2026, 10, 30, 5, 0, tzinfo=dt_timezone.utc)
BASE = {"kind": "delivery", "username": "driver_one", "email": "driver@example.com", "full_name": "Driver One",
        "phone": "9876543210", "address": "Kochi", "password": "StrongPassword123",
        "confirm_password": "StrongPassword123", "status": "ACTIVE"}


def upload(name, content, content_type="application/octet-stream"):
    return SimpleUploadedFile(name, content, content_type=content_type)


class CreateHarness(SimpleTestCase):
    """POST /staff?kind=delivery with every collaborator mocked; returns the response and the mocks."""

    def create(self, data=None, files=None, already_exists=False):
        payload = {**BASE, **(data or {})}
        with ExitStack() as stack:
            stack.enter_context(patch("adminapi.operations.transaction.atomic"))
            metadata_create = stack.enter_context(patch("adminapi.models.DeliveryBoyAdminMetadata.objects.create"))
            stack.enter_context(patch.object(StaffAdminView, "_metadata_table_ready", return_value=True))
            hasher = stack.enter_context(patch("adminapi.operations.hash_password", return_value="$argon2id$stored-hash"))
            model = stack.enter_context(patch("adminapi.operations.DeliveryBoy"))
            model.objects.filter.return_value.exists.return_value = already_exists
            model.objects.create.return_value = SimpleNamespace(
                id=41, username="driver_one", email="driver@example.com", fullname="Driver One",
                phone="9876543210", address="Kochi", created_at=NOW)
            metadata_create.side_effect = lambda **kw: SimpleNamespace(
                updated_at=NOW, is_active=kw["is_active"], joining_date=kw["joining_date"],
                profile_image=kw["profile_image"], profile_image_mime=kw["profile_image_mime"])
            response = StaffAdminView().post(SimpleNamespace(data=payload, FILES=files or {}))
        return SimpleNamespace(response=response, metadata_create=metadata_create, hasher=hasher, model=model)


class CreateTests(CreateHarness):
    def test_create_with_every_field_stores_them_and_generates_the_id(self):
        today = timezone.now().date()
        result = self.create({"joining_date": today.isoformat()}, {"profile_image": upload("me.png", PNG)})
        self.assertEqual(result.response.status_code, 201)
        data = result.response.data
        self.assertEqual((data["id"], data["delivery_boy_id"]), (41, 41))
        self.assertNotIn("id", result.model.objects.create.call_args.kwargs, "the ID comes from the legacy auto-increment, never from the client")
        self.assertEqual((data["username"], data["full_name"], data["phone"], data["address"], data["status"]),
                         ("driver_one", "Driver One", "9876543210", "Kochi", "ACTIVE"))
        self.assertEqual(data["joining_date"], today.isoformat())
        self.assertEqual((data["created_at"], data["updated_at"]), (NOW, NOW))
        self.assertTrue(data["has_profile_image"])
        self.assertEqual(data["profile_image_version"], NOW.isoformat())

    def test_joining_date_is_passed_as_a_real_date_and_returned_in_iso_form(self):
        result = self.create({"joining_date": "2026-10-01"})
        self.assertEqual(result.metadata_create.call_args.kwargs["joining_date"], date(2026, 10, 1))
        self.assertEqual(result.response.data["joining_date"], "2026-10-01")

    def test_joining_date_is_optional_and_distinct_from_created_date(self):
        result = self.create()
        self.assertEqual(result.response.status_code, 201)
        self.assertIsNone(result.response.data["joining_date"])
        self.assertEqual(result.response.data["created_at"], NOW)

    def test_invalid_joining_dates_are_rejected_without_creating_anything(self):
        too_far = (timezone.now().date() + timedelta(days=400)).isoformat()
        for bad in ("31-12-2026", "2026-02-30", "2026-13-01", "abc", "20261001", "1900-01-01", too_far, "2026-1-5"):
            with self.subTest(value=bad):
                result = self.create({"joining_date": bad})
                self.assertEqual(result.response.status_code, 400)
                self.assertIn("joining_date", result.response.data["errors"])
                result.model.objects.create.assert_not_called()

    def test_accepted_formats_are_converted_to_webp_before_database_storage(self):
        for name, content in (("a.png", PNG), ("a.jpg", JPEG), ("a.jpeg", JPEG), ("a.webp", WEBP)):
            with self.subTest(name=name):
                result = self.create(files={"profile_image": upload(name, content)})
                self.assertEqual(result.response.status_code, 201)
                stored = result.metadata_create.call_args.kwargs["profile_image"]
                self.assertEqual(result.metadata_create.call_args.kwargs["profile_image_mime"], "image/webp")
                with Image.open(BytesIO(stored)) as converted:
                    converted.load()
                    self.assertEqual(converted.format, "WEBP")
                    self.assertEqual(converted.size, (8, 8))

    def test_the_response_never_contains_the_image_bytes_or_password(self):
        data = self.create(files={"profile_image": upload("a.png", PNG)}).response.data
        for forbidden in ("profile_image", "password", "password_hash", "confirm_password"):
            self.assertNotIn(forbidden, data)
        self.assertFalse([value for value in data.values() if isinstance(value, (bytes, bytearray))])

    def test_unsupported_image_formats_are_rejected(self):
        for name in ("a.gif", "a.bmp", "a.svg", "a.txt", "a.pdf", "noextension"):
            with self.subTest(name=name):
                result = self.create(files={"profile_image": upload(name, PNG)})
                self.assertEqual(result.response.status_code, 400)
                self.assertIn("profile_image", result.response.data["errors"])
                result.model.objects.create.assert_not_called()

    def test_oversized_images_are_rejected(self):
        result = self.create(files={"profile_image": upload("big.png", PNG + b"\0" * (5 * 1024 * 1024))})
        self.assertEqual(result.response.status_code, 400)
        self.assertIn("5 MB", result.response.data["errors"]["profile_image"])

    def test_corrupted_or_disguised_content_is_rejected_whatever_the_extension(self):
        cases = {"junk.png": b"\x89PNG\r\n\x1a\n" + b"garbage" * 20, "truncated.png": PNG[:-20], "text.jpg": b"this is not an image",
                 "empty.png": b"", "jpeg-as.png": JPEG, "png-as.jpg": PNG, "webp-as.png": WEBP}
        for name, content in cases.items():
            with self.subTest(name=name):
                result = self.create(files={"profile_image": upload(name, content)})
                self.assertEqual(result.response.status_code, 400, name)
                self.assertIn("profile_image", result.response.data["errors"])
                result.model.objects.create.assert_not_called()

    def test_password_and_confirmation_must_match_and_are_hashed_with_the_shared_utility(self):
        mismatch = self.create({"confirm_password": "Different12345"})
        self.assertEqual(mismatch.response.status_code, 400)
        self.assertIn("confirm_password", mismatch.response.data)
        mismatch.model.objects.create.assert_not_called()
        ok = self.create()
        ok.hasher.assert_called_once_with("StrongPassword123")
        self.assertEqual(ok.model.objects.create.call_args.kwargs["password"], "$argon2id$stored-hash")

    def test_missing_confirmation_or_password_is_rejected(self):
        for field in ("password", "confirm_password"):
            with self.subTest(field=field):
                self.assertEqual(self.create({field: ""}).response.status_code, 400)

    def test_status_active_and_inactive_both_persist(self):
        for value, expected in (("ACTIVE", True), ("INACTIVE", False)):
            with self.subTest(status=value):
                result = self.create({"status": value})
                self.assertEqual(result.metadata_create.call_args.kwargs["is_active"], expected)
                self.assertEqual(result.response.data["status"], value)

    def test_unknown_status_is_rejected(self):
        self.assertEqual(self.create({"status": "MAYBE"}).response.status_code, 400)

    def test_duplicate_username_or_email_is_rejected(self):
        result = self.create(already_exists=True)
        self.assertEqual(result.response.status_code, 400)
        self.assertEqual(set(result.response.data["errors"]), {"username", "email"})


class EditHarness(SimpleTestCase):
    def setUp(self):
        self.existing_hash = "$argon2id$existing-hash"
        self.row = SimpleNamespace(id=41, username="driver_one", email="driver@example.com", fullname="Driver One", phone="9876543210",
                                   address="Kochi", password=self.existing_hash,
                                   created_at=datetime(2026, 1, 1, tzinfo=dt_timezone.utc), save=MagicMock())
        self.metadata = SimpleNamespace(is_active=True, updated_at=NOW, joining_date=date(2026, 1, 5), profile_image=b"OLD-BYTES",
                                        profile_image_mime="image/webp", save=MagicMock())

    def patch_(self, data, files=None):
        with ExitStack() as stack:
            stack.enter_context(patch("adminapi.operations.transaction.atomic"))
            stack.enter_context(patch("adminapi.models.DeliveryBoyAdminMetadata.objects.get_or_create", return_value=(self.metadata, False)))
            stack.enter_context(patch.object(StaffAdminView, "_metadata_table_ready", return_value=True))
            self.hasher = stack.enter_context(patch("adminapi.operations.hash_password", return_value="$argon2id$new-hash"))
            model = stack.enter_context(patch("adminapi.operations.DeliveryBoy"))
            model.objects.filter.return_value.first.return_value = self.row
            model.objects.filter.return_value.exclude.return_value.exists.return_value = False
            return StaffAdminView().patch(SimpleNamespace(data={"kind": "delivery", "id": 41, **data}, FILES=files or {}))


class EditTests(EditHarness):
    def test_an_unrelated_edit_preserves_joining_date_image_password_and_created_date(self):
        response = self.patch_({"phone": "9123456780"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.metadata.joining_date, date(2026, 1, 5))
        self.assertEqual((self.metadata.profile_image, self.metadata.profile_image_mime), (b"OLD-BYTES", "image/webp"))
        self.assertEqual(self.row.password, self.existing_hash)
        self.assertEqual(response.data["created_at"], datetime(2026, 1, 1, tzinfo=dt_timezone.utc))
        self.assertEqual(response.data["joining_date"], "2026-01-05")
        self.assertTrue(response.data["has_profile_image"])
        self.assertTrue(self.metadata.is_active, "status is not reset by an edit that does not mention it")

    def test_every_edit_saves_the_metadata_so_updated_at_advances(self):
        from adminapi.models import DeliveryBoyAdminMetadata
        self.patch_({"phone": "9123456780"})
        self.metadata.save.assert_called_once_with()
        self.assertTrue(DeliveryBoyAdminMetadata._meta.get_field("updated_at").auto_now)
        self.assertFalse(getattr(DeliveryBoyAdminMetadata._meta.get_field("joining_date"), "auto_now", False))
        self.assertFalse(getattr(DeliveryBoyAdminMetadata._meta.get_field("joining_date"), "auto_now_add", False))

    def test_joining_date_changes_only_when_explicitly_sent(self):
        self.assertEqual(self.patch_({"joining_date": "2026-03-04"}).data["joining_date"], "2026-03-04")
        self.assertEqual(self.metadata.joining_date, date(2026, 3, 4))

    def test_invalid_or_blank_joining_date_on_edit_is_rejected_and_nothing_is_saved(self):
        for bad in ("04-03-2026", "2026-02-30", "", "nonsense"):
            with self.subTest(value=bad):
                self.metadata.save.reset_mock()
                response = self.patch_({"joining_date": bad})
                self.assertEqual(response.status_code, 400)
                self.assertIn("joining_date", response.data["errors"])
                self.assertEqual(self.metadata.joining_date, date(2026, 1, 5))
                self.metadata.save.assert_not_called()

    def test_image_replacement(self):
        response = self.patch_({}, {"profile_image": upload("new.webp", WEBP)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.metadata.profile_image_mime, "image/webp")
        with Image.open(BytesIO(self.metadata.profile_image)) as stored:
            stored.load()
            self.assertEqual((stored.format, stored.size), ("WEBP", (8, 8)))
        self.assertNotIn("profile_image", response.data)

    def test_image_removal(self):
        for flag in ("true", "1", True):
            with self.subTest(flag=flag):
                self.metadata.profile_image, self.metadata.profile_image_mime = b"OLD-BYTES", "image/webp"
                response = self.patch_({"remove_profile_image": flag})
                self.assertEqual(response.status_code, 200)
                self.assertEqual((self.metadata.profile_image, self.metadata.profile_image_mime), (None, ""))
                self.assertFalse(response.data["has_profile_image"])
                self.assertEqual(response.data["profile_image_version"], "")

    def test_false_removal_flag_keeps_the_image(self):
        self.patch_({"remove_profile_image": "false"})
        self.assertEqual(self.metadata.profile_image, b"OLD-BYTES")

    def test_choosing_a_file_and_removing_in_one_request_is_rejected(self):
        response = self.patch_({"remove_profile_image": "true"}, {"profile_image": upload("new.png", PNG)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.metadata.profile_image, b"OLD-BYTES")

    def test_bad_replacement_images_leave_the_existing_image_untouched(self):
        for name, content in (("a.gif", PNG), ("a.png", b"not an image"), ("a.png", JPEG), ("a.png", PNG[:-20])):
            with self.subTest(name=name, size=len(content)):
                response = self.patch_({}, {"profile_image": upload(name, content)})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.metadata.profile_image, b"OLD-BYTES")

    def test_blank_passwords_preserve_the_existing_hash(self):
        response = self.patch_({"password": "", "confirm_password": "", "full_name": "Driver Renamed"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.row.password, self.existing_hash)
        self.hasher.assert_not_called()

    def test_matching_new_passwords_update_the_hash(self):
        response = self.patch_({"password": "AnotherStrong123", "confirm_password": "AnotherStrong123"})
        self.assertEqual(response.status_code, 200)
        self.hasher.assert_called_once_with("AnotherStrong123")
        self.assertEqual(self.row.password, "$argon2id$new-hash")
        self.assertNotIn("password", response.data)

    def test_mismatched_or_orphan_confirmation_is_rejected_and_the_hash_kept(self):
        for data in ({"password": "AnotherStrong123", "confirm_password": "Different12345"},
                     {"password": "AnotherStrong123", "confirm_password": ""},
                     {"password": "", "confirm_password": "AnotherStrong123"}):
            with self.subTest(data=data):
                response = self.patch_(data)
                self.assertEqual(response.status_code, 400)
                self.assertIn("confirm_password", response.data)
                self.assertEqual(self.row.password, self.existing_hash)

    def test_status_change_persists_and_other_fields_are_left_alone(self):
        response = self.patch_({"status": "INACTIVE"})
        self.assertEqual(response.data["status"], "INACTIVE")
        self.assertFalse(self.metadata.is_active)
        self.assertEqual(self.row.phone, "9876543210")
        self.assertEqual(self.patch_({"status": "ACTIVE"}).data["status"], "ACTIVE")


class ListAndStorageTests(SimpleTestCase):
    def test_listing_carries_image_flag_and_joining_date_but_never_bytes(self):
        metadata = SimpleNamespace(is_active=False, updated_at=NOW, joining_date=date(2026, 2, 3),
                                   profile_image=b"SECRET", profile_image_mime="image/jpeg")
        row = SimpleNamespace(id=7, username="u", email="e@example.com", fullname="Name", phone="9876543210", address="Addr", created_at=NOW)
        out = StaffAdminView._delivery_out(row, metadata)
        self.assertEqual((out["delivery_boy_id"], out["joining_date"], out["status"], out["has_profile_image"]), (7, "2026-02-03", "INACTIVE", True))
        self.assertNotIn("profile_image", out)
        self.assertNotIn(b"SECRET", out.values())
        legacy = StaffAdminView._delivery_out(row, None)
        self.assertEqual((legacy["status"], legacy["joining_date"], legacy["has_profile_image"], legacy["profile_image_version"]),
                         ("ACTIVE", None, False, ""))

    def test_the_listing_query_defers_the_image_column(self):
        with patch("adminapi.operations.connection") as conn, patch("adminapi.models.DeliveryBoyAdminMetadata.objects.filter") as flt, \
                patch.object(StaffAdminView, "_metadata_has_profile_columns", return_value=True):
            conn.introspection.table_names.return_value = ["admin_delivery_boy_metadata"]
            flt.return_value.defer.return_value = []
            StaffAdminView._delivery_metadata([1, 2])
        flt.return_value.defer.assert_called_once_with("profile_image")

    def test_before_the_migration_the_listing_keeps_working_from_the_original_columns(self):
        with patch("adminapi.operations.connection") as conn, patch("adminapi.models.DeliveryBoyAdminMetadata.objects.filter") as flt, \
                patch.object(StaffAdminView, "_metadata_has_profile_columns", return_value=False):
            conn.introspection.table_names.return_value = ["admin_delivery_boy_metadata"]
            flt.return_value.values.return_value = [{"delivery_boy_id": 9, "is_active": True, "updated_at": NOW}]
            found = StaffAdminView._delivery_metadata([9])
        row = SimpleNamespace(id=9, username="u", email="e@example.com", fullname="N", phone="9876543210", address="A", created_at=NOW)
        out = StaffAdminView._delivery_out(row, found[9])
        self.assertEqual((out["status"], out["joining_date"], out["has_profile_image"]), ("ACTIVE", None, False))

    def test_writes_are_blocked_until_the_profile_migration_is_applied(self):
        with patch("adminapi.operations.connection") as conn, patch.object(StaffAdminView, "_metadata_has_profile_columns", return_value=False):
            conn.introspection.table_names.return_value = ["admin_delivery_boy_metadata"]
            self.assertFalse(StaffAdminView._metadata_table_ready())
        with patch("adminapi.operations.connection") as conn, patch.object(StaffAdminView, "_metadata_has_profile_columns", return_value=True):
            conn.introspection.table_names.return_value = ["admin_delivery_boy_metadata"]
            self.assertTrue(StaffAdminView._metadata_table_ready())

    def test_the_model_stores_the_image_as_bytea_and_joining_date_as_a_date(self):
        from adminapi.models import DeliveryBoyAdminMetadata as Meta
        image = Meta._meta.get_field("profile_image")
        self.assertIsInstance(image, models.BinaryField)
        self.assertEqual(image.db_type(connection), "bytea")
        self.assertTrue(image.null)
        self.assertIsInstance(Meta._meta.get_field("joining_date"), models.DateField)
        self.assertNotIsInstance(Meta._meta.get_field("joining_date"), models.DateTimeField)
        self.assertEqual(Meta._meta.get_field("joining_date").db_type(connection), "date")
        for kept in ("delivery_boy_id", "is_active", "updated_at"):
            Meta._meta.get_field(kept)

    def test_migration_0004_is_additive_and_leaves_0003_alone(self):
        migration = importlib.import_module("adminapi.migrations.0004_deliveryboy_profile_joining").Migration
        self.assertEqual(migration.dependencies, [("adminapi", "0003_deliveryboyadminmetadata")])
        self.assertEqual([type(op).__name__ for op in migration.operations], ["AddField"] * 3)
        self.assertEqual({op.name for op in migration.operations}, {"joining_date", "profile_image", "profile_image_mime"})
        for op in migration.operations:
            self.assertTrue(op.field.null or op.field.has_default(), "existing rows must stay valid: " + op.name)
            self.assertEqual(op.model_name, "deliveryboyadminmetadata")


class ImageRetrievalTests(SimpleTestCase):
    def get(self, query, row="__none__", error=None):
        with patch("adminapi.models.DeliveryBoyAdminMetadata.objects.filter") as flt:
            chain = flt.return_value.only.return_value
            if error:
                chain.first.side_effect = error
            else:
                chain.first.return_value = None if row == "__none__" else row
            return DeliveryBoyImageView().get(SimpleNamespace(query_params=query))

    def test_returns_the_stored_bytes_with_safe_headers(self):
        # PostgreSQL drivers hand BYTEA back as a memoryview.
        response = self.get({"id": "41"}, SimpleNamespace(profile_image=memoryview(WEBP), profile_image_mime="image/webp"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, WEBP)
        self.assertEqual(response["Content-Type"], "image/webp")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("private", response["Cache-Control"])

    def test_no_image_invalid_id_and_unmigrated_database(self):
        self.assertEqual(self.get({"id": "41"}).status_code, 404)
        self.assertEqual(self.get({"id": "41"}, SimpleNamespace(profile_image=None, profile_image_mime="")).status_code, 404)
        self.assertEqual(self.get({"id": "abc"}).status_code, 400)
        self.assertEqual(self.get({}).status_code, 400)
        self.assertEqual(self.get({"id": "41"}, error=OperationalError("no such column")).status_code, 503)

    def test_view_requires_the_staff_resource(self):
        self.assertEqual(DeliveryBoyImageView.required_resource, "staff")
        self.assertIn(AdminRolePermission, DeliveryBoyImageView.permission_classes)


class AuthorizationTests(SimpleTestCase):
    def allowed(self, role, method, resource="staff", authenticated=True):
        request = SimpleNamespace(user=SimpleNamespace(is_authenticated=authenticated, role=role), method=method, data={})
        return AdminRolePermission().has_permission(request, SimpleNamespace(required_resource=resource))

    def test_only_super_admin_and_admin_can_manage_delivery_boys_and_images(self):
        for method in ("GET", "POST", "PATCH", "DELETE"):
            for role in ("SUPER_ADMIN", "ADMIN"):
                self.assertTrue(self.allowed(role, method), (role, method))
            for role in ("DELIVERY_BOY", "SALESMAN", "WAREHOUSE_STAFF", "CUSTOMER", None):
                self.assertFalse(self.allowed(role, method), (role, method))
            self.assertFalse(self.allowed("ADMIN", method, authenticated=False), "anonymous")

    def test_the_image_endpoint_uses_the_same_resource_as_the_staff_api(self):
        self.assertEqual(DeliveryBoyImageView.required_resource, StaffAdminView.required_resource)
        self.assertFalse(self.allowed("DELIVERY_BOY", "GET", DeliveryBoyImageView.required_resource))


class HelperTests(SimpleTestCase):
    def test_joining_date_parser(self):
        self.assertIsNone(parse_joining_date(None))
        self.assertIsNone(parse_joining_date("   "))
        self.assertEqual(parse_joining_date("2026-10-01"), date(2026, 10, 1))
        with self.assertRaises(ValueError):
            parse_joining_date("01-10-2026")

    def test_image_reader_returns_metadata_free_webp_and_rejects_extension_mismatch(self):
        converted, mime = read_profile_image(upload("x.png", PNG))
        self.assertEqual(mime, "image/webp")
        with Image.open(BytesIO(converted)) as image:
            image.load()
            self.assertEqual((image.format, image.size), ("WEBP", (8, 8)))
        with self.assertRaises(ProfileImageError):
            read_profile_image(upload("x.png", JPEG))

    def test_exif_orientation_is_applied_and_metadata_is_removed(self):
        source = Image.new("RGB", (13, 7), (90, 120, 150))
        exif = Image.Exif()
        exif[274] = 6
        exif[315] = "Private camera metadata"
        raw = BytesIO()
        source.save(raw, "JPEG", exif=exif)
        converted, mime = read_profile_image(upload("oriented.jpg", raw.getvalue()))
        self.assertEqual(mime, "image/webp")
        with Image.open(BytesIO(converted)) as result:
            result.load()
            self.assertEqual(result.format, "WEBP")
            self.assertEqual(result.size, (7, 13))
            self.assertFalse(result.getexif())
            self.assertNotIn("exif", result.info)
            self.assertNotIn("xmp", result.info)

    def test_password_hashes_use_argon2id_and_never_equal_the_plaintext(self):
        from accounts.hashers import hash_password, verify_php_password
        encoded = hash_password("StrongPassword123")
        self.assertTrue(encoded.startswith("$argon2id$"))
        self.assertNotIn("StrongPassword123", encoded)
        self.assertTrue(verify_php_password("StrongPassword123", encoded))

class RealMultipartRequestTests(CreateHarness):
    """Goes through DRF's real parsers and the real permission class instead of a hand-built request double."""

    def send(self, fields, files):
        from rest_framework.test import APIRequestFactory, force_authenticate
        body = {**BASE, **fields, **files}
        request = APIRequestFactory().post("/admin-api/staff", body, format="multipart")
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, role="ADMIN", pk=1, principal_type="ADMIN"))
        with ExitStack() as stack:
            stack.enter_context(patch("adminapi.operations.transaction.atomic"))
            create = stack.enter_context(patch("adminapi.models.DeliveryBoyAdminMetadata.objects.create"))
            stack.enter_context(patch.object(StaffAdminView, "_metadata_table_ready", return_value=True))
            stack.enter_context(patch("adminapi.operations.hash_password", return_value="$argon2id$stored-hash"))
            model = stack.enter_context(patch("adminapi.operations.DeliveryBoy"))
            model.objects.filter.return_value.exists.return_value = False
            model.objects.create.return_value = SimpleNamespace(id=41, username="driver_one", email="driver@example.com", fullname="Driver One",
                                                                phone="9876543210", address="Kochi", created_at=NOW)
            create.side_effect = lambda **kw: SimpleNamespace(updated_at=NOW, **kw)
            response = StaffAdminView.as_view()(request)
        return response, create

    def test_a_multipart_form_with_a_file_is_parsed_and_the_bytes_survive(self):
        response, create = self.send({"joining_date": "2026-10-01"}, {"profile_image": upload("photo.png", PNG, "image/png")})
        self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        stored = create.call_args.kwargs["profile_image"]
        self.assertEqual(create.call_args.kwargs["profile_image_mime"], "image/webp")
        with Image.open(BytesIO(stored)) as converted:
            converted.load()
            self.assertEqual((converted.format, converted.size), ("WEBP", (8, 8)))
        self.assertEqual(create.call_args.kwargs["joining_date"], date(2026, 10, 1))
        self.assertEqual(response.data["joining_date"], "2026-10-01")

    def test_the_same_endpoint_still_accepts_plain_json(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        request = APIRequestFactory().post("/admin-api/staff", {**BASE, "joining_date": "2026-10-01"}, format="json")
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, role="ADMIN", pk=1, principal_type="ADMIN"))
        with ExitStack() as stack:
            stack.enter_context(patch("adminapi.operations.transaction.atomic"))
            create = stack.enter_context(patch("adminapi.models.DeliveryBoyAdminMetadata.objects.create"))
            stack.enter_context(patch.object(StaffAdminView, "_metadata_table_ready", return_value=True))
            stack.enter_context(patch("adminapi.operations.hash_password", return_value="$argon2id$stored-hash"))
            model = stack.enter_context(patch("adminapi.operations.DeliveryBoy"))
            model.objects.filter.return_value.exists.return_value = False
            model.objects.create.return_value = SimpleNamespace(id=41, username="driver_one", email="driver@example.com", fullname="Driver One",
                                                                phone="9876543210", address="Kochi", created_at=NOW)
            create.side_effect = lambda **kw: SimpleNamespace(updated_at=NOW, **kw)
            response = StaffAdminView.as_view()(request)
        self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        self.assertIsNone(create.call_args.kwargs["profile_image"])

    def test_an_unauthorized_role_is_refused_before_any_work(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        request = APIRequestFactory().post("/admin-api/staff", BASE, format="json")
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, role="DELIVERY_BOY", pk=2, principal_type="ADMIN"))
        with patch("adminapi.operations.DeliveryBoy") as model:
            response = StaffAdminView.as_view()(request)
        self.assertEqual(response.status_code, 403)
        model.objects.create.assert_not_called()
