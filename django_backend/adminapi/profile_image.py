"""Authenticated, Super Admin-only profile-image upload and persistence."""
import uuid
from pathlib import Path
from PIL import Image, UnidentifiedImageError

from django.conf import settings
from django.db import OperationalError, ProgrammingError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from legacydb.models import AdminAccount
from .image_urls import image_url
from .profile import ActiveStaffPermission
from .profile_image_storage import save_admin_profile_image_path

MAX_PROFILE_IMAGE_BYTES = 5 * 1024 * 1024
PROFILE_IMAGE_SIGNATURES = {
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
}


def _valid_image_content(extension, header, upload):
    """Check extension signature and verify the actual image payload with Pillow."""
    if not any(header.startswith(signature) for signature in PROFILE_IMAGE_SIGNATURES[extension]):
        return False
    if extension == ".webp" and header[8:12] != b"WEBP":
        return False
    try:
        upload.seek(0)
        with Image.open(upload) as image:
            expected_format = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".gif": "GIF", ".webp": "WEBP"}[extension]
            if image.format != expected_format:
                return False
            image.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return False
    finally:
        upload.seek(0)


class AdminProfileImageView(APIView):
    """POST /admin-api/profile/image: upload and save the authenticated Super Admin's image path."""
    permission_classes = [IsAuthenticated, ActiveStaffPermission]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        user = request.user
        if getattr(user, "principal_type", None) != "ADMIN" or getattr(user, "role", None) != "SUPER_ADMIN":
            return Response({"success": False, "message": "Only the Super Admin can update this profile image."}, status=403)
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"success": False, "message": "Choose an image file to upload."}, status=400)
        if upload.size > MAX_PROFILE_IMAGE_BYTES:
            return Response({"success": False, "message": "The image must be 5 MB or smaller."}, status=400)
        name = (upload.name or "").lower()
        extension = "." + name.rsplit(".", 1)[1] if "." in name else ""
        if extension not in PROFILE_IMAGE_SIGNATURES:
            return Response({"success": False, "message": "Choose a JPG, PNG, WEBP, or GIF image."}, status=400)
        header = upload.read(16)
        upload.seek(0)
        if not _valid_image_content(extension, header, upload):
            return Response({"success": False, "message": "The file content does not match its image type."}, status=400)
        if not getattr(settings, "SERVE_LOCAL_UPLOADS", False):
            return Response({"success": False, "message": "Profile image storage is not enabled for this deployment."}, status=503)

        account = AdminAccount.objects.filter(pk=user.pk).first()
        if account is None:
            return Response({"success": False, "message": "Admin account not found."}, status=404)

        upload_dir = Path(settings.MEDIA_ROOT) / "uploads" / "admin_profiles"
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{uuid.uuid4().hex}{extension}"
            saved_file = upload_dir / filename
            relative_path = f"uploads/admin_profiles/{filename}"
            with saved_file.open("xb") as handle:
                for chunk in upload.chunks():
                    handle.write(chunk)
            try:
                if not save_admin_profile_image_path(account.pk, relative_path):
                    saved_file.unlink(missing_ok=True)
                    return Response({"success": False, "message": "Admin account not found."}, status=404)
            except (OperationalError, ProgrammingError):
                saved_file.unlink(missing_ok=True)
                return Response({"success": False, "message": "Profile image storage is not ready because the approved admin profile-image database migration has not been applied."}, status=503)
            except Exception:
                saved_file.unlink(missing_ok=True)
                raise
        except OSError:
            return Response({"success": False, "message": "The profile image could not be stored."}, status=503)

        return Response({
            "success": True,
            "message": "Profile image saved.",
            "profile_image_url": image_url(relative_path, request=request),
        }, status=200)
