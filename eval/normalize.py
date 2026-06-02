"""지명/POI 정규화 헬퍼.

C안에서는 Claude가 직접 매칭 판단하지만, 객관 추출 단계에서
"카카오 화면에서 origin/destination이 등장한 후보 텍스트"를 미리 추리는 데 사용한다.
"""
from __future__ import annotations
import re
import unicodedata


_TRAILING_TOKENS = ('역', '공항', '점', '타워', '몰', '센터')
_PUNCT_RE = re.compile(r'[\s\(\)\[\]·,.\-_/]+')


def normalize(s: str) -> str:
    if not s:
        return ''
    s = unicodedata.normalize('NFC', s)
    s = _PUNCT_RE.sub('', s).lower()
    for tok in _TRAILING_TOKENS:
        if s.endswith(tok) and len(s) > len(tok):
            s = s[: -len(tok)]
            break
    return s


def bigrams(s: str) -> set[str]:
    s = s.replace(' ', '')
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def candidate_matches(expected: str, texts: list[str]) -> list[dict]:
    """expected 지명에 대해 후보 텍스트들의 매칭 레벨을 추리. Claude가 보는 입력으로 사용."""
    norm_e = normalize(expected)
    bg_e = bigrams(norm_e)
    out = []
    for t in texts:
        norm_t = normalize(t)
        if expected in t:
            level = 'exact'
        elif norm_e and norm_e == norm_t:
            level = 'exact_normalized'
        elif norm_e and norm_e in norm_t:
            level = 'substring'
        else:
            j = jaccard(bg_e, bigrams(norm_t))
            if j >= 0.6:
                level = f'jaccard:{j:.2f}'
            else:
                continue
        out.append({'text': t, 'level': level})
    return out
