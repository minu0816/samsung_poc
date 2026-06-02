"""uiautomator XML 파싱 공용 헬퍼."""
from __future__ import annotations
import re
from pathlib import Path
from typing import Iterator


_NODE_RE = re.compile(r'<node\b[^>]*/?>')
_ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')


def iter_nodes(xml_path: str | Path) -> Iterator[dict]:
    p = Path(xml_path)
    if not p.exists():
        return
    text = p.read_text(encoding='utf-8', errors='replace')
    for m in _NODE_RE.finditer(text):
        node = dict(_ATTR_RE.findall(m.group(0)))
        yield node


def all_texts(xml_path: str | Path, *, dedup: bool = True) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for n in iter_nodes(xml_path):
        t = (n.get('text') or '').strip()
        if not t:
            continue
        if dedup and t in seen:
            continue
        out.append(t)
        seen.add(t)
    return out


def all_content_descs(xml_path: str | Path) -> list[str]:
    out, seen = [], set()
    for n in iter_nodes(xml_path):
        d = (n.get('content-desc') or '').strip()
        if d and d not in seen:
            out.append(d); seen.add(d)
    return out


def find_node_with_text(xml_path: str | Path, needle: str) -> dict | None:
    for n in iter_nodes(xml_path):
        if needle in (n.get('text') or ''):
            return n
    return None


def selected_tab_text(xml_path: str | Path) -> str | None:
    """selected="true"인 노드의 text를 반환 (탭 선택 검증에 사용)."""
    for n in iter_nodes(xml_path):
        if n.get('selected') == 'true' and (n.get('text') or '').strip():
            return n['text'].strip()
    return None


def has_text(xml_path: str | Path, needle: str) -> bool:
    return any(needle in (n.get('text') or '') for n in iter_nodes(xml_path))


def bounds_center(node: dict) -> tuple[int, int] | None:
    b = node.get('bounds') or ''
    m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', b)
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)
