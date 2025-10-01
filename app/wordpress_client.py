"""Utilities for programmatic interactions with a WordPress instance.

This module exposes a thin wrapper above ``requests.Session`` that makes it
possible to authenticate against the traditional WordPress login form and then
request pages from the admin interface.  It is intentionally lightweight so it
can easily be reused inside scripts, notebooks or future API endpoints.

Example
-------
>>> from app.wordpress_client import WordPressClient
>>> client = WordPressClient("https://example.com")
>>> client.login("admin", "password")
>>> html = client.fetch_admin_page("/wp-admin/about.php")

The :class:`WordPressClient` will raise informative exceptions whenever the
authentication flow fails, which makes it easier to diagnose credential or
permission issues without having to inspect raw HTTP responses manually.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


class WordPressAuthenticationError(RuntimeError):
    """Raised when the WordPress login flow cannot be completed."""


class WordPressExportError(RuntimeError):
    """Raised when the WooCommerce export flow cannot be completed."""


@dataclass
class WordPressClient:
    """Helper used to authenticate and interact with a WordPress instance.

    Parameters
    ----------
    base_url:
        The root URL of the WordPress site (e.g. ``"https://example.com"``).
    session:
        Optional pre-configured :class:`requests.Session`.  When omitted a new
        session is created automatically.
    """

    base_url: str
    session: Optional[requests.Session] = None

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

        # Some hosting providers apply additional security checks based on the
        # ``User-Agent`` header.  When ``requests`` uses its default value the
        # login flow can be rejected with an interstitial page that shows the
        # "You are now logging in to WordPress" message.  Spoof a modern
        # browser UA to ensure WordPress treats the session like a real user
        # agent.  Users can still override the header on the provided session
        # when necessary.
        self.session.headers.setdefault(
            "User-Agent",
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )

        parsed = urlparse(self.base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(
                "base_url must be an absolute URL such as 'https://example.com'"
            )

        # Normalise the base URL so later ``urljoin`` calls behave predictably.
        if not self.base_url.endswith("/"):
            self.base_url = f"{self.base_url}/"

    @property
    def _login_url(self) -> str:
        return urljoin(self.base_url, "wp-login.php")

    def login(self, username: str, password: str) -> None:
        """Authenticate the session against WordPress.

        Parameters
        ----------
        username:
            WordPress username or email address.
        password:
            WordPress password.

        Raises
        ------
        WordPressAuthenticationError
            If the credentials are invalid or WordPress does not grant access
            to the admin area for the provided account.
        """

        # Fetch the login page so WordPress sets its "test cookie".
        response = self.session.get(self._login_url)
        response.raise_for_status()

        # WordPress expects the test cookie to be present on the POST request.
        parsed = urlparse(self.base_url)
        cookie_domain = parsed.hostname or ""
        self.session.cookies.set(
            "wordpress_test_cookie",
            "WP Cookie check",
            domain=cookie_domain,
            path="/",
        )

        payload = {
            "log": username,
            "pwd": password,
            "rememberme": "forever",
            "wp-submit": "Log In",
            "redirect_to": urljoin(self.base_url, "wp-admin/"),
            "testcookie": "1",
        }

        login_response = self.session.post(
            self._login_url,
            data=payload,
            allow_redirects=False,
        )

        # Successful authentication returns a 302 redirect to the admin area.
        if login_response.status_code not in {301, 302}:
            raise WordPressAuthenticationError(
                "Login failed: unexpected status code "
                f"{login_response.status_code}"
            )

        location = login_response.headers.get("Location", "")
        if "wp-login.php" in location:
            # WordPress redirects back to the login form on failure.
            raise WordPressAuthenticationError(
                "Login failed: WordPress redirected back to the login page. "
                "Please verify the username, password and 2FA requirements."
            )

        # Follow the redirect to ensure the session has proper admin cookies.
        admin_destination = location or urljoin(self.base_url, "wp-admin/")
        admin_url = urljoin(self.base_url, admin_destination)
        admin_response = self.session.get(admin_url)
        admin_response.raise_for_status()

    def fetch_admin_page(self, path: str) -> str:
        """Fetch an admin page and return its HTML content.

        Parameters
        ----------
        path:
            Absolute URL or path relative to the site root.  Examples include
            ``"/wp-admin/about.php"`` or a full URL such as
            ``"https://example.com/wp-admin/about.php"``.

        Returns
        -------
        str
            The HTML content of the requested page.
        """

        url = urljoin(self.base_url, path)
        response = self.session.get(url)
        response.raise_for_status()
        return response.text


def fetch_subscriptions_page(
    base_url: str,
    username: str,
    password: str,
    client: Optional[WordPressClient] = None,
) -> str:
    """Convenience helper that logs in and returns the WooCommerce page HTML.

    This helper is tailored for the "WooCommerce → Import Export Suite →
    Subscriptions" page.  The exact slug can vary depending on the plugin
    version, so the function targets the common default provided by WebToffee's
    Import Export Suite.
    """

    if client is None:
        client = WordPressClient(base_url)

    client.login(username, password)

    subscriptions_path = (
        "wp-admin/admin.php?page=wf_subscriptions_csv_im_ex&tab=subscriptions"
    )
    return client.fetch_admin_page(subscriptions_path)


def export_subscriptions_csv(
    base_url: str,
    username: str,
    password: str,
    *,
    client: Optional[WordPressClient] = None,
) -> Tuple[bytes, Optional[str], Optional[str]]:
    """Trigger the WooCommerce subscriptions export and return the CSV bytes."""

    if client is None:
        client = WordPressClient(base_url)

    client.login(username, password)

    subscriptions_path = (
        "wp-admin/admin.php?page=wf_subscriptions_csv_im_ex&tab=subscriptions"
    )
    page_url = urljoin(client.base_url, subscriptions_path)
    html = client.fetch_admin_page(subscriptions_path)

    action_url, payload = _prepare_export_request(html, page_url)

    response = client.session.post(
        action_url,
        data=payload,
        headers={"Referer": page_url},
        stream=True,
    )
    response.raise_for_status()

    filename = _extract_filename(response.headers.get("Content-Disposition", ""))
    content_type = response.headers.get("Content-Type")
    return response.content, filename, content_type


def _prepare_export_request(html: str, page_url: str) -> Tuple[str, dict]:
    soup = BeautifulSoup(html, "lxml")

    for form in soup.find_all("form"):
        submit = _find_export_button(form)
        if submit is None:
            continue

        action = form.get("action") or page_url
        action_url = urljoin(page_url, action)
        payload = _extract_form_fields(form, submit)
        return action_url, payload

    raise WordPressExportError(
        "Impossible de trouver le formulaire d'export dans la page WordPress."
    )


def _find_export_button(form):
    candidates = []
    candidates.extend(form.find_all("input", attrs={"type": "submit"}))
    candidates.extend(form.find_all("button"))

    for candidate in candidates:
        label = ""
        if candidate.name == "input":
            label = candidate.get("value", "")
        else:
            label = candidate.get_text(strip=True)

        name = candidate.get("name", "")
        identifier = " ".join(filter(None, [label, name])).lower()
        if "export" in identifier:
            return candidate

    return None


def _extract_form_fields(form, submit_button) -> dict:
    data = {}

    for input_tag in form.find_all("input"):
        name = input_tag.get("name")
        if not name:
            continue

        input_type = (input_tag.get("type") or "").lower()

        if input_tag is submit_button:
            data[name] = input_tag.get("value", "")
            continue

        if input_type in {"submit", "button", "image"}:
            continue

        if input_type in {"checkbox", "radio"}:
            if input_tag.has_attr("checked"):
                data[name] = input_tag.get("value", "on")
            continue

        if input_type == "file":
            continue

        data[name] = input_tag.get("value", "")

    if submit_button.name == "button":
        name = submit_button.get("name")
        if name:
            data[name] = (
                submit_button.get("value") or submit_button.get_text(strip=True)
            )

    for textarea in form.find_all("textarea"):
        name = textarea.get("name")
        if name and name not in data:
            data[name] = textarea.text or ""

    for select in form.find_all("select"):
        name = select.get("name")
        if not name or name in data:
            continue

        option = select.find("option", selected=True)
        if option is None:
            option = select.find("option")

        if option is not None:
            data[name] = option.get("value") or option.get_text(strip=True)

    return data


def _extract_filename(content_disposition: str) -> Optional[str]:
    if not content_disposition:
        return None

    parts = [part.strip() for part in content_disposition.split(";")]

    for part in parts:
        if part.lower().startswith("filename*="):
            _, value = part.split("=", 1)
            if "''" in value:
                _, value = value.split("''", 1)
            return unquote(value.strip('"'))
        if part.lower().startswith("filename="):
            _, value = part.split("=", 1)
            return value.strip('"')

    return None


__all__ = [
    "WordPressClient",
    "WordPressAuthenticationError",
    "fetch_subscriptions_page",
    "export_subscriptions_csv",
    "WordPressExportError",
]

