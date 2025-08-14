from __future__ import annotations

import io
import re
import zipfile
import tempfile
from typing import Dict, Tuple
from html import escape as html_escape

from markdownify import markdownify as html_to_md
import mammoth

# --------- Pandoc (optionnel) ----------
try:
    import pypandoc  # type: ignore
    try:
        _ = pypandoc.get_pandoc_version()
        HAS_PANDOC = True
    except Exception:
        HAS_PANDOC = False
except Exception:
    HAS_PANDOC = False


# ========= Réglages =========

# Shortcode attendu par WordPress
NOTE_TAG = "note"  # mets "NOTE" si ton plugin attend [NOTE]...[/NOTE]

# Souhaité d'après ton exemple: transformer <h1-6> en <strong>…</strong> (sans <p>)
CONVERT_HEADINGS_TO_BARE_STRONG = True


# ========= Helpers génériques =========

def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _convert_headings_to_bare_strong(html: str) -> str:
    """
    Remplace <h1-6>…</h1-6> par <strong>…</strong> (sans paragraphe).
    Si tu préfères <p><strong>…</strong></p>, remplace le return par la ligne commentée.
    """
    def repl(m: re.Match) -> str:
        inner = m.group(2).strip()
        return f"<strong>{inner}</strong>"
        # return f"<p><strong>{inner}</strong></p>"
    return re.sub(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>", repl, html)


def _wrap_note_block(inner_html: str) -> str:
    """Entoure le contenu fourni par [note]…[/note] (ou [NOTE] si NOTE_TAG=NOTE)."""
    return f"[{NOTE_TAG}]{inner_html}[/" + NOTE_TAG + "]"


# ========= Gestion des NOTES =========

def _extract_docx_footnotes(docx_bytes: bytes) -> Dict[str, str]:
    """
    Extrait les notes depuis word/footnotes.xml du DOCX → {id: texte}
    (utile quand Mammoth ne génère pas une section de notes HTML toute faite).
    """
    notes: Dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
            with z.open("word/footnotes.xml") as f:
                xml = f.read().decode("utf-8", "ignore")
        # Chaque <w:footnote w:id="..."> ... </w:footnote>
        for m in re.finditer(r'(?is)<w:footnote[^>]*w:id="(-?\d+)"[^>]*>(.*?)</w:footnote>', xml):
            fid, inner = m.group(1), m.group(2)
            # On ignore les id négatifs (separator/continuation)
            if not fid.isdigit() or int(fid) <= 0:
                continue
            # Concaténation des fragments de texte <w:t>
            text = " ".join(t.strip() for t in re.findall(r'(?is)<w:t[^>]*>(.*?)</w:t>', inner))
            notes[fid] = _normalize_spaces(text)
    except KeyError:
        # pas de footnotes.xml
        pass
    except Exception:
        # on reste silencieux : on échouera gracieusement
        pass
    return notes


def _append_note_block_at_end(html: str, endnotes_html: str) -> str:
    """
    Supprime toute section/div .footnotes préexistante puis ajoute en fin:
    [note] ... [/note]
    """
    # Enlève les blocs natifs "footnotes" si présents
    html_no_section = re.sub(r'(?is)<(section|div)[^>]*class="footnotes"[^>]*>.*?</\1>', "", html)
    block = "\n" + _wrap_note_block(endnotes_html.strip()) + "\n"
    return html_no_section.rstrip() + block


def _wrap_trailing_footnote_ol(html: str) -> str:
    """
    Cherche le DERNIER <ol> qui ressemble à une liste de notes (endnotes) et
    l’enveloppe littéralement avec [note]…[/note].
    Si rien ne correspond, renvoie l'html inchangé.
    """
    # Déjà un bloc [note] présent ? Ne rien faire.
    if re.search(rf"(?is)\[\s*{NOTE_TAG}\s*\]", html):
        return html

    # Capture tous les <ol>…</ol>
    all_ols = list(re.finditer(r"(?is)<ol[^>]*>.*?</ol>", html))
    if not all_ols:
        return html

    def looks_like_footnotes(ol_html: str) -> bool:
        # Heuristiques:
        # 1) IDs/ancres typiques (endnote-X ou post-...-endnote-X)
        if re.search(r'(?i)id="(?:endnote|post-[^"]*endnote)-\d+"', ol_html):
            return True
        if re.search(r'(?i)href="#(?:endnote|post-[^"]*endnote)-\d+"', ol_html):
            return True
        # 2) nombre d'items avec marqueurs [n]
        lis = re.findall(r"(?is)<li[^>]*>(.*?)</li>", ol_html)
        if lis:
            markers = sum(
                1 for li in lis
                if re.search(r"^\s*(?:<p>\s*)?\[\d+\]", li)
                or re.search(r"^\s*<sup>\s*\[\d+\]\s*</sup>", li)
            )
            return markers >= max(2, int(0.5 * len(lis)))
        return False

    # On prend le DERNIER <ol> qui matche les heuristiques
    for m in reversed(all_ols):
        ol_html = m.group(0)
        if looks_like_footnotes(ol_html):
            start, end = m.span()
            wrapped = _wrap_note_block(ol_html)
            return html[:start] + wrapped + html[end:]

    return html


def _ensure_note_block_in_html(html: str, docx_bytes: bytes | None) -> str:
    """
    Politique :
      1) Si une section/div .footnotes existe → la replacer en fin dans [note]…[/note]
      2) Sinon, détecter un <ol> de notes (endnotes) en fin → l’envelopper en [note]…[/note]
      3) Sinon, reconstruire depuis footnotes.xml → ajouter [note]…[/note] en fin
    """
    # 1) Section/div .footnotes
    m = re.search(r'(?is)<(section|div)[^>]*class="footnotes"[^>]*>(.*?)</\1>', html)
    if m:
        inner = m.group(2)  # souvent <hr><ol>…</ol>
        return _append_note_block_at_end(html, inner)

    # 2) <ol> final ressemblant à des notes
    html2 = _wrap_trailing_footnote_ol(html)
    if html2 != html:
        return html2

    # 3) Reconstruction à partir de footnotes.xml (cas Mammoth typique)
    if docx_bytes:
        notes = _extract_docx_footnotes(docx_bytes)
        if notes:
            items = "\n".join(
                f'  <li id="endnote-{i}">{html_escape(txt)} <a href="#endnote-ref-{i}">↑</a></li>'
                for i, txt in notes.items()
            )
            endnotes = f"\n<ol>\n{items}\n</ol>\n"
            return _append_note_block_at_end(html, endnotes)

    # Rien à faire
    return html


# ========= Conversions =========

def _pandoc_docx_to_markdown_and_html(docx_bytes: bytes) -> Tuple[str, str]:
    """Conversion via Pandoc → (markdown, html)"""
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=True) as tmp:
        tmp.write(docx_bytes)
        tmp.flush()
        # Markdown GitHub Flavored + footnotes
        md = pypandoc.convert_file(tmp.name, to="gfm+footnotes", format="docx")
        # HTML de base (on post-traite nous-mêmes footnotes + headings)
        html = pypandoc.convert_file(tmp.name, to="html", format="docx")
    return md, html


