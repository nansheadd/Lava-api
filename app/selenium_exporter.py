"""Automation helpers powered by Selenium.

This module mirrors the behaviour of :mod:`app.playwright_exporter` but uses
Selenium WebDriver instead of Playwright.  While Playwright generally provides
better tooling for headless automation, certain hosting providers apply
JavaScript challenges or bot mitigation that prevent the Playwright browser
from completing the login flow.  Selenium gives us the flexibility to plug in
alternative drivers (Chrome, Chromium, Firefox, …) which can help bypass those
restrictions.
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import tempfile
import time
from typing import Optional, Tuple
from urllib.parse import urljoin

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .wordpress_client import WordPressAuthenticationError, WordPressExportError

_EXPORT_PATH = "wp-admin/admin.php?page=wf_subscriptions_csv_im_ex&tab=subscriptions"


def export_subscriptions_csv_with_selenium(
    base_url: str,
    username: str,
    password: str,
    *,
    browser: str = "chromium",
    headless: bool = True,
    timeout: int = 60,
) -> Tuple[bytes, Optional[str], Optional[str]]:
    """Download the WooCommerce subscriptions CSV using Selenium."""

    login_url = urljoin(base_url, "wp-login.php")
    export_url = urljoin(base_url, _EXPORT_PATH)

    download_dir = tempfile.mkdtemp(prefix="selenium-download-")

    try:
        driver = _launch_browser(browser, headless=headless, download_dir=download_dir)
    except WebDriverException as exc:  # pragma: no cover - defensive guard
        message = str(exc)
        if "executable needs to be in PATH" in message:
            message = (
                "Selenium ne trouve pas le navigateur. "
                "Merci d'installer le driver (chromedriver/geckodriver)."
            )
        raise WordPressExportError(message) from exc

    driver.set_page_load_timeout(timeout)

    try:
        wait = WebDriverWait(driver, timeout)
        _login_with_selenium(driver, wait, login_url, username, password)
        content, filename, content_type = _download_export(
            driver, wait, export_url, download_dir, timeout
        )
        return content, filename, content_type
    except TimeoutException as exc:
        raise WordPressExportError(
            "Le navigateur automatisé n'a pas réussi à finaliser l'export WooCommerce."
        ) from exc
    except WebDriverException as exc:
        raise WordPressExportError(f"L'automatisation Selenium a échoué: {exc}") from exc
    finally:
        try:
            driver.quit()
        finally:
            shutil.rmtree(download_dir, ignore_errors=True)


def _launch_browser(
    browser: str,
    *,
    headless: bool,
    download_dir: str,
) -> WebDriver:
    browser = (browser or "chromium").strip().lower()

    if browser in {"chrome", "chromium"}:
        options = ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        prefs = {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
        }
        options.add_experimental_option("prefs", prefs)
        service = ChromeService()
        return webdriver.Chrome(options=options, service=service)

    if browser in {"firefox", "gecko"}:
        options = FirefoxOptions()
        if headless:
            options.add_argument("-headless")

        options.set_preference("browser.download.folderList", 2)
        options.set_preference("browser.download.dir", download_dir)
        options.set_preference(
            "browser.helperApps.neverAsk.saveToDisk",
            "text/csv,application/csv,application/octet-stream",
        )
        options.set_preference("pdfjs.disabled", True)

        service = FirefoxService()
        return webdriver.Firefox(options=options, service=service)

    raise WordPressExportError(f"Navigateur Selenium inconnu: '{browser}'.")


def _login_with_selenium(
    driver: WebDriver,
    wait: WebDriverWait,
    login_url: str,
    username: str,
    password: str,
) -> None:
    driver.get(login_url)

    username_field = wait.until(
        EC.presence_of_element_located((By.NAME, "log"))
    )
    password_field = wait.until(
        EC.presence_of_element_located((By.NAME, "pwd"))
    )

    username_field.clear()
    username_field.send_keys(username)
    password_field.clear()
    password_field.send_keys(password)

    try:
        remember_me = driver.find_element(By.ID, "rememberme")
        if remember_me.is_displayed() and not remember_me.is_selected():
            remember_me.click()
    except NoSuchElementException:
        pass  # Optionnel

    try:
        submit_button = driver.find_element(By.ID, "wp-submit")
        if submit_button.is_enabled():
            submit_button.click()
        else:
            password_field.send_keys(Keys.ENTER)
    except NoSuchElementException:
        password_field.send_keys(Keys.ENTER)

    try:
        wait.until(EC.url_contains("/wp-admin"))
    except TimeoutException as exc:
        if "wp-login.php" in driver.current_url:
            message = _extract_login_error(driver)
            raise WordPressAuthenticationError(message) from exc
        raise


def _extract_login_error(driver: WebDriver) -> str:
    try:
        error = driver.find_element(By.ID, "login_error")
    except NoSuchElementException:
        return (
            "Connexion WordPress échouée. Merci de vérifier l'identifiant, le mot "
            "de passe et les éventuelles étapes de validation supplémentaires."
        )

    text = error.text.strip()
    if text:
        return text

    return (
        "Connexion WordPress échouée. Merci de vérifier l'identifiant, le mot "
        "de passe et les éventuelles étapes de validation supplémentaires."
    )


def _download_export(
    driver: WebDriver,
    wait: WebDriverWait,
    export_url: str,
    download_dir: str,
    timeout: int,
) -> Tuple[bytes, Optional[str], Optional[str]]:
    driver.get(export_url)
    try:
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    except TimeoutException:
        pass  # La page est peut-être déjà prête

    locator = _locate_export_button(driver)

    if locator is None:
        raise WordPressExportError(
            "Impossible de trouver le bouton d'export WooCommerce dans l'interface."
        )

    locator.click()

    try:
        file_path = _wait_for_download(download_dir, timeout)
    except TimeoutError as exc:  # pragma: no cover - dépend des performances
        raise WordPressExportError(
            "Le téléchargement du fichier d'export n'a pas démarré."
        ) from exc

    with open(file_path, "rb") as handle:
        content = handle.read()

    filename = os.path.basename(file_path)
    content_type = mimetypes.guess_type(filename)[0]

    return content, filename, content_type


def _locate_export_button(driver: WebDriver):
    keywords = ("export", "exporter")
    candidates = driver.find_elements(By.CSS_SELECTOR, "button, a, input[type='submit']")

    for element in candidates:
        try:
            if not element.is_displayed() or not element.is_enabled():
                continue

            text_fragments = [
                element.text or "",
                element.get_attribute("value") or "",
                element.get_attribute("aria-label") or "",
                element.get_attribute("title") or "",
            ]
            text = " ".join(fragment.strip().lower() for fragment in text_fragments)
            if any(keyword in text for keyword in keywords):
                return element
        except WebDriverException:
            continue

    return None


def _wait_for_download(directory: str, timeout: int) -> str:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        files = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if not name.startswith(".")
        ]

        ready_files = [
            path
            for path in files
            if os.path.isfile(path)
            and not path.endswith(".crdownload")
            and not path.endswith(".part")
            and not path.endswith(".tmp")
        ]

        if ready_files:
            ready_files.sort(key=os.path.getmtime, reverse=True)
            return ready_files[0]

        time.sleep(0.5)

    raise TimeoutError("Download timed out")


__all__ = ["export_subscriptions_csv_with_selenium"]

