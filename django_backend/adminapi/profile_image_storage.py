"""Narrow SQL access to the additive admins.profile_image column.

This deliberately avoids adding the not-yet-provisioned column to the unmanaged AdminAccount
model, so profile/password queries continue working until the approved migration is applied.
"""
from django.db import ProgrammingError, connection


def _is_missing_profile_image_column(exc):
    message = str(exc).lower()
    return "profile_image" in message and ("does not exist" in message or "undefined column" in message or "unknown column" in message)


def get_admin_profile_image_path(principal_type, principal_id):
    if principal_type != "ADMIN":
        return ""
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT "profile_image" FROM "admins" WHERE "id" = %s', [principal_id])
            row = cursor.fetchone()
    except ProgrammingError as exc:
        # The migration is intentionally not applied during this review. Until it is approved,
        # an absent column means no saved profile image, not a broken existing profile endpoint.
        if _is_missing_profile_image_column(exc):
            return ""
        raise
    return (row[0] or "") if row else ""


def save_admin_profile_image_path(principal_id, relative_path):
    with connection.cursor() as cursor:
        cursor.execute('UPDATE "admins" SET "profile_image" = %s WHERE "id" = %s', [relative_path, principal_id])
        return cursor.rowcount == 1
