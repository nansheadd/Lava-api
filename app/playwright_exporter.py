"""Automation helpers powered by Playwright.

This module provides a thin wrapper around the synchronous Playwright API in
order to drive a headless browser through the WordPress authentication flow and
trigger the WooCommerce subscriptions export.  Using a real browser makes the
automation much closer to the behaviour a human would experience, which is
particularly useful on hosts that deploy aggressive bot mitigation on the admin
area.
"""

from __future__ import annotations

import mimetypes
import os
import re
import tempfile
from typing import Optional, Tuple
from urllib.parse import urljoin

from playwright.sync_api import (
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .wordpress_client import WordPressAuthenticationError, WordPressExportError

_EXPORT_PATH = "wp-admin/admin.php?page=wf_subscriptions_csv_im_ex&tab=subscriptions"


def export_subscriptions_csv_with_playwright(
    base_url: str,
    username: str,
    password: str,
    *,
    headless: bool = True,
    browser: str = "chromium",
    timeout: int = 60_000,
) -> Tuple[bytes, Optional[str], Optional[str]]:
    """Download the WooCommerce subscriptions CSV using Playwright."""

    login_url = urljoin(base_url, "wp-login.php")
    export_url = urljoin(base_url, _EXPORT_PATH)

    try:
        with sync_playwright() as playwright:
            try:
                browser_type = getattr(playwright, browser)
            except AttributeError as exc:  # pragma: no cover - defensive guard
                raise WordPressExportError(
                    f"Navigateur Playwright inconnu: '{browser}'."
                ) from exc

            launched_browser = browser_type.launch(headless=headless)
            context = launched_browser.new_context(accept_downloads=True)

            try:
                page = context.new_page()
                _login_with_playwright(page, login_url, username, password, timeout)
                content, filename, content_type = _download_export(
                    page, export_url, timeout
                )
            finally:
                context.close()
                launched_browser.close()

            return content, filename, content_type

    except PlaywrightTimeoutError as exc:
        raise WordPressExportError(
            "Le navigateur automatisé n'a pas réussi à finaliser l'export WooCommerce."
        ) from exc
    except PlaywrightError as exc:
        message = str(exc)
        if "executable doesn't exist" in message.lower():
            message = (
                "Playwright ne trouve pas le binaire du navigateur. "
                "Exécutez `playwright install chromium` puis réessayez."
            )
        else:
            message = f"L'automatisation Playwright a échoué: {message}"
        raise WordPressExportError(message) from exc


def _login_with_playwright(
    page: Page,
    login_url: str,
    username: str,
    password: str,
    timeout: int,
) -> None:
    page.goto(login_url, wait_until="domcontentloaded", timeout=timeout)

    page.fill('input[name="log"]', username)
    page.fill('input[name="pwd"]', password)

    remember_checkbox = page.locator('input[name="rememberme"]')
    if remember_checkbox.count():
        try:
            remember_checkbox.first.check()
        except PlaywrightError:
            pass  # optionnel, ne bloque pas si la case n'est pas cliquable

    submit_locator = page.locator('input[name="wp-submit"]')
    if submit_locator.count():
        submit_locator.first.click()
    else:
        page.keyboard.press("Enter")

    try:
        page.wait_for_url("**/wp-admin/**", timeout=timeout)
    except PlaywrightTimeoutError as exc:
        if "wp-login.php" in page.url:
            message = _extract_login_error(page)
            raise WordPressAuthenticationError(message) from exc
        raise


def _extract_login_error(page: Page) -> str:
    error_locator = page.locator("#login_error")
    if error_locator.count():
        try:
            text = error_locator.first.inner_text().strip()
            if text:
                return text
        except PlaywrightError:
            pass
    return (
        "Connexion WordPress échouée. Merci de vérifier l'identifiant, le mot "
        "de passe et les éventuelles étapes de validation supplémentaires."
    )


def _download_export(
    page: Page,
    export_url: str,
    timeout: int,
) -> Tuple[bytes, Optional[str], Optional[str]]:
    page.goto(export_url, wait_until="domcontentloaded", timeout=timeout)
    page.wait_for_load_state("networkidle", timeout=timeout)

    locator = _locate_export_button(page, timeout)

    try:
        with page.expect_download(timeout=timeout) as download_info:
            if locator is not None:
                locator.click()
            else:
                page.keyboard.press("Enter")
    except PlaywrightTimeoutError as exc:
        message = (
            "Impossible de trouver le bouton d'export WooCommerce dans l'interface."
            if locator is None
            else "Le téléchargement du fichier d'export n'a pas démarré."
        )
        raise WordPressExportError(message) from exc

    download = download_info.value

    temp_file = tempfile.NamedTemporaryFile(delete=False)
    temp_file.close()

    try:
        download.save_as(temp_file.name)
        with open(temp_file.name, "rb") as handle:
            content = handle.read()
    finally:
        try:
            os.unlink(temp_file.name)
        except OSError:
            pass

    filename = download.suggested_filename
    content_type = getattr(download, "content_type", None)

    if not content_type and filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            content_type = guessed

    return content, filename, content_type


def _locate_export_button(page: Page, timeout: int) -> Optional[Locator]:
    candidates = [
        page.get_by_role("button", name=re.compile("export", re.I)),
        page.get_by_role("link", name=re.compile("export", re.I)),
        page.locator('input[type="submit"][value*="Export"]'),
        page.locator('input[type="submit"][value*="export"]'),
        page.locator('input[type="submit"][value*="Exporter"]'),
        page.locator('button[name*="export"]'),
        page.locator('button[value*="export"]'),
        page.locator('button:has-text("Export")'),
        page.locator('button:has-text("Exporter")'),
    ]

    for locator in candidates:
        try:
            if locator.count() == 0:
                continue
        except PlaywrightError:
            continue

        try:
            wait_timeout = max(timeout // 2, 1_000)
            locator.first.wait_for(state="visible", timeout=wait_timeout)
            return locator.first
        except PlaywrightError:
            continue

    return None


__all__ = ["export_subscriptions_csv_with_playwright"]

