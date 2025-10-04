from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import (
    StaleElementReferenceException,
    ElementClickInterceptedException,
    ElementNotInteractableException,
    TimeoutException,
)
import time, os

# --- Config ---
USERNAME = "chahid.lorenzo@protonmail.com"
PASSWORD = "hsp507R@yc@"  # en prod: os.environ["WP_PASS"]

WP_LOGIN_URL = "https://lavamedia.be/wp-login.php"
PLUGIN_URL   = "https://lavamedia.be/wp-admin/admin.php?page=wt_import_export_for_woo"

# Dossier de téléchargement (évite la popup du navigateur)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "exports")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

options = ChromeOptions()
# options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-gpu")
options.add_argument("--window-size=1920,1080")

chrome_binary = os.environ.get("CHROME_BINARY")
if chrome_binary:
    options.binary_location = chrome_binary

# Préférences pour télécharger automatiquement les CSV sans popup
prefs = {
    "download.default_directory": DOWNLOAD_DIR,
    "download.prompt_for_download": False,
    "download.directory_upgrade": True,
    "safebrowsing.enabled": True,
}
options.add_experimental_option("prefs", prefs)

service = ChromeService()
try:
    driver = webdriver.Chrome(service=service, options=options)
except Exception:
    # Fallback propre vers Firefox si Chrome n'est pas disponible sur l'environnement courant
    from selenium.webdriver.firefox.service import Service as FirefoxService
    from selenium.webdriver.firefox.options import Options as FirefoxOptions

    ff_options = FirefoxOptions()
    ff_options.set_preference("browser.download.folderList", 2)
    ff_options.set_preference("browser.download.dir", DOWNLOAD_DIR)
    ff_options.set_preference("browser.download.useDownloadDir", True)
    ff_options.set_preference("browser.download.manager.showWhenStarting", False)
    ff_options.set_preference("browser.download.alwaysOpenPanel", False)
    ff_options.set_preference(
        "browser.helperApps.neverAsk.saveToDisk",
        "text/csv,application/csv,application/vnd.ms-excel,application/octet-stream,text/plain",
    )
    ff_options.set_preference("pdfjs.disabled", True)

    driver = webdriver.Firefox(service=FirefoxService(), options=ff_options)
wait = WebDriverWait(driver, 45)  # délais plus longs pour site lent

# --------- Helpers robustes ----------
def sleep_s(low=0.8, high=1.4):
    time.sleep((low + high) / 2)

def wait_dom_ready(timeout=45):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

def wait_ajax_idle(timeout=45):
    # Attend que jQuery (s'il existe) soit idle, sinon passe.
    end = time.time() + timeout
    while time.time() < end:
        try:
            active = driver.execute_script(
                "return (window.jQuery && jQuery.active) ? jQuery.active : 0;"
            )
            if not active:
                return
        except Exception:
            return
        time.sleep(0.25)

def scroll_into_view(el):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)

def is_interactable(el):
    try:
        if not el.is_displayed() or not el.is_enabled():
            return False
        disabled_attr = el.get_attribute("disabled")
        if disabled_attr in ("true", "disabled"):
            return False
        return driver.execute_script("""
            const el = arguments[0];
            const st = window.getComputedStyle(el);
            if (st.visibility === 'hidden' || st.display === 'none' || st.pointerEvents === 'none') return false;
            const r = el.getBoundingClientRect();
            return (r.width > 0 && r.height > 0);
        """, el)
    except StaleElementReferenceException:
        return False

def safe_click(el):
    try:
        el.click()
    except (ElementClickInterceptedException, ElementNotInteractableException):
        driver.execute_script("arguments[0].click();", el)

def click_with_retries(locator, tries=8, gap=1.2):
    last_exc = None
    for attempt in range(tries):
        try:
            el = wait.until(EC.presence_of_element_located(locator))
            WebDriverWait(driver, 20, ignored_exceptions=(StaleElementReferenceException,)).until(
                lambda d: is_interactable(el)
            )
            scroll_into_view(el)
            safe_click(el)
            return True
        except Exception as e:
            last_exc = e
            time.sleep(gap)
    if last_exc:
        raise last_exc
    return False

def set_checkbox(cb, should_check=True):
    try:
        selected = cb.is_selected()
    except StaleElementReferenceException:
        selected = False
    if should_check != selected:
        scroll_into_view(cb); safe_click(cb)

