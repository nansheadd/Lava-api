from __future__ import annotations

import asyncio
import base64
import os
import traceback
from typing import Optional
from urllib.parse import urljoin

import requests
from requests import RequestException
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .converters import docx_to_markdown_and_html
from .selenium_exporter import export_subscriptions_csv_with_selenium
from .wordpress_client import normalise_base_url


# -----------------------------------------------------------------------------
# App & config
# -----------------------------------------------------------------------------

app = FastAPI(title="LavaTools", version="1.1.0")


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"", "1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return default


_DEFAULT_SELENIUM_BROWSER = os.getenv("SELENIUM_BROWSER", "firefox")
_DEFAULT_SELENIUM_HEADLESS = _env_flag("SELENIUM_HEADLESS", True)

allow_all = os.getenv("CORS_ALLOW_ALL") == "1"
_allowed_origins = os.getenv("ALLOWED_ORIGINS", "")

if allow_all:
    allow_origins = ["*"]
else:
    if _allowed_origins:
        allow_origins = [o.strip() for o in _allowed_origins.split(",") if o.strip()]
    else:
        allow_origins = ["https://lavatools-web.fly.dev", "http://localhost:5173", "http://127.0.0.1:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------

class ConvertResponse(BaseModel):
    markdown: str
    html: str
    engine: str
    stats: dict


class WordPressConnectRequest(BaseModel):
    site_url: Optional[str] = Field(None, alias="siteUrl")
    url: Optional[str] = None
    base_url: Optional[str] = Field(None, alias="baseUrl")
    username: Optional[str] = None
    user: Optional[str] = None
    application_password: Optional[str] = Field(None, alias="applicationPassword")
    password: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)

    def normalised_base_url(self) -> str:
        for candidate in (self.base_url, self.site_url, self.url):
            if candidate:
                return normalise_base_url(candidate)
        raise ValueError("Merci de renseigner l'URL de votre site WordPress.")

    def resolved_username(self) -> str:
        u = self.username or self.user
        if not u:
            raise ValueError("Merci de renseigner l'identifiant WordPress.")
        return u

    def resolved_api_password(self) -> str:
        pw = self.application_password or self.password
        if not pw:
            raise ValueError("Merci de renseigner un Application Password ou mot de passe WordPress.")
        return pw


class WordPressConnectResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    username: Optional[str] = None
    display_name: Optional[str] = Field(None, alias="displayName")
    url: str

    model_config = ConfigDict(populate_by_name=True)


class WordPressPublishRequest(WordPressConnectRequest):
    title: Optional[str] = None
    slug: Optional[str] = None
    status: str = "draft"
    markdown: Optional[str] = None
    html: Optional[str] = None
    content: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        value = value or "draft"
        if value not in {"draft", "publish"}:
            raise ValueError("Le statut WordPress doit être 'draft' ou 'publish'.")
        return value


class WordPressPublishResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    link: Optional[str] = None
    url: Optional[str] = None
    permalink: Optional[str] = None
    post_id: Optional[int] = Field(None, alias="postId")
    status: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class WordPressAdminCreds(BaseModel):
    site_url: Optional[str] = Field(None, alias="siteUrl")
    url: Optional[str] = None
    base_url: Optional[str] = Field(None, alias="baseUrl")
    username: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)

    def normalised_base_url(self) -> str:
        for candidate in (self.base_url, self.site_url, self.url):
            if candidate:
                return normalise_base_url(candidate)
        raise ValueError("Merci de renseigner l'URL de votre site WordPress.")

    def resolved_username(self) -> str:
        u = self.username or self.user
        if not u:
            raise ValueError("Merci de renseigner l'identifiant WordPress.")
        return u

    def resolved_admin_password(self) -> str:
        if not self.password:
            raise ValueError("Merci de renseigner le mot de passe WordPress.")
        return self.password


class WordPressSubscriptionsRequest(WordPressAdminCreds):
    pass


class WordPressSubscriptionsResponse(BaseModel):
    base_url: str = Field(..., alias="baseUrl")
    admin_path: str = Field(..., alias="adminPath")
    html: str = ""

    model_config = ConfigDict(populate_by_name=True)


