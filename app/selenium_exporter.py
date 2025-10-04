# app/selenium_exporter.py
from __future__ import annotations

import os
import shutil
import time
from typing import Tuple, Optional, Callable, Dict, Any
from urllib.parse import urljoin

import requests

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChService
from selenium.webdriver.chrome.options import Options as ChOptions
from selenium.webdriver.firefox.service import Service as FxService
from selenium.webdriver.firefox.options import Options as FxOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
    ElementNotInteractableException,
)

from .wordpress_client import (
    WordPressAuthenticationError,
    WordPressExportError,
    normalise_base_url,
)

# ------------------------------------------------------------------------------
# Constantes
# ------------------------------------------------------------------------------
PLUGIN_PATH = "wp-admin/admin.php?page=wt_import_export_for_woo"

DESIRED_COLUMNS = {
    "subscription_status",
    "shipping_address_1",
    "shipping_postcode",
    "shipping_city",
    "shipping_country",
    "shipping_first_name",
    "shipping_last_name",
}
DEFAULT_TIMEOUT = 45


# ------------------------------------------------------------------------------
# Helpers Selenium (attentes / interactions)
# ------------------------------------------------------------------------------
def wait_dom_ready(driver, timeout: int = DEFAULT_TIMEOUT):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def wait_ajax_idle(driver, timeout: int = DEFAULT_TIMEOUT):
    end = time.time() + timeout
    while time.time() < end:
        try:
            active = driver.execute_script(
                "return (window.jQuery && jQuery.active) ? jQuery.active : 0;"
            )
            if not active:
                return
        except Exception:
            # jQuery pas présent -> on ne bloque pas
            return
        time.sleep(0.25)


def scroll_into_view(driver, el):
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center', inline:'nearest'});", el
    )


def is_interactable(driver, el) -> bool:
    try:
        if not el.is_displayed() or not el.is_enabled():
            return False
        if el.get_attribute("disabled") in ("true", "disabled"):
            return False
        return driver.execute_script(
            """
            const el = arguments[0];
            const st = window.getComputedStyle(el);
            if (st.visibility === 'hidden' || st.display === 'none' || st.pointerEvents === 'none') return false;
            const r = el.getBoundingClientRect();
            return (r.width > 0 && r.height > 0);
            """,
            el,
        )
    except StaleElementReferenceException:
        return False


def safe_click(driver, el):
    try:
        el.click()
    except (ElementClickInterceptedException, ElementNotInteractableException):
        driver.execute_script("arguments[0].click();", el)


def click_with_retries(driver, locator, tries: int = 10, gap: float = 1.2):
    last_exc = None
    for _ in range(tries):
        try:
            el = WebDriverWait(driver, DEFAULT_TIMEOUT).until(
                EC.presence_of_element_located(locator)
            )
            WebDriverWait(
                driver, 20, ignored_exceptions=(StaleElementReferenceException,)
            ).until(lambda d: is_interactable(driver, el))
            scroll_into_view(driver, el)
            safe_click(driver, el)
            return True
        except Exception as e:
            last_exc = e
            time.sleep(gap)
    if last_exc:
        raise last_exc
    return False


def set_checkbox(driver, cb, should_check: bool = True):
    try:
        selected = cb.is_selected()
    except StaleElementReferenceException:
        selected = False
    if should_check != selected:
        scroll_into_view(driver, cb)
        safe_click(driver, cb)


# ------------------------------------------------------------------------------
# WebDrivers (Chrome par défaut, Firefox en fallback)
# ------------------------------------------------------------------------------

def make_chrome(headless: bool) -> webdriver.Chrome:
    opts = ChOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")

    # Localiser Chromium si un binaire custom est fourni
    chrome_binary = os.environ.get("CHROME_BINARY")
    for candidate in (
        chrome_binary,
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ):
        if candidate and os.path.exists(candidate):
            opts.binary_location = candidate
            break

    # Premier essai : laisser Selenium Manager résoudre le driver Chrome
    # (présent à partir de Selenium 4.6+)
    try:
        return webdriver.Chrome(options=opts)
    except Exception as first_error:
        last_error = first_error

    # Fallback manuel : rechercher un chromedriver local si Selenium Manager
    # n'a pas pu le télécharger
    driver_path = os.environ.get("CHROMEDRIVER_PATH")
    candidates = (
        driver_path,
        shutil.which("chromedriver"),
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    )

    for candidate in candidates:
        if not candidate:
            continue
        if not os.path.exists(candidate) or os.path.isdir(candidate):
            continue
        if not os.access(candidate, os.X_OK):
            continue
        driver_path = candidate
        break

    if driver_path and os.path.exists(driver_path):
        service = ChService(executable_path=driver_path)
        return webdriver.Chrome(service=service, options=opts)

    # Aucun driver disponible -> remonter l'erreur initiale pour déclencher un fallback
    raise last_error

