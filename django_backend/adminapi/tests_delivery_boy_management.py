from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from adminapi.operations import StaffAdminView
from adminapi.permissions import AdminRolePermission


class DeliveryBoyCreateTests(SimpleTestCase):
    def setUp(self):
        self.view = StaffAdminView()
        self.now = datetime(2026, 10, 30, 5, 0, tzinfo=dt_timezone.utc)
        self.data = {
            "kind": "delivery", "username": "driver_one", "email": "driver@example.com",
            "full_name": "Driver One", "phone": "9876543210", "address": "Kochi",
            "password": "StrongPassword123", "confirm_password": "StrongPassword123",
            "status": "ACTIVE",
        }

    @patch("adminapi.operations.transaction.atomic")
    @patch("adminapi.models.DeliveryBoyAdminMetadata.objects.create")
    @patch.object(StaffAdminView, "_metadata_table_ready", return_value=True)
    @patch("adminapi.operations.hash_password", return_value="argon2id$test-hash")
    @patch("adminapi.operations.DeliveryBoy")
    def test_create_generates_id_hashes_password_and_never_returns_hash(
        self, delivery_model, hash_password, _table_ready, metadata_create, _atomic
    ):
        delivery_model.objects.filter.return_value.exists.return_value = False
        delivery_model.objects.create.return_value = SimpleNamespace(
            id=41, username="driver_one", email="driver@example.com", fullname="Driver One", created_at=self.now
        )
        metadata_create.return_value = SimpleNamespace(updated_at=self.now)

        response = self.view.post(SimpleNamespace(data=self.data))

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["id"], 41)
        self.assertEqual(response.data["status"], "ACTIVE")
        self.assertEqual(response.data["created_at"], self.now)
        self.assertEqual(response.data["updated_at"], self.now)
        self.assertNotIn("password", response.data)
        self.assertNotIn("password_hash", response.data)
        hash_password.assert_called_once_with("StrongPassword123")
        self.assertEqual(delivery_model.objects.create.call_args.kwargs["password"], "argon2id$test-hash")
        self.assertEqual(delivery_model.objects.create.call_args.kwargs["id"] if "id" in delivery_model.objects.create.call_args.kwargs else None, None)

    @patch("adminapi.operations.DeliveryBoy")
    def test_duplicate_username_and_email_are_rejected(self, delivery_model):
        delivery_model.objects.filter.return_value.exists.return_value = True
        response = self.view.post(SimpleNamespace(data=self.data))
        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.data["errors"])
        self.assertIn("email", response.data["errors"])
        delivery_model.objects.create.assert_not_called()

    @patch("adminapi.operations.DeliveryBoy")
    def test_password_confirmation_mismatch_is_rejected(self, delivery_model):
        payload = {**self.data, "confirm_password": "different-password"}
        response = self.view.post(SimpleNamespace(data=payload))
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirm_password", response.data)
        delivery_model.objects.create.assert_not_called()

    @patch.object(StaffAdminView, "_metadata_table_ready", return_value=False)
    def test_create_does_not_write_until_required_metadata_migration_is_applied(self, _table_ready):
        with patch("adminapi.operations.DeliveryBoy") as delivery_model:
            delivery_model.objects.filter.return_value.exists.return_value = False
            response = self.view.post(SimpleNamespace(data=self.data))
        self.assertEqual(response.status_code, 503)
        delivery_model.objects.create.assert_not_called()


