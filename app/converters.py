from __future__ import annotations

import io
import re
import zipfile
from typing import Dict, Tuple
from html import escape as html_escape

from markdownify import markdownify as html_to_md
import mammoth

# ========= Réglages =========

# Shortcode attendu par WordPress
NOTE_TAG = "note"  # mets "NOTE" si ton plugin attend [NOTE]...[/NOTE]

# Transforme <h1-6> en <strong>…</strong> (sans <p>)
CONVERT_HEADINGS_TO_BARE_STRONG = True


# ========= Helpers génériques =========

def _normalize_spaces(s: str) -> str:
    """Réduit les espaces multiples à un seul et supprime les espaces de début/fin."""
    return re.sub(r"\s+", " ", s).strip()


def _convert_headings_to_bare_strong(html: str) -> str:
    """Remplace <h1-6>…</h1-6> par <strong>…</strong> (sans paragraphe)."""
    def repl(m: re.Match) -> str:
        inner = m.group(2).strip()
        return f"<strong>{inner}</strong>"
    return re.sub(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>", repl, html)


def _wrap_note_block(inner_html: str) -> str:
    """Entoure le contenu fourni par [note]…[/note]."""
    return f"[{NOTE_TAG}]{inner_html}[/{NOTE_TAG}]"


# ========= Gestion des NOTES =========

def _extract_docx_footnotes(docx_bytes: bytes) -> Dict[str, str]:
    """Extrait les notes depuis word/footnotes.xml du DOCX → {id: texte}."""
    notes: Dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z, z.open("word/footnotes.xml") as f:
            xml = f.read().decode("utf-8", "ignore")
        for m in re.finditer(r'(?is)<w:footnote[^>]*w:id="(-?\d+)"[^>]*>(.*?)</w:footnote>', xml):
            fid, inner = m.group(1), m.group(2)
            if not fid.isdigit() or int(fid) <= 0:
                continue
            text = " ".join(t.strip() for t in re.findall(r'(?is)<w:t[^>]*>(.*?)</w:t>', inner))
            notes[fid] = _normalize_spaces(text)
    except Exception:
        pass  # Échoue silencieusement si pas de notes ou erreur de parsing
    return notes


def _append_note_block_at_end(html: str, endnotes_html: str) -> str:
    """Supprime toute section .footnotes existante puis ajoute le bloc [note]…[/note] en fin."""
    html_no_section = re.sub(r'(?is)<(section|div)[^>]*class="footnotes"[^>]*>.*?</\1>', "", html)
    block = "\n" + _wrap_note_block(endnotes_html.strip()) + "\n"
    return html_no_section.rstrip() + block


def _wrap_trailing_footnote_ol(html: str) -> str:
    """Cherche le DERNIER <ol> qui ressemble à une liste de notes et l'enveloppe avec [note]…[/note]."""
    if re.search(rf"(?is)\[\s*{NOTE_TAG}\s*\]", html):
        return html

    all_ols = list(re.finditer(r"(?is)<ol[^>]*>.*?</ol>", html))
    if not all_ols:
        return html

    def looks_like_footnotes(ol_html: str) -> bool:
        if re.search(r'(?i)id="(?:endnote|post-[^"]*endnote)-\d+"', ol_html):
            return True
        if re.search(r'(?i)href="#(?:endnote|post-[^"]*endnote)-\d+"', ol_html):
            return True
        lis = re.findall(r"(?is)<li[^>]*>(.*?)</li>", ol_html)
        if lis:
            markers = sum(1 for li in lis if re.search(r"^\s*(?:<p>\s*)?\[\d+\]", li))
            return markers >= max(2, int(0.5 * len(lis)))
        return False

    for m in reversed(all_ols):
        ol_html = m.group(0)
        if looks_like_footnotes(ol_html):
            start, end = m.span()
            wrapped = _wrap_note_block(ol_html)
            return html[:start] + wrapped + html[end:]

    return html


def _ensure_note_block_in_html(html: str, docx_bytes: bytes) -> str:
    """Politique de gestion des notes de bas de page pour les envelopper dans le shortcode [note]."""
    m = re.search(r'(?is)<(section|div)[^>]*class="footnotes"[^>]*>(.*?)</\1>', html)
    if m:
        return _append_note_block_at_end(html, m.group(2))

    html2 = _wrap_trailing_footnote_ol(html)
    if html2 != html:
        return html2

    notes = _extract_docx_footnotes(docx_bytes)
    if notes:
        items = "\n".join(
            f'  <li id="endnote-{i}">{html_escape(txt)} <a href="#endnote-ref-{i}">↑</a></li>'
            for i, txt in notes.items()
        )
        endnotes_html = f"\n<ol>\n{items}\n</ol>\n"
        return _append_note_block_at_end(html, endnotes_html)

    return html


# ========= Conversion Principale (Mammoth uniquement) =========

def _mammoth_docx_to_html(docx_bytes: bytes) -> str:
    """Conversion via Mammoth → HTML simple."""
    with io.BytesIO(docx_bytes) as f:
        result = mammoth.convert_to_html(f)
    return result.value or ""


def docx_to_markdown_and_html(docx_bytes: bytes) -> Tuple[str, str, str]:
    """
    Convertit un docx en Markdown et HTML en utilisant Mammoth et Markdownify.
    Retourne (markdown, html, engine).
    """
    # 1. Conversion DOCX -> HTML avec Mammoth
    html_from_mammoth = _mammoth_docx_to_html(docx_bytes)

    # 2. Post-traitement de l'HTML
    html_processed = html_from_mammoth
    if CONVERT_HEADINGS_TO_BARE_STRONG:
        html_processed = _convert_headings_to_bare_strong(html_processed)
    
    html_final = _ensure_note_block_in_html(html_processed, docx_bytes=docx_bytes)

    # 3. Conversion de l'HTML final en Markdown
    markdown_final = html_to_md(html_final, strip=['span'], heading_style="ATX")

    return markdown_final, html_final, "mammoth+markdownify"