# converters.py
from __future__ import annotations
import io
import re
import zipfile
from typing import Dict, Tuple
from html import unescape

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


def _normalize_inline(text: str) -> str:
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\s*\n\s*', '\n', text)
    return text.strip()


def _inline_children(tag: Tag) -> str:
    return "".join(_inline_markdown(child) for child in tag.children)


def _inline_markdown(node) -> str:
    if isinstance(node, NavigableString):
        return str(node)

    name = node.name.lower()

    if name in {"strong", "b"}:
        content = _inline_children(node)
        return f"**{content}**" if content else ""
    if name in {"em", "i"}:
        content = _inline_children(node)
        return f"*{content}*" if content else ""
    if name in {"code", "kbd"}:
        content = _inline_children(node)
        return f"`{content}`" if content else ""
    if name == "br":
        return "\n"
    if name == "a":
        content = _inline_children(node)
        href = node.get("href", "")
        if href and content:
            return f"[{content}]({href})"
        return content or href
    if name == "span":
        return _inline_children(node)
    if name == "p":
        return _inline_children(node)
    if name == "sup":
        return _inline_children(node)
    if name in {"ul", "ol"}:
        return "\n".join(_list_to_markdown(node))

    return _inline_children(node)


def _list_to_markdown(list_tag: Tag, indent: int = 0) -> list[str]:
    ordered = list_tag.name.lower() == "ol"
    lines: list[str] = []
    index = 1
    for li in list_tag.find_all("li", recursive=False):
        primary_parts: list[str] = []
        nested_lists: list[Tag] = []

        for child in li.children:
            if isinstance(child, NavigableString):
                text = str(child)
                if text.strip():
                    primary_parts.append(text)
            elif isinstance(child, Tag) and child.name.lower() in {"ul", "ol"}:
                nested_lists.append(child)
            else:
                primary_parts.append(_inline_markdown(child))

        text = _normalize_inline("".join(primary_parts))
        bullet = f"{index}." if ordered else "-"
        indent_str = "  " * indent
        if text:
            lines.append(f"{indent_str}{bullet} {text}".rstrip())
        else:
            lines.append(f"{indent_str}{bullet}")

        for nested in nested_lists:
            lines.extend(_list_to_markdown(nested, indent + 1))
        index += 1
    return lines


def _block_to_markdown(tag: Tag) -> list[str]:
    name = tag.name.lower()
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(name[1])
        content = _normalize_inline(_inline_children(tag))
        return [f"{'#' * level} {content}".rstrip()] if content else []
    if name == "p":
        content = _normalize_inline(_inline_children(tag))
        return [content] if content else []
    if name in {"ul", "ol"}:
        return _list_to_markdown(tag)
    if name == "blockquote":
        content = _normalize_inline(_inline_children(tag))
        return [f"> {content}".rstrip()] if content else []

    content = _normalize_inline(_inline_children(tag))
    return [content] if content else []


def _html_to_markdown(html: str) -> str:
    if not html:
        return ""

    wrapper = f"<div>{html}</div>"
    try:
        soup = BeautifulSoup(wrapper, "lxml")
    except FeatureNotFound:
        soup = BeautifulSoup(wrapper, "html.parser")

    container = soup.find("div")
    if not container:
        return ""

    blocks: list[str] = []
    for child in container.children:
        if isinstance(child, NavigableString):
            text = _normalize_inline(str(child))
            if text:
                blocks.append(text)
            continue
        if isinstance(child, Tag):
            blocks.extend(_block_to_markdown(child))

    return "\n\n".join(line for line in blocks if line).strip()


def _inject_note_shortcodes(html: str, notes: Dict[str, str]) -> str:
    if not html or not notes:
        return html
    wrapper = f"<div>{html}</div>"
    try:
        soup = BeautifulSoup(wrapper, "lxml")
    except FeatureNotFound:
        soup = BeautifulSoup(wrapper, "html.parser")

    container = soup.find("div")
    if not container:
        return html

    note_link_selector = re.compile(r"^(end|foot)note-ref-\d+$")

    for anchor in list(container.find_all("a", id=note_link_selector)):
        note_id = anchor.get("id", "").split("-")[-1]
        note_text = notes.get(note_id)
        if not note_text:
            continue

        target = anchor
        while target.parent and target.parent.name == "sup":
            target = target.parent

        replacement = BeautifulSoup(f"[note]{note_text}[/note]", "html.parser")
        new_nodes = list(replacement.contents)
        for new_node in reversed(new_nodes):
            target.insert_after(new_node)
        target.decompose()

    return container.decode_contents(formatter="html")


def docx_to_markdown_and_html(docx_bytes: bytes) -> Tuple[str, str, str, Dict[str, str]]:
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
    notes_map = dict(notes)
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
    
    final_text_output = _inject_note_shortcodes(final_text_output, notes_map)
    final_text_output = unescape(final_text_output)

    md_output = _html_to_markdown(final_text_output)
    if notes_map:
        for note_id, note_html in sorted(notes_map.items(), key=lambda kv: int(kv[0])):
            pattern = re.compile(rf'\[{re.escape(note_id)}\]')
            replacement = f"[note]{note_html}[/note]"
            md_output = pattern.sub(replacement, md_output)

    return md_output, final_text_output, "LavaConverter", notes_map
