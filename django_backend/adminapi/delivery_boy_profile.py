"""Delivery Boy profile helpers: Joining Date parsing, validated profile-image bytes, and the protected image endpoint.

The image bytes live in PostgreSQL (admin_delivery_boy_metadata.profile_image, BYTEA). Nothing here writes a file or
returns a filesystem path, and listing responses never carry the bytes.
"""
import re
import warnings
from datetime import date, timedelta
from io import BytesIO

from django.conf import settings
from django.db import OperationalError, ProgrammingError
from django.http import HttpResponse
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework.response import Response
from rest_framework.views import APIView

from .permissions import AdminRolePermission

MAX_PROFILE_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG', '.webp': 'WEBP'}
WEBP_MIME = 'image/webp'
DEFAULT_WEBP_QUALITY = 83
MIN_JOINING_DATE = date(1950, 1, 1)
MAX_JOINING_DAYS_AHEAD = 366
_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_TRUE = {'1', 'true', 'yes', 'on'}


class ProfileImageError(ValueError):
    """A user-facing reason the uploaded profile image was rejected."""


def parse_joining_date(raw):
    """Return a date from 'YYYY-MM-DD', or None when blank. Raises ValueError with a user-facing message."""
    text = '' if raw is None else str(raw).strip()
    if not text:
        return None
    if not _ISO_DATE.match(text):
        raise ValueError('Enter a valid joining date (YYYY-MM-DD).')
    try:
        value = date.fromisoformat(text)
    except ValueError:
        raise ValueError('Enter a valid joining date.')
    if value < MIN_JOINING_DATE:
        raise ValueError('Joining date cannot be before 01-01-1950.')
    if value > timezone.now().date() + timedelta(days=MAX_JOINING_DAYS_AHEAD):
        raise ValueError('Joining date cannot be more than a year in the future.')
    return value


def read_profile_image(upload):
    """Validate and convert a profile image to metadata-free WebP without resizing it."""
    name = (getattr(upload, 'name', '') or '').lower()
    extension = '.' + name.rsplit('.', 1)[1] if '.' in name else ''
    expected = ALLOWED_EXTENSIONS.get(extension)
    if expected is None:
        raise ProfileImageError('Choose a JPG, JPEG, PNG or WEBP image.')
    if getattr(upload, 'size', 0) > MAX_PROFILE_IMAGE_BYTES:
        raise ProfileImageError('The image must be 5 MB or smaller.')
    data = upload.read(MAX_PROFILE_IMAGE_BYTES + 1)
    if not data:
        raise ProfileImageError('The image file is empty.')
    if len(data) > MAX_PROFILE_IMAGE_BYTES:
        raise ProfileImageError('The image must be 5 MB or smaller.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as probe:
                if probe.format != expected:
                    raise ProfileImageError('The file content does not match its image type.')
                probe.verify()
            with Image.open(BytesIO(data)) as source:
                source.load()
                oriented = ImageOps.exif_transpose(source)
                has_alpha = oriented.mode in ('RGBA', 'LA') or 'transparency' in oriented.info
                converted = oriented.convert('RGBA' if has_alpha else 'RGB')
                output = BytesIO()
                quality = int(getattr(settings, 'DELIVERY_BOY_PROFILE_IMAGE_WEBP_QUALITY', DEFAULT_WEBP_QUALITY))
                if not 1 <= quality <= 100:
                    raise ProfileImageError('The WebP quality setting must be between 1 and 100.')
                converted.save(output, format='WEBP', quality=quality, method=6)
                result = output.getvalue()
                with Image.open(BytesIO(result)) as check:
                    check.load()
                    if check.format != 'WEBP' or check.size != converted.size:
                        raise ProfileImageError('The image could not be converted to WebP safely.')
    except ProfileImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ProfileImageError('The image is corrupted or exceeds Pillow\'s decompression safety limit.')
    return result, WEBP_MIME


def profile_upload(request):
    """The uploaded 'profile_image' file of a multipart request, or None (JSON requests have no files)."""
    files = getattr(request, 'FILES', None)
    return files.get('profile_image') if files else None


def wants_image_removal(data):
    value = data.get('remove_profile_image') if hasattr(data, 'get') else None
    return value is True or str(value).strip().lower() in _TRUE


class DeliveryBoyImageView(APIView):
    """GET /admin-api/delivery-boy-image?id=<delivery boy id>: the stored image bytes, staff-resource permission only."""
    permission_classes = [AdminRolePermission]
    required_resource = 'staff'

    def get(self, request):
        try:
            rid = int(request.query_params.get('id'))
        except (ValueError, TypeError):
            return Response({'id': 'Valid delivery boy id required'}, status=400)
        from .models import DeliveryBoyAdminMetadata
        try:
            row = DeliveryBoyAdminMetadata.objects.filter(delivery_boy_id=rid).only('profile_image', 'profile_image_mime').first()
        except (OperationalError, ProgrammingError):
            return Response({'detail': 'Profile image storage is not ready: the Delivery Boy profile migration has not been applied.'}, status=503)
        data = bytes(row.profile_image) if row is not None and row.profile_image else b''
        if not data:
            return Response({'detail': 'This delivery boy has no profile image.'}, status=404)
        response = HttpResponse(data, content_type=row.profile_image_mime or 'application/octet-stream')
        response['Cache-Control'] = 'private, no-cache'
        response['X-Content-Type-Options'] = 'nosniff'
        response['Content-Disposition'] = 'inline; filename="delivery-boy-%d.webp"' % rid
        return response

