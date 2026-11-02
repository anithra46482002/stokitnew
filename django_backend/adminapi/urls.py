from django.urls import path

from .views import AdminLoginView, AdminRefreshView, DashboardView, CategoryListView, ProductListView, CustomerListView, OrderListView
from .crud import CategoryCRUDView, SubcategoryCRUDView, ProductCRUDView, VariantCRUDView, ProductImageCRUDView, CustomerCRUDView, OrderAdminCRUDView
from .operations import BannerAdminView, NotificationAdminView, WarehouseAdminView, InventoryAdminView, StaffAdminView, AdvancePaymentAdminView, ReportsAdminView, LocationAdminView, TargetAdminView, TargetAssignmentAdminView
from .advance_allocations import AdvanceAllocationAdminView
from .allocation_management import AllocationManagementView
from .uploads import ImageUploadView
from .offers import AdvancePaymentOffersView
from .cart_discount import CartDiscountSettingsView
from .order_pages import OrderManageView, OrderBulkAssignView, OrderDetailView, OrderStatusView
from .profile import (
    AdminProfileView, ChangePasswordVerifyCurrentView, ChangePasswordSendOtpView, ChangePasswordVerifyOtpView,
    ChangePasswordConfirmView, ForgotPasswordSendOtpView, ForgotPasswordVerifyOtpView, ForgotPasswordResetView,
)
from .profile_image import AdminProfileImageView
from .delivery_boy_profile import DeliveryBoyImageView
from .account_identity import UsernameOtpView, UsernameVerifyView, EmailCurrentOtpView, EmailNewOtpView, EmailVerifyView, EmailCompleteView
from legacy_api import delivery_auth

