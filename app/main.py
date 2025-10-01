from __future__ import annotations

import traceback

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .converters import docx_to_markdown_and_html
from .wordpress_client import (
    WordPressAuthenticationError,
    WordPressClient,
    fetch_subscriptions_page,
)

app = FastAPI(title="LavaTools", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://lavatools-web.fly.dev",  # frontend prod
    ],
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