class WordPressSubscriptionsExportRequest(WordPressAdminCreds):
    browser: Optional[str] = None
    headless: Optional[bool] = None


class WordPressSubscriptionsExportResponse(BaseModel):
    filename: Optional[str] = None
    content_type: Optional[str] = Field(None, alias="contentType")
    data: str

    model_config = ConfigDict(populate_by_name=True)


# -----------------------------------------------------------------------------
# Helpers HTTP WordPress
# -----------------------------------------------------------------------------

def _wordpress_error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "message": message, "error": message},
    )


def _wordpress_auth_request(
    method: str,
    url: str,
    username: str,
    password: str,
    *,
    json_payload: Optional[dict] = None,
) -> requests.Response:
    try:
        resp = requests.request(
            method,
            url,
            auth=(username, password),
            json=json_payload,
            timeout=30,
            headers={"Accept": "application/json"},
        )
    except RequestException as exc:
        raise HTTPException(502, detail=f"Connexion à WordPress impossible: {exc}") from exc
    return resp


def _parse_wordpress_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or "La requête WordPress a échoué."
    if isinstance(payload, dict):
        return payload.get("message") or payload.get("error") or payload.get("detail") or "La requête WordPress a échoué."
    return "La requête WordPress a échoué."


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/convert", response_model=ConvertResponse)
async def convert(file: UploadFile = File(...)) -> ConvertResponse:
    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(400, detail="Merci d'envoyer un fichier .docx")

    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, detail="Fichier trop volumineux (>10 Mo)")

    try:
        md, html, engine = docx_to_markdown_and_html(data)
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(500, detail=f"Conversion échouée: {exc}") from exc

    stats = {"bytes": len(data), "chars_md": len(md), "chars_html": len(html), "engine": engine}
    return ConvertResponse(markdown=md, html=html, engine=engine, stats=stats)


@app.post("/wordpress/connect", response_model=WordPressConnectResponse)
def wordpress_connect(payload: WordPressConnectRequest):
    try:
        base_url = payload.normalised_base_url()
        username = payload.resolved_username()
        password = payload.resolved_api_password()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    api_url = urljoin(base_url, "wp-json/wp/v2/users/me")
    resp = _wordpress_auth_request("GET", api_url, username, password)

    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        display_name = data.get("name") or data.get("slug") if isinstance(data, dict) else None
        site_url = base_url.rstrip("/")
        return WordPressConnectResponse(
            success=True,
            message=f"Connexion réussie à WordPress pour l'utilisateur {display_name or username}.",
            username=username,
            display_name=display_name,
            url=site_url,
        )

    error_message = _parse_wordpress_error(resp)
    return _wordpress_error(resp.status_code, error_message)


@app.post("/wordpress/publish", response_model=WordPressPublishResponse)
def wordpress_publish(payload: WordPressPublishRequest):
    try:
        base_url = payload.normalised_base_url()
        username = payload.resolved_username()
        password = payload.resolved_api_password()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    content = payload.html or payload.content or payload.markdown
    if not content:
        raise HTTPException(400, detail="Merci de fournir du contenu (HTML ou Markdown) à publier.")

    post_payload = {"title": payload.title or "", "status": payload.status, "content": content}
    if payload.slug:
        post_payload["slug"] = payload.slug

    api_url = urljoin(base_url, "wp-json/wp/v2/posts")
    resp = _wordpress_auth_request("POST", api_url, username, password, json_payload=post_payload)

    if resp.status_code in {200, 201}:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        link = data.get("link") if isinstance(data, dict) else None
        permalink = data.get("permalink") if isinstance(data, dict) else None
        status = data.get("status") if isinstance(data, dict) else None
        post_id = data.get("id") if isinstance(data, dict) else None
        message = "Article publié avec succès sur WordPress."
        return WordPressPublishResponse(
            success=True,
            message=message,
            link=link or permalink,
            url=link or permalink,
            permalink=permalink or link,
            post_id=post_id,
            status=status,
        )

    error_message = _parse_wordpress_error(resp)
    return _wordpress_error(resp.status_code, error_message)