def make_firefox(headless: bool) -> webdriver.Firefox:
    opts = FxOptions()
    if headless:
        opts.add_argument("-headless")

    # Help find the ESR binary on Debian
    if os.path.exists("/usr/bin/firefox-esr"):
        opts.binary_location = "/usr/bin/firefox-esr"

    # Disable content sandbox (avoids EPERM userns issue in Docker)
    # (env MOZ_DISABLE_CONTENT_SANDBOX=1 is also set in Dockerfile; this is belt & suspenders)
    opts.set_preference("security.sandbox.content.level", 0)
    opts.set_preference("network.proxy.type", 0)

    # Point explicitly to geckodriver we installed
    service = FxService(executable_path=os.environ.get("GECKODRIVER_PATH", "/usr/local/bin/geckodriver"))
    return webdriver.Firefox(service=service, options=opts)

def build_driver(browser: str = "chrome", headless: bool = True):
    b = (browser or os.getenv("SELENIUM_BROWSER", "chrome")).strip().lower()
    last_error: Optional[Exception] = None

    if b in ("chrome", "chromium", "google-chrome", ""):
        try:
            return make_chrome(headless)
        except Exception as exc:
            last_error = exc

    # Firefox reste disponible en fallback si Chrome n'est pas utilisable
    try:
        return make_firefox(headless)
    except Exception:
        if last_error:
            raise last_error
        raise


# ------------------------------------------------------------------------------
# Étapes métier
# ------------------------------------------------------------------------------
def login_wp_admin(driver, base_url: str, username: str, password: str):
    login_url = urljoin(base_url, "wp-login.php")
    driver.get(login_url)
    wait_dom_ready(driver)
    wait_ajax_idle(driver)

    try:
        user_input = WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            EC.element_to_be_clickable((By.ID, "user_login"))
        )
    except TimeoutException as exc:
        raise WordPressAuthenticationError(
            "Impossible d'afficher la page de connexion WordPress."
        ) from exc

    pass_input = driver.find_element(By.ID, "user_pass")
    user_input.clear()
    user_input.send_keys(username)
    pass_input.clear()
    pass_input.send_keys(password)
    click_with_retries(driver, (By.ID, "wp-submit"))

    # Présence de la barre admin = connecté
    try:
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, "wpadminbar"))
        )
    except TimeoutException as exc:
        raise WordPressAuthenticationError(
            "Identifiants WordPress refusés ou 2FA requis."
        ) from exc


def select_subscription_and_go_to_step2(driver, base_url: str):
    plugin_url = urljoin(base_url, PLUGIN_PATH)
    driver.get(plugin_url)
    wait_dom_ready(driver)
    wait_ajax_idle(driver)
    time.sleep(1.2)

    sub_card_locator = (
        By.CSS_SELECTOR,
        'div.wt_iew_post-type-card[data-post-type="subscription"]',
    )
    sub_card = WebDriverWait(driver, DEFAULT_TIMEOUT).until(
        EC.presence_of_element_located(sub_card_locator)
    )
    if "selected" not in (sub_card.get_attribute("class") or ""):
        click_with_retries(driver, sub_card_locator, tries=12, gap=1.3)
        WebDriverWait(driver, 30).until(
            lambda d: "selected"
            in d.find_element(*sub_card_locator).get_attribute("class")
        )
    time.sleep(1.2)
    wait_ajax_idle(driver)

    step2_locator = (
        By.CSS_SELECTOR,
        'button.wt_iew_export_action_btn[data-action="method_export"]',
    )
    click_with_retries(driver, step2_locator, tries=12, gap=1.3)
    wait_dom_ready(driver)
    wait_ajax_idle(driver)
    time.sleep(0.8)


def choose_new_export_and_go_to_step3(driver):
    new_radio = WebDriverWait(driver, DEFAULT_TIMEOUT).until(
        EC.presence_of_element_located((By.ID, "wt_iew_export_new_export"))
    )
    set_checkbox(driver, new_radio, True)
    time.sleep(0.3)

    step3_locator = (
        By.CSS_SELECTOR,
        'button.wt_iew_export_action_btn[data-action="filter"]',
    )
    click_with_retries(driver, step3_locator, tries=10, gap=1.0)
    wait_dom_ready(driver)
    wait_ajax_idle(driver)
    time.sleep(1.0)


