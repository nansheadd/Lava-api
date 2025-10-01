from __future__ import annotations


import traceback

from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile

import os


from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

import base64
import requests
from requests import RequestException
from urllib.parse import urljoin, urlparse

from .converters import docx_to_markdown_and_html
from .wordpress_client import (
    WordPressAuthenticationError,
    WordPressClient,
    WordPressExportError,
    export_subscriptions_csv,
    fetch_subscriptions_page,
)

app = FastAPI(title="LavaTools", version="0.1.0")


_allowed_origins = os.getenv("ALLOWED_ORIGINS")
if _allowed_origins:
    allow_origins = [origin.strip() for origin in _allowed_origins.split(",") if origin.strip()]
else:
    allow_origins = [
        "https://lavatools-web.fly.dev",  # frontend prod
        "http://localhost:5173",  # local dev
        "http://127.0.0.1:5173",  # local dev loopback
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConvertResponse(BaseModel):
    markdown: str
    html: str
    engine: str
    stats: dict


class WordPressSubscriptionsRequest(BaseModel):
    base_url: str
    username: str
    password: str


class WordPressSubscriptionsResponse(BaseModel):
    base_url: str
    admin_path: str
    html: str


class WordPressCredentials(BaseModel):
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
                return _normalise_base_url(candidate)
        raise ValueError("Merci de renseigner l'URL de votre site WordPress.")

    def resolved_username(self) -> str:
        username = self.username or self.user
        if not username:
            raise ValueError("Merci de renseigner l'identifiant WordPress.")
        return username

    def resolved_password(self) -> str:
        password = self.application_password or self.password
        if not password:
            raise ValueError("Merci de renseigner le mot de passe/application password WordPress.")
        return password


class WordPressConnectRequest(WordPressCredentials):
    pass


class WordPressConnectResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    username: Optional[str] = None
    display_name: Optional[str] = Field(None, alias="displayName")
    url: str

    model_config = ConfigDict(populate_by_name=True)


class WordPressPublishRequest(WordPressCredentials):
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
        allowed = {"draft", "publish"}
        if value not in allowed:
            raise ValueError(
                "Le statut WordPress doit être 'draft' ou 'publish'."
            )
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


class WordPressSubscriptionsExportRequest(WordPressCredentials):
    pass


class WordPressSubscriptionsExportResponse(BaseModel):
    filename: Optional[str] = None
    content_type: Optional[str] = Field(None, alias="contentType")
    data: str

    model_config = ConfigDict(populate_by_name=True)


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
    except Exception as exc:  # pragma: no cover - defensive guard
        traceback.print_exc()  # utile en dev pour voir la stack dans les logs
        raise HTTPException(500, detail=f"Conversion échouée: {exc}") from exc

    stats = {
        "bytes": len(data),
        "chars_md": len(md),
        "chars_html": len(html),
        "engine": engine,
    }
    return ConvertResponse(markdown=md, html=html, engine=engine, stats=stats)


@app.post(
    "/wordpress/subscriptions",
    response_model=WordPressSubscriptionsResponse,
    summary="Fetch the WooCommerce subscriptions admin page HTML.",
)
async def wordpress_subscriptions(
    payload: WordPressSubscriptionsRequest,
) -> WordPressSubscriptionsResponse:
    """Authenticate against WordPress and return the subscriptions page HTML."""

    client = WordPressClient(payload.base_url)

    try:
        html = fetch_subscriptions_page(
            base_url=client.base_url,
            username=payload.username,
            password=payload.password,
            client=client,
        )
    except WordPressAuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    subscriptions_path = (
        "wp-admin/admin.php?page=wf_subscriptions_csv_im_ex&tab=subscriptions"
    )
    return WordPressSubscriptionsResponse(
        base_url=client.base_url,
        admin_path=subscriptions_path,
        html=html,
    )


@app.post(
    "/wordpress/subscriptions/export",
    response_model=WordPressSubscriptionsExportResponse,
    summary="Export WooCommerce subscriptions as a CSV file.",
)
async def wordpress_subscriptions_export(
    payload: WordPressSubscriptionsExportRequest,
) -> WordPressSubscriptionsExportResponse:
    try:
        base_url = payload.normalised_base_url()
        username = payload.resolved_username()
        password = payload.resolved_password()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    client = WordPressClient(base_url)

    try:
        content, filename, content_type = export_subscriptions_csv(
            base_url=client.base_url,
            username=username,
            password=password,
            client=client,
        )
    except WordPressAuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except WordPressExportError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except requests.HTTPError as exc:  # pragma: no cover - network failure details
        status_code = exc.response.status_code if exc.response else 502
        raise HTTPException(
            status_code=status_code,
            detail="L'export WooCommerce a échoué.",
        ) from exc

    encoded = base64.b64encode(content).decode("ascii")
    return WordPressSubscriptionsExportResponse(
        filename=filename,
        content_type=content_type,
        data=encoded,
    )


def _normalise_base_url(raw_url: str) -> str:
    raw_url = raw_url.strip()
    if not raw_url:
        raise ValueError("Merci de renseigner l'URL de votre site WordPress.")

    parsed = urlparse(raw_url)
    if not parsed.scheme:
        raw_url = f"https://{raw_url}"
        parsed = urlparse(raw_url)

    if not parsed.netloc:
        raise ValueError(
            "L'URL fournie n'est pas valide. Exemple attendu: https://monsite.com"
        )

    if not raw_url.endswith("/"):
        raw_url = f"{raw_url}/"

    return raw_url


def _wordpress_error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "message": message, "error": message},
    )