urlpatterns = [
    # Delivery Boy authentication aliases for the public /admin-api/ namespace.
    # Reuse the exact same views as /api/ (Flutter); no authentication logic is duplicated.
    path("delivery/auth/login", delivery_auth.DeliveryBoyLoginView.as_view(), name="admin-delivery-auth-login"),
    path("delivery/auth/refresh", delivery_auth.DeliveryBoyRefreshView.as_view(), name="admin-delivery-auth-refresh"),
    path("delivery/auth/forgot-password/request-otp", delivery_auth.DeliveryBoyForgotRequestView.as_view(), name="admin-delivery-auth-forgot-request"),
    path("delivery/auth/forgot-password/verify-otp", delivery_auth.DeliveryBoyForgotVerifyView.as_view(), name="admin-delivery-auth-forgot-verify"),
    path("delivery/auth/forgot-password/reset-password", delivery_auth.DeliveryBoyForgotResetView.as_view(), name="admin-delivery-auth-forgot-reset"),
    path("delivery/auth/change-password/request-otp", delivery_auth.DeliveryBoyChangeRequestView.as_view(), name="admin-delivery-auth-change-request"),
    path("delivery/auth/change-password/verify-otp", delivery_auth.DeliveryBoyChangeVerifyView.as_view(), name="admin-delivery-auth-change-verify"),
    path("delivery/auth/change-password", delivery_auth.DeliveryBoyChangePasswordView.as_view(), name="admin-delivery-auth-change"),
    path("delivery/auth/profile", delivery_auth.DeliveryBoyProfileView.as_view(), name="admin-delivery-auth-profile"),
    path("delivery/auth/logout", delivery_auth.DeliveryBoyLogoutView.as_view(), name="admin-delivery-auth-logout"),
    path("delivery/auth/sessions", delivery_auth.DeliveryBoySessionsView.as_view(), name="admin-delivery-auth-sessions"),
    path("delivery/auth/sessions/revoke", delivery_auth.DeliveryBoyRevokeSessionView.as_view(), name="admin-delivery-auth-session-revoke"),
    path("delivery/profile/image", delivery_auth.DeliveryBoyProfileImageView.as_view(), name="admin-delivery-profile-image"),

    path("auth/login", AdminLoginView.as_view(), name="admin-login"),
    path("auth/refresh", AdminRefreshView.as_view(), name="admin-refresh"),
    path("profile", AdminProfileView.as_view(), name="admin-profile"),
    path("profile/image", AdminProfileImageView.as_view(), name="admin-profile-image"),
    path("profile/username/send-otp", UsernameOtpView.as_view(), name="admin-username-send-otp"),
    path("profile/username/verify-otp", UsernameVerifyView.as_view(), name="admin-username-verify-otp"),
    path("profile/email/send-current-otp", EmailCurrentOtpView.as_view(), name="admin-email-current-otp"),
    path("profile/email/send-new-otp", EmailNewOtpView.as_view(), name="admin-email-new-otp"),
    path("profile/email/verify-otp", EmailVerifyView.as_view(), name="admin-email-verify-otp"),
    path("profile/email/complete", EmailCompleteView.as_view(), name="admin-email-complete"),
    path("password/change/verify-current", ChangePasswordVerifyCurrentView.as_view(), name="admin-password-change-verify-current"),
    path("password/change/send-otp", ChangePasswordSendOtpView.as_view(), name="admin-password-change-send-otp"),
    path("password/change/verify-otp", ChangePasswordVerifyOtpView.as_view(), name="admin-password-change-verify-otp"),
    path("password/change/confirm", ChangePasswordConfirmView.as_view(), name="admin-password-change-confirm"),
    path("password/forgot/send-otp", ForgotPasswordSendOtpView.as_view(), name="admin-password-forgot-send-otp"),
    path("password/forgot/verify-otp", ForgotPasswordVerifyOtpView.as_view(), name="admin-password-forgot-verify-otp"),
    path("password/forgot/reset", ForgotPasswordResetView.as_view(), name="admin-password-forgot-reset"),
    path("uploads", ImageUploadView.as_view(), name="admin-uploads"),
    path("dashboard", DashboardView.as_view(), name="admin-dashboard"),
    path("reports", ReportsAdminView.as_view(), name="admin-reports"),
    path("categories", CategoryCRUDView.as_view(), name="admin-categories"),
    path("subcategories", SubcategoryCRUDView.as_view(), name="admin-subcategories"),
    path("products", ProductCRUDView.as_view(), name="admin-products"),
    path("product-variants", VariantCRUDView.as_view(), name="admin-product-variants"),
    path("product-images", ProductImageCRUDView.as_view(), name="admin-product-images"),
    path("customers", CustomerCRUDView.as_view(), name="admin-customers"),
    path("orders", OrderAdminCRUDView.as_view(), name="admin-orders"),
    path("orders/manage", OrderManageView.as_view(), name="admin-orders-manage"),
    path("orders/bulk-assign", OrderBulkAssignView.as_view(), name="admin-orders-bulk-assign"),
    path("orders/<int:order_id>/detail", OrderDetailView.as_view(), name="admin-order-detail"),
    path("orders/<int:order_id>/status", OrderStatusView.as_view(), name="admin-order-status"),
    path("banners", BannerAdminView.as_view(), name="admin-banners"),
    path("notifications", NotificationAdminView.as_view(), name="admin-notifications"),
    path("warehouses", WarehouseAdminView.as_view(), name="admin-warehouses"),
    path("inventory", InventoryAdminView.as_view(), name="admin-inventory"),
    path("cart-discount-settings", CartDiscountSettingsView.as_view(), name="admin-cart-discount-settings"),
    path("staff", StaffAdminView.as_view(), name="admin-staff"),
    path("delivery-boy-image", DeliveryBoyImageView.as_view(), name="admin-delivery-boy-image"),
    path("advance-payments", AdvancePaymentOffersView.as_view(), name="admin-advance-payments"),
    path("advance-allocations", AdvanceAllocationAdminView.as_view(), name="admin-advance-allocations"),
    path("allocations", AllocationManagementView.as_view(), name="admin-allocations"),
    path("allocations/<int:allocation_id>", AllocationManagementView.as_view(), name="admin-allocation-detail"),
    path("locations", LocationAdminView.as_view(), name="admin-locations"),
    path("targets", TargetAdminView.as_view(), name="admin-targets"),
    path("targets/assignments", TargetAssignmentAdminView.as_view(), name="admin-target-assignments"),
]
