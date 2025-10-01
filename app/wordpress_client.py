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
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests


class WordPressAuthenticationError(RuntimeError):
    """Raised when the WordPress login flow cannot be completed."""


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
        admin_response = self.session.get(location or urljoin(self.base_url, "wp-admin/"))
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


__all__ = [
    "WordPressClient",
    "WordPressAuthenticationError",
    "fetch_subscriptions_page",
]