class DeliveryBoyEditTests(SimpleTestCase):
    def setUp(self):
        self.view = StaffAdminView()
        self.existing_hash = "argon2id$existing-hash"
        self.delivery_boy = SimpleNamespace(
            id=41, username="driver_one", email="driver@example.com", fullname="Driver One",
            phone="9876543210", address="Kochi", password=self.existing_hash,
            created_at=datetime(2026, 1, 1, tzinfo=dt_timezone.utc), save=MagicMock(),
        )
        self.metadata = SimpleNamespace(is_active=True, updated_at=None, save=MagicMock())

    @patch("adminapi.operations.transaction.atomic")
    @patch("adminapi.models.DeliveryBoyAdminMetadata.objects.get_or_create")
    @patch.object(StaffAdminView, "_metadata_table_ready", return_value=True)
    @patch("adminapi.operations.hash_password")
    @patch("adminapi.operations.DeliveryBoy")
    def test_edit_without_password_preserves_existing_hash(
        self, delivery_model, hash_password, _table_ready, metadata_get, _atomic
    ):
        delivery_model.objects.filter.return_value.first.return_value = self.delivery_boy
        metadata_get.return_value = (self.metadata, False)
        request = SimpleNamespace(data={"kind": "delivery", "id": 41, "status": "INACTIVE"})

        response = self.view.patch(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.delivery_boy.password, self.existing_hash)
        self.assertEqual(response.data["status"], "INACTIVE")
        hash_password.assert_not_called()

    @patch("adminapi.operations.transaction.atomic")
    @patch("adminapi.models.DeliveryBoyAdminMetadata.objects.get_or_create")
    @patch.object(StaffAdminView, "_metadata_table_ready", return_value=True)
    @patch("adminapi.operations.hash_password", return_value="argon2id$new-hash")
    @patch("adminapi.operations.DeliveryBoy")
    def test_edit_with_password_hashes_new_password(
        self, delivery_model, hash_password, _table_ready, metadata_get, _atomic
    ):
        delivery_model.objects.filter.return_value.first.return_value = self.delivery_boy
        metadata_get.return_value = (self.metadata, False)
        request = SimpleNamespace(data={
            "kind": "delivery", "id": 41, "password": "AnotherStrong123",
            "confirm_password": "AnotherStrong123",
        })

        response = self.view.patch(request)

        self.assertEqual(response.status_code, 200)
        hash_password.assert_called_once_with("AnotherStrong123")
        self.assertEqual(self.delivery_boy.password, "argon2id$new-hash")
        self.assertNotIn("password", response.data)


class DeliveryBoyAuthorizationTests(SimpleTestCase):
    def test_delivery_boy_role_cannot_manage_staff_resource(self):
        request = SimpleNamespace(user=SimpleNamespace(is_authenticated=True, role="DELIVERY_BOY"), method="GET")
        view = SimpleNamespace(required_resource="staff")
        self.assertFalse(AdminRolePermission().has_permission(request, view))

class DeliveryBoyDeleteTests(SimpleTestCase):
    @patch("adminapi.operations.connection")
    @patch("adminapi.operations.Checkout")
    @patch("adminapi.operations.DeliveryBoy")
    def test_delete_is_blocked_when_delivery_boy_has_order_history(self, delivery_model, checkout_model, db_connection):
        delivery_model.objects.filter.return_value.exists.return_value = True
        checkout_model.objects.filter.return_value.exists.return_value = True
        cursor = MagicMock()
        cursor.fetchone.return_value = (0,)
        db_connection.cursor.return_value.__enter__.return_value = cursor
        request = SimpleNamespace(query_params={"kind": "delivery", "id": "41"}, data={})

        response = StaffAdminView().delete(request)

        self.assertEqual(response.status_code, 409)
        self.assertIn("Inactive", response.data["detail"])
        delivery_model.objects.filter.return_value.delete.assert_not_called()

class DeliveryBoyListTests(SimpleTestCase):
    @patch.object(StaffAdminView, "_delivery_metadata", return_value={})
    @patch("adminapi.operations.DeliveryBoy")
    def test_search_and_pagination_are_preserved_and_legacy_rows_default_active(self, delivery_model, _metadata):
        row = SimpleNamespace(
            id=41, username="driver_one", email="driver@example.com", fullname="Driver One",
            phone="9876543210", address="Kochi", created_at=None,
        )
        queryset = MagicMock()
        queryset.count.return_value = 12
        queryset.__getitem__.return_value = [row]
        delivery_model.objects.order_by.return_value = queryset
        delivery_model.objects.order_by.return_value.filter.return_value = queryset
        request = SimpleNamespace(query_params={"kind": "delivery", "search": "driver", "page": "2", "page_size": "5"})

        response = StaffAdminView().get(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data["count"], response.data["page"], response.data["page_size"]), (12, 2, 5))
        self.assertEqual(response.data["results"][0]["id"], 41)
        self.assertEqual(response.data["results"][0]["status"], "ACTIVE")
        self.assertIn("created_at", response.data["results"][0])
        self.assertIn("updated_at", response.data["results"][0])

    def test_new_password_hashes_are_verifiable_and_not_plain_text(self):
        from accounts.hashers import hash_password, verify_php_password
        password = "StrongPassword123"
        encoded = hash_password(password)
        self.assertNotEqual(encoded, password)
        self.assertTrue(encoded.startswith("$argon2id$"))
        self.assertTrue(verify_php_password(password, encoded))
        self.assertFalse(verify_php_password("wrong-password", encoded))
