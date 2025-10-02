from __future__ import annotations

from urllib.parse import urlparse

class WordPressAuthenticationError(Exception):
    """Raised when login to WordPress admin fails."""
    pass

class WordPressExportError(Exception):
    """Raised when the export flow fails after login."""
    pass

def normalise_base_url(raw_url: str) -> str:
    raw_url = (raw_url or "").strip()
    if not raw_url:
        raise ValueError("Merci de renseigner l'URL de votre site WordPress.")

    parsed = urlparse(raw_url)
    if not parsed.scheme:
        raw_url = f"https://{raw_url}"
        parsed = urlparse(raw_url)

    if not parsed.netloc:
        raise ValueError("L'URL fournie n'est pas valide. Exemple: https://monsite.com")

    path = parsed.path or "/"
    lowered = path.lower()

    for marker in ("/wp-admin", "/wp-login.php"):
        idx = lowered.find(marker)
        if idx != -1:
            path = path[:idx] or "/"
            lowered = path.lower()
            break

    if not path.endswith("/"):
        path = f"{path}/"

    normalised = parsed._replace(path=path, params="", query="", fragment="")
    return normalised.geturl()