def open_select2_near_label(label_text):
    # ouvre la Select2 alignée avec le label (ici "Statuts")
    el = wait.until(EC.presence_of_element_located((
        By.XPATH,
        f"//label[normalize-space()='{label_text}']/ancestor::tr//span[contains(@class,'select2-selection')]"
    )))
    scroll_into_view(el)
    safe_click(el)
    # attendre que le dropdown soit ouvert
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".select2-container--open .select2-results__options")))
    return el

def clear_select2_tokens():
    # supprime toutes les valeurs déjà choisies
    removes = driver.find_elements(By.CSS_SELECTOR, "span.select2-selection__choice__remove")
    for r in list(removes)[::-1]:
        safe_click(r)
        time.sleep(0.2)

def select_status_active():
    """
    Sélectionne 'Active' dans la Select2 des Statuts (par ID se terminant en '-wc-active'
    ou par texte 'Active/Actif'), puis vérifie le chip.
    """
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".select2-container--open .select2-results__options")))
    except TimeoutException:
        open_select2_near_label("Statuts")

    for attempt in range(3):
        # Essai par ID suffixe '-wc-active'
        try:
            opt = wait.until(EC.element_to_be_clickable((
                By.CSS_SELECTOR, ".select2-container--open li.select2-results__option[id$='-wc-active']"
            )))
            scroll_into_view(opt)
            safe_click(opt)
            time.sleep(0.6)
        except Exception:
            # Fallback par texte 'Active'/'Actif'
            try:
                opt2 = wait.until(EC.element_to_be_clickable((
                    By.XPATH, "//li[contains(@class,'select2-results__option')][normalize-space()='Active' or normalize-space()='Actif']"
                )))
                scroll_into_view(opt2)
                safe_click(opt2)
                time.sleep(0.6)
            except Exception:
                pass

        chips = driver.find_elements(By.CSS_SELECTOR, "li.select2-selection__choice")
        if any("active" in (c.text or c.get_attribute("title") or "").lower() or
               "actif"  in (c.text or c.get_attribute("title") or "").lower()
               for c in chips):
            return True

        # rouvre pour la prochaine tentative
        try:
            safe_click(driver.find_element(By.CSS_SELECTOR, "span.select2-selection"))
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".select2-container--open .select2-results__options")))
        except Exception:
            open_select2_near_label("Statuts")

    raise TimeoutException("Impossible de sélectionner le statut 'Active/Actif'.")

def wait_for_download_link(max_wait_seconds=600):
    """
    Attend jusqu'à 10 minutes que la box 'Export file processing completed' apparaisse
    et renvoie le lien 'Télécharger le fichier'.
    """
    end = time.time() + max_wait_seconds
    locator_box = (By.CSS_SELECTOR, "div.wt_iew_loader_info_box")
    locator_link = (By.CSS_SELECTOR, "div.wt_iew_loader_info_box a.button.button-secondary[href*='wt_iew_export_download=true']")

    while time.time() < end:
        try:
            # La box "processing completed"
            box = wait.until(EC.presence_of_element_located(locator_box))
            # Le lien de téléchargement
            link = driver.find_element(*locator_link)
            if link.is_displayed():
                return link
        except Exception:
            pass
        time.sleep(1.0)

    raise TimeoutException("Lien 'Télécharger le fichier' non apparu dans le délai imparti.")