def set_status_active_then_go_to_step4(driver):
    # Ouvrir Select2 à côté du label "Statuts"
    def open_select2_near_label(label_text: str):
        el = WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    f"//label[normalize-space()='{label_text}']/ancestor::tr//span[contains(@class,'select2-selection')]",
                )
            )
        )
        scroll_into_view(driver, el)
        safe_click(driver, el)
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, ".select2-container--open .select2-results__options")
            )
        )
        return el

    def clear_select2_tokens():
        removes = driver.find_elements(
            By.CSS_SELECTOR, "span.select2-selection__choice__remove"
        )
        for r in list(removes)[::-1]:
            safe_click(driver, r)
            time.sleep(0.2)

    def select_status_active():
        # Essai par id ...-wc-active sinon par texte "Active"/"Actif"
        for _ in range(3):
            try:
                opt = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable(
                        (
                            By.CSS_SELECTOR,
                            ".select2-container--open li.select2-results__option[id$='-wc-active']",
                        )
                    )
                )
                scroll_into_view(driver, opt)
                safe_click(driver, opt)
                time.sleep(0.6)
            except Exception:
                try:
                    opt2 = WebDriverWait(driver, 10).until(
                        EC.element_to_be_clickable(
                            (
                                By.XPATH,
                                "//li[contains(@class,'select2-results__option')][normalize-space()='Active' or normalize-space()='Actif']",
                            )
                        )
                    )
                    scroll_into_view(driver, opt2)
                    safe_click(driver, opt2)
                    time.sleep(0.6)
                except Exception:
                    pass

            chips = driver.find_elements(
                By.CSS_SELECTOR, "li.select2-selection__choice"
            )
            if any(
                "active" in (c.text or c.get_attribute("title") or "").lower()
                or "actif" in (c.text or c.get_attribute("title") or "").lower()
                for c in chips
            ):
                return True

            # rouvre pour retenter
            try:
                sel = driver.find_element(By.CSS_SELECTOR, "span.select2-selection")
                safe_click(driver, sel)
                WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located(
                        (
                            By.CSS_SELECTOR,
                            ".select2-container--open .select2-results__options",
                        )
                    )
                )
            except Exception:
                open_select2_near_label("Statuts")

        raise TimeoutException("Impossible de sélectionner le statut 'Active/Actif'.")

    open_select2_near_label("Statuts")
    clear_select2_tokens()
    select_status_active()
    time.sleep(0.5)

    step4_locator = (
        By.CSS_SELECTOR,
        'button.wt_iew_export_action_btn[data-action="mapping"]',
    )
    click_with_retries(driver, step4_locator, tries=10, gap=1.0)
    wait_dom_ready(driver)
    wait_ajax_idle(driver)
    time.sleep(1.0)


def mapping_keep_only_desired_and_meta_language_then_step5(driver):
    WebDriverWait(driver, DEFAULT_TIMEOUT).until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, "table.wt-iew-mapping-tb")
        )
    )
    checkboxes = driver.find_elements(
        By.CSS_SELECTOR, "table.wt-iew-mapping-tb input.columns_key"
    )
    # Correction du sélecteur CSS (ancienne faute de frappe)
    if not checkboxes:
        checkboxes = driver.find_elements(
            By.CSS_SELECTOR, "table.wt-iew-mapping-tb input.columns_key"
        )

    for cb in checkboxes:
        key = cb.get_attribute("value") or ""
        set_checkbox(driver, cb, key in DESIRED_COLUMNS)
    time.sleep(0.4)

    # Ouvrir "Méta supplémentaire" si replié
    try:
        meta_header = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//*[contains(@class,'meta_mapping_box_hd')][contains(., 'Méta supplémentaire')]",
                )
            )
        )
        scroll_into_view(driver, meta_header)
        safe_click(driver, meta_header)
        time.sleep(0.6)
    except Exception:
        pass

    # Cocher uniquement meta:Language
    lang_cb = WebDriverWait(driver, DEFAULT_TIMEOUT).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//label[normalize-space()='meta:Language']/preceding::input[@type='checkbox'][1]",
            )
        )
    )
    set_checkbox(driver, lang_cb, True)

    other_meta_cbs = driver.find_elements(
        By.XPATH,
        "//label[starts-with(normalize-space(),'meta:') and normalize-space()!='meta:Language']/preceding::input[@type='checkbox'][1]",
    )
    for cb in other_meta_cbs:
        set_checkbox(driver, cb, False)
    time.sleep(0.4)

    step5_locator = (
        By.CSS_SELECTOR,
        'button.wt_iew_export_action_btn[data-action="advanced"]',
    )
    click_with_retries(driver, step5_locator, tries=10, gap=1.0)
    wait_dom_ready(driver)
    wait_ajax_idle(driver)
    time.sleep(0.8)