def _mammoth_docx_to_html(docx_bytes: bytes) -> str:
    """Conversion via Mammoth → HTML simple"""
    with io.BytesIO(docx_bytes) as f:
        result = mammoth.convert_to_html(f)
    return result.value or ""


def docx_to_markdown_and_html(docx_bytes: bytes) -> Tuple[str, str, str]:
    """
    Retourne (markdown, html, engine).
    - HTML : titres <h1-6> → <strong>…</strong> (sans <p>), bloc final des notes enveloppé par [note]…[/note].
    - Markdown : si Pandoc dispo, on renvoie le MD Pandoc (peut ne pas contenir [note] textuel).
                 sinon, on dérive du HTML post-traité.
    """
    # Tentative Pandoc
    if HAS_PANDOC:
        try:
            md_raw, html_raw = _pandoc_docx_to_markdown_and_html(docx_bytes)

            html_pp = html_raw
            if CONVERT_HEADINGS_TO_BARE_STRONG:
                html_pp = _convert_headings_to_bare_strong(html_pp)

            html_pp = _ensure_note_block_in_html(html_pp, docx_bytes=docx_bytes)

            # On garde le MD Pandoc (meilleure qualité), même s'il ne contient pas les shortcodes [note]
            return md_raw, html_pp, "pandoc"
        except Exception:
            # fallback Mammoth
            pass

    # Fallback Mammoth + markdownify
    html_m = _mammoth_docx_to_html(docx_bytes)
    if CONVERT_HEADINGS_TO_BARE_STRONG:
        html_m = _convert_headings_to_bare_strong(html_m)
    html_m = _ensure_note_block_in_html(html_m, docx_bytes=docx_bytes)

    # Attention: markdownify ne conservera pas les balises [note] comme balises,
    # mais leur contenu oui. Tu colleras donc le HTML côté WordPress pour profiter du shortcode.
    md_m = html_to_md(html_m, strip=['span'], heading_style="ATX")

    return md_m, html_m, "mammoth+markdownify"