# --------------- Flow ----------------
try:
    # 1) Login WP
    driver.get(WP_LOGIN_URL)
    wait_dom_ready(); wait_ajax_idle()
    user_input = wait.until(EC.element_to_be_clickable((By.ID, "user_login")))
    pass_input = driver.find_element(By.ID, "user_pass")
    user_input.clear(); user_input.send_keys(USERNAME)
    pass_input.clear(); pass_input.send_keys(PASSWORD)
    click_with_retries((By.ID, "wp-submit"))
    wait.until(EC.presence_of_element_located((By.ID, "wpadminbar")))
    sleep_s()

    # 2) Page plugin
    driver.get(PLUGIN_URL)
    wait_dom_ready(); wait_ajax_idle()
    sleep_s(1.0, 1.6)

    # 3) Sélectionner "Subscription"
    sub_card_locator = (By.CSS_SELECTOR, 'div.wt_iew_post-type-card[data-post-type="subscription"]')
    sub_card = wait.until(EC.presence_of_element_located(sub_card_locator))
    if "selected" not in (sub_card.get_attribute("class") or ""):
        click_with_retries(sub_card_locator, tries=10, gap=1.0)
        WebDriverWait(driver, 30).until(
            lambda d: "selected" in d.find_element(*sub_card_locator).get_attribute("class")
        )
    sleep_s(1.2, 1.8); wait_ajax_idle()

    # 4) Étape 2: Méthode d'export
    step2_locator = (By.CSS_SELECTOR, 'button.wt_iew_export_action_btn[data-action="method_export"]')
    click_with_retries(step2_locator, tries=12, gap=1.3)
    wait_dom_ready(); wait_ajax_idle(); sleep_s()

    # 5) "Nouveau export"
    new_radio = wait.until(EC.presence_of_element_located((By.ID, "wt_iew_export_new_export")))
    set_checkbox(new_radio, True)
    sleep_s()

    # 6) Étape 3: Filtrer
    step3_locator = (By.CSS_SELECTOR, 'button.wt_iew_export_action_btn[data-action="filter"]')
    click_with_retries(step3_locator, tries=10, gap=1.0)
    wait_dom_ready(); wait_ajax_idle(); sleep_s(1.0, 1.6)

    # 7) Filtre Statuts = Active
    open_select2_near_label("Statuts")
    clear_select2_tokens()
    select_status_active()
    sleep_s()

    # 8) Étape 4: Mapping
    step4_locator = (By.CSS_SELECTOR, 'button.wt_iew_export_action_btn[data-action="mapping"]')
    click_with_retries(step4_locator, tries=10, gap=1.0)
    wait_dom_ready(); wait_ajax_idle(); sleep_s(1.0, 1.6)

    # 9) (Dé)sélectionner les colonnes
    desired = {
        "subscription_status",
        "shipping_address_1",
        "shipping_postcode",
        "shipping_city",
        "shipping_country",
        "shipping_first_name",
        "shipping_last_name",
    }
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table.wt-iew-mapping-tb")))
    checkboxes = driver.find_elements(By.CSS_SELECTOR, "table.wt-iew-mapping-tb input.columns_key")
    for cb in checkboxes:
        key = cb.get_attribute("value") or ""
        set_checkbox(cb, key in desired)
    sleep_s()

    # 10) Méta supplémentaire: seulement meta:Language
    try:
        meta_header = wait.until(EC.element_to_be_clickable((
            By.XPATH, "//*[contains(@class,'meta_mapping_box_hd')][contains(., 'Méta supplémentaire')]"
        )))
        scroll_into_view(meta_header)
        safe_click(meta_header)
        time.sleep(0.6)
    except Exception:
        pass
    lang_cb = wait.until(EC.presence_of_element_located((
        By.XPATH, "//label[normalize-space()='meta:Language']/preceding::input[@type='checkbox'][1]"
    )))
    set_checkbox(lang_cb, True)
    other_meta_cbs = driver.find_elements(
        By.XPATH, "//label[starts-with(normalize-space(),'meta:') and normalize-space()!='meta:Language']/preceding::input[@type='checkbox'][1]"
    )
    for cb in other_meta_cbs:
        set_checkbox(cb, False)
    sleep_s()

    # 11) Étape 5: Options avancées
    step5_locator = (By.CSS_SELECTOR, 'button.wt_iew_export_action_btn[data-action="advanced"]')
    click_with_retries(step5_locator, tries=10, gap=1.0)
    wait_dom_ready(); wait_ajax_idle(); sleep_s(1.0, 1.6)

    # 12) Cliquer sur "Exporter"
    export_btn_locator = (By.CSS_SELECTOR, "button.wt_iew_export_action_btn.iew_export_btn[data-action='export'][data-action-type='non-step']")
    click_with_retries(export_btn_locator, tries=15, gap=1.5)
    print("⏳ Export en cours… j'attends le lien de téléchargement…")
    # Tu peux voir un loader; on attend que la box 'Export file processing completed' s'affiche

    # 13) Attendre l'apparition du lien "Télécharger le fichier"
    download_link_el = wait_for_download_link(max_wait_seconds=600)  # jusqu'à 10 minutes si nécessaire
    href = download_link_el.get_attribute("href")
    print("✅ Lien de téléchargement prêt:", href)

    # 14) Déclencher le téléchargement
    # -> on navigue directement vers le lien pour forcer le téléchargement (évite popup/grab du nouvel onglet)
    driver.get(href)
    print(f"📥 Téléchargement lancé. Le fichier devrait apparaître dans: {DOWNLOAD_DIR}")

    # 15) Laisser la fenêtre ouverte pour vérification manuelle
    input("Fenêtre ouverte. Appuie Entrée ici quand tu as terminé…")

except Exception as e:
    print("❌ Erreur:", e)

finally:
    # pas de driver.quit() pour te laisser la main
    pass