def click_export_and_wait_for_download_link(
    driver, base_url: str, max_wait_s: int = 600
) -> str:
    export_btn_locator = (
        By.CSS_SELECTOR,
        "button.wt_iew_export_action_btn.iew_export_btn[data-action='export'][data-action-type='non-step']",
    )
    click_with_retries(driver, export_btn_locator, tries=15, gap=1.5)

    # Attendre la box "Export file processing completed" + lien
    end = time.time() + max_wait_s
    locator_link = (
        By.CSS_SELECTOR,
        "div.wt_iew_loader_info_box a.button.button-secondary[href*='wt_iew_export_download=true']",
    )
    while time.time() < end:
        try:
            link = driver.find_element(*locator_link)
            if link.is_displayed():
                href = link.get_attribute("href")
                if href:
                    return href
        except Exception:
            pass
        time.sleep(1.0)

    raise WordPressExportError("Lien de téléchargement non apparu après délai maximal.")


def download_with_selenium_cookies(
    driver, download_url: str
) -> Tuple[bytes, str, str]:
    """
    Télécharge le fichier via requests en réutilisant la session (cookies Selenium).
    Retourne (content, filename, content_type).
    """
    sess = requests.Session()
    for c in driver.get_cookies():
        sess.cookies.set(
            c.get("name"),
            c.get("value"),
            domain=c.get("domain"),
            path=c.get("path", "/"),
        )

    resp = sess.get(download_url, allow_redirects=True, timeout=120)
    if resp.status_code != 200:
        raise WordPressExportError(f"Échec du téléchargement ({resp.status_code}).")

    filename = "subscriptions_export.csv"
    cd = resp.headers.get("Content-Disposition") or resp.headers.get(
        "content-disposition"
    ) or ""
    if "filename=" in cd:
        part = cd.split("filename=")[-1].strip().strip('"')
        if part:
            filename = part

    content_type = resp.headers.get("Content-Type") or "text/csv"
    return resp.content, filename, content_type


# ------------------------------------------------------------------------------
# API publique
# ------------------------------------------------------------------------------
def export_subscriptions_csv_with_selenium(
    base_url: str,
    username: str,
    password: str,
    *,
    browser: str = "chrome",
    headless: bool = True,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Tuple[bytes, str, str]:
    """
    Lance un navigateur headless, exécute le flux d'export, puis télécharge le CSV via la session.
    Retourne (content_bytes, filename, content_type).
    """

    def emit(ev_type: str, message: str, **extra):
        if progress_cb:
            try:
                progress_cb({"type": ev_type, "message": message, **extra})
            except Exception:
                pass

    base_url = normalise_base_url(base_url)
    driver = build_driver(browser, headless)
    try:
        emit("progress", "Connexion à WordPress…", step="login", pct=5)
        login_wp_admin(driver, base_url, username, password)
        emit("progress", "Connecté à WordPress.", step="login_ok", pct=10)

        emit("progress", "Ouverture du plugin Import/Export…", step="open_plugin", pct=15)
        select_subscription_and_go_to_step2(driver, base_url)
        emit("progress", "Étape 2: méthode d’export OK.", step="step2_ok", pct=25)

        emit("progress", "Sélection 'Nouveau export' → Étape 3…", step="step3", pct=30)
        choose_new_export_and_go_to_step3(driver)
        emit("progress", "Étape 3: filtres chargés.", step="step3_ok", pct=35)

        emit("progress", "Filtre 'Statuts = Active'…", step="filter_active", pct=40)
        set_status_active_then_go_to_step4(driver)
        emit("progress", "Statut 'Active' appliqué → Étape 4.", step="step4_ok", pct=55)

        emit("progress", "Mapping colonnes + meta:Language…", step="mapping", pct=65)
        mapping_keep_only_desired_and_meta_language_then_step5(driver)
        emit("progress", "Étape 5: options avancées.", step="step5_ok", pct=75)

        emit("progress", "Clique sur 'Exporter'…", step="export_click", pct=80)
        export_link = click_export_and_wait_for_download_link(driver, base_url, max_wait_s=600)
        emit("progress", "Lien de téléchargement prêt.", step="export_link", pct=90)

        emit("progress", "Téléchargement du CSV…", step="download", pct=95)
        content, filename, content_type = download_with_selenium_cookies(driver, export_link)
        emit("progress", "Téléchargement terminé.", step="download_ok", pct=100)

        return content, filename, content_type

    except WordPressAuthenticationError as exc:
        emit("error", "Échec d’authentification WordPress.", step="login_error")
        raise
    except TimeoutException as exc:
        emit("error", f"Timeout pendant l’export: {exc}", step="timeout")
        raise WordPressExportError(f"Timeout pendant l'export: {exc}") from exc
    except Exception as exc:
        emit("error", f"Export échoué: {exc}", step="unexpected")
        raise WordPressExportError(f"Export échoué: {exc}") from exc
    finally:
        try:
            driver.quit()
        except Exception:
            pass