@app.post(
    "/wordpress/subscriptions",
    response_model=WordPressSubscriptionsResponse,
    summary="Retourne le chemin admin pour les abonnements WooCommerce (aperçu/lien).",
)
def wordpress_subscriptions(payload: WordPressSubscriptionsRequest) -> WordPressSubscriptionsResponse:
    """
    Pas d'API key WooCommerce ici : on renvoie le lien admin vers le plugin Import/Export.
    L'HTML est laissé vide (le frontend affichera surtout le lien 'Ouvrir la page').
    """
    try:
        base_url = payload.normalised_base_url()
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc

    subscriptions_path = "wp-admin/admin.php?page=wt_import_export_for_woo"
    return WordPressSubscriptionsResponse(baseUrl=base_url, adminPath=subscriptions_path, html="")


@app.post(
    "/wordpress/subscriptions/export",
    response_model=WordPressSubscriptionsExportResponse,
    summary="Export WooCommerce subscriptions (CSV) via Selenium (HTTP sync).",
)
def wordpress_subscriptions_export_http(payload: WordPressSubscriptionsExportRequest):
    """
    Route HTTP synchrone (optionnelle). Le WebSocket est recommandé pour le suivi en temps réel.
    """
    try:
        base_url = payload.normalised_base_url()
        username = payload.resolved_username()
        password = payload.resolved_admin_password()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    headless = _DEFAULT_SELENIUM_HEADLESS if payload.headless is None else bool(payload.headless)
    browser = (payload.browser or _DEFAULT_SELENIUM_BROWSER).strip() or _DEFAULT_SELENIUM_BROWSER

    try:
        content, filename, content_type = export_subscriptions_csv_with_selenium(
            base_url=base_url,
            username=username,
            password=password,
            browser=browser,
            headless=headless,
        )
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Export échoué: {exc}") from exc

    encoded = base64.b64encode(content).decode("ascii")
    return WordPressSubscriptionsExportResponse(filename=filename, content_type=content_type, data=encoded)


@app.websocket("/ws/wordpress/subscriptions/export")
async def ws_wordpress_subscriptions_export(websocket: WebSocket):
    """
    WebSocket de streaming de progression pour l'export Selenium.
    Envoie des événements {"type":"progress","message":..., "pct": int}
    puis un final {"type":"done", "filename":..., "contentType":..., "data": base64_csv}
    """
    await websocket.accept()
    try:
        # 1) On attend le payload initial du client
        first = await websocket.receive_json()
        base_url = (first.get("baseUrl") or first.get("siteUrl") or first.get("url") or "").strip()
        username = (first.get("username") or first.get("user") or "").strip()
        password = (first.get("password") or "").strip()
        browser = (first.get("browser") or _DEFAULT_SELENIUM_BROWSER).strip()
        headless = first.get("headless")
        if headless is None:
            headless = _DEFAULT_SELENIUM_HEADLESS

        if not base_url or not username or not password:
            await websocket.send_json({"type": "error", "message": "Merci de fournir baseUrl, username et password."})
            await websocket.close()
            return

        loop = asyncio.get_running_loop()

        # callback thread-safe pour pousser les événements de progression
        def progress_cb(ev: dict):
            try:
                asyncio.run_coroutine_threadsafe(websocket.send_json(ev), loop)
            except RuntimeError:
                pass

        # 2) Lancer l'export dans un worker thread pour ne pas bloquer l'event loop
        def run_export():
            return export_subscriptions_csv_with_selenium(
                base_url=base_url,
                username=username,
                password=password,
                browser=browser,
                headless=bool(headless),
                progress_cb=progress_cb,
            )

        try:
            content, filename, content_type = await loop.run_in_executor(None, run_export)
        except Exception as exc:
            await websocket.send_json({"type": "error", "message": f"{exc}"})
            await websocket.close()
            return

        # 3) Envoi final avec le CSV encodé
        await websocket.send_json({
            "type": "done",
            "message": "Export terminé.",
            "filename": filename or "woocommerce-subscriptions.csv",
            "contentType": content_type or "text/csv",
            "data": base64.b64encode(content).decode("ascii"),
        })
        await websocket.close()

    except WebSocketDisconnect:
        return
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": f"Erreur WebSocket: {exc}"})
        finally:
            try:
                await websocket.close()
            except Exception:
                pass