def _parse_wordpress_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or "La requête WordPress a échoué."

    if isinstance(payload, dict):
        return (
            payload.get("message")
            or payload.get("error")
            or payload.get("detail")
            or "La requête WordPress a échoué."
        )
    return "La requête WordPress a échoué."


def _wordpress_auth_request(
    method: str,
    url: str,
    username: str,
    password: str,
    *,
    json_payload: Optional[dict] = None,
) -> requests.Response:
    try:
        response = requests.request(
            method,
            url,
            auth=(username, password),
            json=json_payload,
            timeout=15,
            headers={"Accept": "application/json"},
        )
    except RequestException as exc:  # pragma: no cover - network failure
        raise HTTPException(
            status_code=502,
            detail=f"Connexion à WordPress impossible: {exc}",
        ) from exc
    return response


@app.post("/wordpress/connect", response_model=WordPressConnectResponse)
def wordpress_connect(payload: WordPressConnectRequest):
    try:
        base_url = payload.normalised_base_url()
        username = payload.resolved_username()
        password = payload.resolved_password()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    api_url = urljoin(base_url, "wp-json/wp/v2/users/me")
    response = _wordpress_auth_request("GET", api_url, username, password)

    if response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            data = {}

        display_name = None
        if isinstance(data, dict):
            display_name = data.get("name") or data.get("slug")

        message = (
            f"Connexion réussie à WordPress pour l'utilisateur {display_name or username}."
        )
        site_url = base_url.rstrip("/")
        return WordPressConnectResponse(
            success=True,
            message=message,
            username=username,
            display_name=display_name,
            url=site_url,
        )

    error_message = _parse_wordpress_error(response)
    return _wordpress_error(response.status_code, error_message)


@app.post("/wordpress/publish", response_model=WordPressPublishResponse)
def wordpress_publish(payload: WordPressPublishRequest):
    try:
        base_url = payload.normalised_base_url()
        username = payload.resolved_username()
        password = payload.resolved_password()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    content = payload.html or payload.content or payload.markdown
    if not content:
        raise HTTPException(
            status_code=400,
            detail="Merci de fournir du contenu (HTML ou Markdown) à publier.",
        )

    post_payload = {
        "title": payload.title or "",
        "status": payload.status,
        "content": content,
    }

    if payload.slug:
        post_payload["slug"] = payload.slug

    posts_url = urljoin(base_url, "wp-json/wp/v2/posts")
    response = _wordpress_auth_request(
        "POST", posts_url, username, password, json_payload=post_payload
    )

    if response.status_code in {200, 201}:
        try:
            data = response.json()
        except ValueError:
            data = {}

        link = None
        permalink = None
        status = None
        post_id = None
        if isinstance(data, dict):
            link = data.get("link")
            permalink = data.get("permalink") or link
            status = data.get("status")
            post_id = data.get("id")

        message = "Article publié avec succès sur WordPress."
        return WordPressPublishResponse(
            success=True,
            message=message,
            link=link or permalink,
            url=link or permalink,
            permalink=permalink,
            post_id=post_id,
            status=status,
        )

    error_message = _parse_wordpress_error(response)
    return _wordpress_error(response.status_code, error_message)
