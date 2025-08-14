from __future__ import annotations
import traceback
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from .converters import docx_to_markdown_and_html

app = FastAPI(title="LavaTools", version="0.1.0")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[  # frontend prod
        "http://localhost:5173"           # dev Vite
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

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/convert", response_model=ConvertResponse)
async def convert(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(400, detail="Merci d'envoyer un fichier .docx")

    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, detail="Fichier trop volumineux (>10 Mo)")

    try:
        md, html, engine = docx_to_markdown_and_html(data)
    except Exception as e:
        traceback.print_exc()  # utile en dev pour voir la stack dans les logs
        raise HTTPException(500, detail=f"Conversion échouée: {e}")

    stats = {
        "bytes": len(data),
        "chars_md": len(md),
        "chars_html": len(html),
        "engine": engine,
    }
    return ConvertResponse(markdown=md, html=html, engine=engine, stats=stats)