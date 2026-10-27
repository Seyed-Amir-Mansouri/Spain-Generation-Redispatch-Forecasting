from django.conf import settings


def asset_version(request):
    """Add the newest change time of the static files, so browsers reload them after an edit."""
    folder = settings.BASE_DIR / "dashboard" / "static" / "dashboard"
    return {"asset_version": max(int(path.stat().st_mtime) for path in folder.iterdir() if path.is_file())}
