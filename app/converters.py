# converters.py
from __future__ import annotations
import io
import re
import zipfile
from typing import Dict, Tuple

import mammoth
from bs4 import BeautifulSoup, NavigableString

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
    soup = BeautifulSoup(raw_html, 'lxml')

    # 3. Remplacement chirurgical des appels de note par le shortcode [note]
    docx_file.seek(0)
    notes = _extract_notes_from_docx(docx_file)
    if notes:
        for a_tag in soup.find_all("a", id=re.compile(r"^(end|foot)note-ref-\d+$")):
            note_id = a_tag["id"].split("-")[-1]
            note_text = notes.get(note_id)

            if note_text:
                shortcode = NavigableString(f"[note]{note_text}[/note]")
                if a_tag.parent and a_tag.parent.name == 'sup':
                    a_tag.parent.replace_with(shortcode)
                else:
                    a_tag.replace_with(shortcode)

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
    output_blocks = []
    if soup.body:
        for element in soup.body.children:
            if isinstance(element, NavigableString) and not element.strip():
                continue

            if element.name in ['h1', 'h2', 'h3', 'ul', 'ol']:
                output_blocks.append(str(element))
            elif element.name == 'p':
                # On décode le contenu du paragraphe pour garder <strong>, <em> et nos [note]
                content = element.decode_contents(formatter="html").strip()
                # On ne garde pas les paragraphes vides
                if content:
                    output_blocks.append(content)

    # On assemble le tout, séparé par des doubles sauts de ligne
    final_text_output = "\n\n".join(output_blocks)
    
    # Par sécurité, on nettoie les <strong> autour des h2 que Mammoth ajoute parfois
    final_text_output = re.sub(r'<h2><strong>(.*?)</strong></h2>', r'<h2>\1</h2>', final_text_output)
    
    # On décode les entités HTML comme &apos; en ' pour un texte plus propre
    # from html import unescape
    # final_text_output = unescape(final_text_output)

    # La partie "markdown" n'a plus beaucoup de sens, on renvoie une version texte simple
    md_output = soup.get_text(separator='\n\n')

    return md_output, final_text_output, "LavaConverter"