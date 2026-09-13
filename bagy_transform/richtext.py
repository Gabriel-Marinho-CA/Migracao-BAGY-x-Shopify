"""HTML do editor da Bagy -> formatos estruturados da Shopify.

* html_to_rich_text: valor de metafield `rich_text_field` (JSON com root,
  paragraph, heading, list, list-item, link e text com bold/italic). Tabela,
  style e script ficam de fora: nao existem em rich text.
* nutrition_table: a <table> da informacao nutricional vira colunas + linhas,
  com a porcao (texto antes da tabela) e a observacao (texto depois).
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
BLOCKS = ("p", "div", "blockquote")


def _prune(node: dict) -> None:
    kept = []
    for child in node.get("children") or []:
        if child["type"] == "text":
            if child["value"]:
                kept.append(child)
        else:
            _prune(child)
            if child.get("children"):
                kept.append(child)
    node["children"] = kept


def _texts(node: dict) -> list:
    found = []
    for child in node.get("children") or []:
        if child["type"] == "text":
            found.append(child)
        else:
            found.extend(_texts(child))
    return found


class _RichText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.children = []
        self.lists = []
        self.block = None
        self.link = None
        self.bold = 0
        self.italic = 0
        self.skip = 0

    def _in_item(self) -> bool:
        return bool(self.block and self.block["type"] == "list-item")

    def _ensure(self) -> dict:
        if self.block is None:
            self.block = {"type": "paragraph", "children": []}
        return self.block

    def flush(self) -> None:
        block, self.block, self.link = self.block, None, None
        if block is None:
            return
        texts = _texts(block)
        if texts:
            texts[0]["value"] = texts[0]["value"].lstrip()
            texts[-1]["value"] = texts[-1]["value"].rstrip()
        _prune(block)
        if not block["children"]:
            return
        if block["type"] == "list-item":
            if self.lists:
                self.lists[-1]["children"].append(block)
                return
            block["type"] = "paragraph"
        self.children.append(block)

    def _text(self, text: str) -> None:
        target = self.link["children"] if self.link is not None else self._ensure()["children"]
        node = {"type": "text", "value": text}
        if self.bold:
            node["bold"] = True
        if self.italic:
            node["italic"] = True
        last = target[-1] if target else None
        if last and last["type"] == "text" and last.get("bold") == node.get("bold") \
                and last.get("italic") == node.get("italic"):
            last["value"] += text
        else:
            target.append(node)

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "table"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("ul", "ol"):
            self.flush()
            node = {"type": "list", "listType": "ordered" if tag == "ol" else "unordered", "children": []}
            self.children.append(node)
            self.lists.append(node)
        elif tag == "li":
            self.flush()
            self.block = {"type": "list-item", "children": []}
        elif tag in HEADINGS:
            self.flush()
            self.block = {"type": "heading", "level": HEADINGS[tag], "children": []}
        elif tag in BLOCKS:
            if not self._in_item():
                self.flush()
        elif tag == "br":
            if self._in_item():
                self._text(" ")
            else:
                self.flush()
        elif tag in ("b", "strong"):
            self.bold += 1
        elif tag in ("i", "em"):
            self.italic += 1
        elif tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if href:
                self.link = {"type": "link", "url": href, "children": []}
                self._ensure()["children"].append(self.link)

    def handle_endtag(self, tag):
        if tag in ("style", "script", "table"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("ul", "ol"):
            self.flush()
            if self.lists:
                self.lists.pop()
        elif tag == "li" or tag in HEADINGS:
            self.flush()
        elif tag in BLOCKS:
            if not self._in_item():
                self.flush()
        elif tag in ("b", "strong"):
            self.bold = max(0, self.bold - 1)
        elif tag in ("i", "em"):
            self.italic = max(0, self.italic - 1)
        elif tag == "a":
            self.link = None

    def handle_data(self, data):
        if self.skip:
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip() and self.block is None:
            return
        self._text(text)


def html_to_rich_text(html) -> str | None:
    if not html or not str(html).strip():
        return None
    parser = _RichText()
    parser.feed(str(html))
    parser.close()
    parser.flush()
    children = [child for child in parser.children if child["type"] != "list" or child["children"]]
    if not children:
        return None
    return json.dumps({"type": "root", "children": children}, ensure_ascii=False)


class _Nutrition(HTMLParser):
    LINE_BREAKS = ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_table = False
        self.table_done = False
        self.row = None
        self.row_is_header = False
        self.cell = None
        self.header = []
        self.rows = []
        self.before = [""]
        self.after = [""]

    def _lines(self) -> list:
        return self.after if self.table_done else self.before

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.skip += 1
            return
        if tag == "table" and not self.table_done:
            self.in_table = True
            return
        if self.in_table:
            if tag == "tr":
                self.row, self.row_is_header = [], False
            elif tag in ("td", "th"):
                self.cell = []
                self.row_is_header = self.row_is_header or tag == "th"
            return
        if tag in self.LINE_BREAKS:
            self._lines().append("")

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip = max(0, self.skip - 1)
            return
        if self.in_table:
            if tag in ("td", "th") and self.cell is not None and self.row is not None:
                self.row.append(" ".join("".join(self.cell).split()))
                self.cell = None
            elif tag == "tr" and self.row is not None:
                if self.row_is_header and not self.header:
                    self.header = self.row
                elif any(self.row):
                    self.rows.append(self.row)
                self.row = None
            elif tag == "table":
                self.in_table, self.table_done = False, True
            return
        if tag in self.LINE_BREAKS:
            self._lines().append("")

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_table:
            if self.cell is not None:
                self.cell.append(data)
            return
        self._lines()[-1] += data


def _clean_lines(lines: list) -> list:
    return [" ".join(line.split()) for line in lines if line and line.split()]


def nutrition_table(html) -> dict | None:
    """Informacao nutricional em HTML -> dados. None se nao houver tabela."""
    if not html:
        return None
    parser = _Nutrition()
    parser.feed(str(html))
    parser.close()
    if not parser.rows:
        return None
    before = _clean_lines(parser.before)
    after = _clean_lines(parser.after)
    header = parser.header[1:] if parser.header and not parser.header[0] else parser.header

    def find(pattern):
        for line in before:
            for part in re.split(r"(?=Porç(?:ão|ões|ao|oes)\b)", line):
                match = re.match(pattern, part.strip(), re.IGNORECASE)
                if match:
                    return match.group(1).strip()
        return None

    return {
        "colunas": header,
        "linhas": [{"nutriente": row[0], "valores": row[1:]} for row in parser.rows],
        "porcao": find(r"porç(?:ão|ao)\s*:\s*(.+)"),
        "porcoes_por_embalagem": find(r"porç(?:ões|oes)\s+por\s+embalagem\s*:\s*(.+)"),
        "observacao": "\n".join(after) or None,
    }
