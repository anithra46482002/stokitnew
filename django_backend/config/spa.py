"""Serve the built React admin app (admin_frontend/dist) from this Django process.

Opt-in: `config.urls` registers these routes only when settings.SERVE_ADMIN_FRONTEND is true, and they are appended
AFTER every existing route, so /api/, /admin-api/ and /admin/manage/uploads/ behave exactly as before. The routes
are read-only (GET/HEAD), never leave ADMIN_FRONTEND_DIST, and never turn an unknown API path into the SPA page.
"""
import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404
from django.urls import re_path
from django.views.decorators.http import require_safe

# Explicit types: on Windows mimetypes reads the registry, which can label .js as text/plain and break module scripts.
CONTENT_TYPES = {
    ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml", ".json": "application/json", ".map": "application/json", ".webmanifest": "application/manifest+json",
    ".png": "image/png", ".ico": "image/x-icon", ".webp": "image/webp", ".woff": "font/woff", ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
IMMUTABLE = "public, max-age=31536000, immutable"  # /assets/* names carry a content hash
SHORT = "public, max-age=3600"
NO_CACHE = "no-cache"  # index.html must always be revalidated so a rebuild is picked up


def _dist_root():
    return Path(settings.ADMIN_FRONTEND_DIST).resolve()


def _safe_file(relative):
    """The file under the build folder, or None for anything missing, outside it, or not a plain relative path."""
    if not relative or "\\" in relative or "\x00" in relative or ":" in relative:
        return None
    root = _dist_root()
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root)
    except (ValueError, OSError):
        return None
    return candidate if candidate.is_file() else None


def _file_response(path, cache_control):
    content_type = CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    response = FileResponse(path.open("rb"), content_type=content_type)
    response["Cache-Control"] = cache_control
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_safe
def spa_asset(request, path):
    found = _safe_file("assets/" + path)
    if found is None:
        raise Http404  # a missing hashed asset is a 404, never the SPA page
    return _file_response(found, IMMUTABLE)


@require_safe
def spa_root_file(request, name):
    found = _safe_file(name)
    if found is None or found.name.lower() == "index.html":
        raise Http404
    return _file_response(found, SHORT)


@require_safe
def spa_index(request, path=""):
    found = _safe_file("index.html")
    if found is None:
        raise Http404
    return _file_response(found, NO_CACHE)


# Prefixes that belong to Django. `admin` is reserved with its sub-paths too (/admin, /admin/manage/...).
_RESERVED = r"(?!(?:api|admin-api|admin|static|assets)(?:/|$))"


def spa_urlpatterns():
    return [
        re_path(r"^assets/(?P<path>.+)$", spa_asset, name="admin-spa-asset"),
        re_path(r"^(?P<name>[\w.-]+\.(?:svg|ico|png|webp|txt|json|webmanifest))$", spa_root_file, name="admin-spa-root-file"),
        re_path(r"^" + _RESERVED + r"(?P<path>.*)$", spa_index, name="admin-spa-index"),
    ]
