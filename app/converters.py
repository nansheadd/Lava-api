# converters.py
from __future__ import annotations
import io
import re
import zipfile
from typing import Dict, Tuple

import mammoth
from bs4 import BeautifulSoup, NavigableString, FeatureNotFound, Tag

def _extract_notes_from_docx(docx_file: io.BytesIO) -> Dict[str, str]:
    """Extrait les notes depuis word/footnotes.xml ou word/endnotes.xml."""
    notes: Dict[str, str] = {}
    note_filenames = ["word/endnotes.xml", "word/footnotes.xml"]
    try:
        with zipfile.ZipFile(docx_file) as z:
            for note_file in note_filenames:
                if note_file in z.namelist():
                    with z.open(note_file) as f:
                        xml = f.read().decode("utf-8", "ignore")
                    
                    note_tag = "endnote" if "endnotes" in note_file else "footnote"
                    
                    for m in re.finditer(rf'(?is)<w:{note_tag}[^>]*w:id="(-?\d+)"[^>]*>(.*?)</w:{note_tag}>', xml):
                        note_id, inner_xml = m.group(1), m.group(2)
                        if not note_id.isdigit(): continue
                        
                        text_fragments = re.findall(r'(?is)<w:t[^>]*>(.*?)</w:t>', inner_xml)
                        full_text = "".join(text_fragments).strip()
                        notes[note_id] = re.sub('<[^<]+?>', '', full_text)
    except Exception:
        pass
    return notes


def _collect_notes_from_soup(soup: BeautifulSoup) -> Dict[str, str]:
    """Récupère le contenu HTML des notes déjà présentes dans la sortie Mammoth."""
    notes: Dict[str, str] = {}
    if not soup.body:
        return notes

    for li in soup.select("li[id^='footnote-'], li[id^='endnote-']"):
        note_id = li.get("id", "").split("-")[-1]
        if not note_id:
            continue

        parts = []
        for child in li.children:
            if isinstance(child, NavigableString):
                text = child.strip()
                if text:
                    parts.append(text)
            elif isinstance(child, Tag):
                if child.name == "p":
                    content = child.decode_contents(formatter="html").strip()
                else:
                    content = str(child).strip()
                if content:
                    parts.append(content)

        html_content = "<br/><br/>".join(parts).strip()
        if html_content:
            notes[note_id] = html_content

    return notes

def docx_to_markdown_and_html(docx_bytes: bytes) -> Tuple[str, str, str]:
    """
    Convertit un .docx en format texte pour l'éditeur WordPress.
    """
    docx_file = io.BytesIO(docx_bytes)

    # RÈGLES DE STYLE : Utilisez les styles dans Word (Titre 1, Titre 2...)
    style_map = """
    p[style-name^='Heading 1'] => h1:fresh
    p[style-name^='Titre 1'] => h1:fresh
    p[style-name^='Heading 2'] => h2:fresh
    p[style-name^='Titre 2'] => h2:fresh
    p[style-name^='Heading 3'] => h3:fresh
    p[style-name^='Titre 3'] => h3:fresh
    """

    # 1. Conversion de base en HTML avec Mammoth
    result = mammoth.convert_to_html(docx_file, style_map=style_map)
    raw_html = result.value

    # 2. Utilisation de BeautifulSoup pour une manipulation fiable du HTML
    try:
        soup = BeautifulSoup(raw_html, "lxml")
    except FeatureNotFound:
        soup = BeautifulSoup(raw_html, "html.parser")

    # 3. Remplacement chirurgical des appels de note par le shortcode [note]
    notes = _collect_notes_from_soup(soup)
    if not notes:
        docx_file.seek(0)
        notes = _extract_notes_from_docx(docx_file)
    else:
        docx_file.seek(0)
    if notes:
        for a_tag in soup.find_all("a", id=re.compile(r"^(end|foot)note-ref-\d+$")):
            note_id = a_tag["id"].split("-")[-1]
            note_text = notes.get(note_id)

            if note_text:
                target = a_tag.parent if a_tag.parent and a_tag.parent.name == "sup" else a_tag
                fragment = BeautifulSoup(f"[note]{note_text}[/note]", "html.parser")
                new_nodes = list(fragment.contents)

                for new_node in reversed(new_nodes):
                    target.insert_after(new_node)

                target.decompose()

    # ==============================================================================
    # CORRECTION FINALE : Suppression garantie de la liste de notes à la fin
    # ==============================================================================
    for ol_tag in soup.find_all("ol"):
        first_li = ol_tag.find("li")
        # Si le premier <li> d'une liste a un id="endnote-...", c'est la liste à supprimer
        if first_li and first_li.get("id", "").startswith(("endnote-", "footnote-")):
            ol_tag.decompose()
            break # La liste est trouvée et supprimée, on arrête la boucle

    # 4. Construction du texte final au format WordPress
    if soup.body:
        # Supprime les nœuds vides pour éviter les paragraphes fantômes
        for element in list(soup.body.children):
            if isinstance(element, NavigableString):
                if not element.strip():
                    element.extract()
            elif isinstance(element, Tag):
                if element.name == "p" and not element.get_text(strip=True):
                    element.decompose()

        final_text_output = soup.body.decode_contents(formatter="html").strip()
    else:
        final_text_output = raw_html.strip()
    
    # Par sécurité, on nettoie les <strong> autour des h2 que Mammoth ajoute parfois
    final_text_output = re.sub(r'<h2><strong>(.*?)</strong></h2>', r'<h2>\1</h2>', final_text_output)
    
    # On décode les entités HTML comme &apos; en ' pour un texte plus propre
    # from html import unescape
    # final_text_output = unescape(final_text_output)

    # La partie "markdown" n'a plus beaucoup de sens, on renvoie une version texte simple
    md_output = soup.get_text(separator='\n\n')

    return md_output, final_text_output, "LavaConverter"
