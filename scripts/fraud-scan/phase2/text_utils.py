"""결정론적 텍스트 처리 공용부 (LLM 없음).

Phase 1 TypeScript scoring.ts 의 termRegex 와 같은 규칙: 단어 사이 공백 무시, exclude 는 같은 위치 부정 전방탐색.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable


@lru_cache(maxsize=4096)
def term_regex(term: str, excludes: tuple[str, ...] = ()) -> re.Pattern:
    def flex(t: str) -> str:
        return r"\s*".join(re.escape(ch) for ch in re.sub(r"\s+", "", t))

    neg = f"(?!{'|'.join(flex(e) for e in excludes)})" if excludes else ""
    return re.compile(neg + flex(term), re.IGNORECASE)


def find_terms(text: str, terms: Iterable[str]) -> list[str]:
    return [t for t in terms if term_regex(t).search(text or "")]


def has_any(text: str, terms: Iterable[str]) -> bool:
    return any(term_regex(t).search(text or "") for t in terms)


def split_sentences(text: str) -> list[str]:
    """한국어 공시 문장 분리: 줄바꿈 / '다.' '음.' 등 종결 / 표 행('|')은 행 단위."""
    out: list[str] = []
    for line in (text or "").split("\n"):
        line = re.sub(r"^[▶\s#|]+", "", line).strip()
        if not line:
            continue
        parts = re.split(r"(?<=[다음함임요]\.)\s+|(?<=\.)\s+(?=[가-힣A-Z])", line)
        out.extend(p.strip() for p in parts if p.strip())
    return out


def sentences_with(text: str, terms: Iterable[str]) -> list[str]:
    terms = list(terms)
    return [s for s in split_sentences(text) if has_any(s, terms)]


def squash(s: str) -> str:
    """원문 대조용 정규화: 공백·▶·표 구분자 제거"""
    return re.sub(r"[\s▶|#]+", "", s or "")


def quote_in_source(quote: str, source: str) -> bool:
    """quote(여러 줄 가능)의 모든 줄이 source 에 글자 그대로 있는가 (공백 무시)"""
    src = squash(source)
    parts = [squash(p) for p in re.split(r"\n+", quote or "") if squash(p)]
    return bool(parts) and all(p in src for p in parts)


# ─── 역할(직위) ───
# 정규화된 역할명 → 별칭. 영문·국문 혼용 RCM/인터뷰를 같은 역할로 묶어 비교한다.
ROLE_ALIASES: dict[str, list[str]] = {
    "대표이사": ["대표이사", "CEO", "사장", "대표"],
    "회장": ["회장"],
    "최대주주": ["최대주주", "대주주", "지배주주"],
    "CFO": ["CFO", "재무이사", "재무담당임원", "재무 담당 임원", "재무담당이사", "Chief Financial Officer", "재무본부장"],
    "재무팀장": ["재무팀장", "재무 팀장", "Finance Manager", "재무부장", "경영지원팀장", "재경팀장", "회계팀장", "자금팀장"],
    "자금담당자": ["자금담당자", "자금 담당자", "자금담당 직원", "자금 담당 직원", "자금담당", "Treasury staff", "Treasury Officer", "출납담당자", "출납 담당자"],
    "회계담당자": ["회계담당자", "회계 담당자", "Accountant"],
    "구매담당자": ["구매담당자", "구매 담당자", "구매팀"],
    "임원": ["임원", "상무", "전무", "이사"],
    "직원": ["직원", "사원", "담당자", "팀원"],
    "감사위원회": ["감사위원회", "상근감사", "감사"],
    "이사회": ["이사회"],
}
_ROLE_ORDER = list(ROLE_ALIASES.keys())


def _alias_pattern() -> list[tuple[str, re.Pattern]]:
    pats = []
    for role, aliases in ROLE_ALIASES.items():
        for a in sorted(aliases, key=len, reverse=True):
            # 영문 별칭은 단어 경계, 국문은 앞에 다른 한글이 붙지 않을 때 ("감사인"의 "감사"는 제외)
            if re.match(r"^[A-Za-z]", a):
                pats.append((role, re.compile(rf"(?<![A-Za-z]){re.escape(a)}(?![A-Za-z])", re.IGNORECASE)))  # 한글 조사가 붙어도 매칭
            else:
                pats.append((role, re.compile(rf"{re.escape(a)}(?![인원회])" if a in ("감사", "이사") else re.escape(a))))
    pats.sort(key=lambda rp: -len(rp[1].pattern))
    return pats


_ROLE_PATTERNS = _alias_pattern()


def find_roles(text: str) -> list[tuple[str, int, int]]:
    """(정규화 역할, start, end) — 겹치는 매칭은 긴 별칭 우선"""
    found: list[tuple[str, int, int]] = []
    taken: list[tuple[int, int]] = []
    for role, pat in _ROLE_PATTERNS:
        for m in pat.finditer(text or ""):
            if any(not (m.end() <= s or m.start() >= e) for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            found.append((role, m.start(), m.end()))
    found.sort(key=lambda r: r[1])
    return found


def has_role(text: str) -> bool:
    return bool(find_roles(text))


# ─── 통제 기술(記述)의 모호성 ───
VAGUE_TERMS = ["적절히", "적절하게", "적정하게", "적절한", "필요 시", "필요시", "필요한 경우", "충분히", "적의", "적시에"]
METHOD_TERMS = [
    "전표", "품의", "서명", "날인", "로그", "시스템", "대사", "확인서", "검토서", "체크리스트", "증빙", "보고서", "목록",
    "비교", "조회", "통장사본", "사업자등록증", "OTP", "인증서", "권한", "대장", "계약서", "세금계산서", "검수", "콜백",
    "결재선", "이사회", "의사록", "리포트", "명세", "내역", "Maker", "Approver",
]


def vagueness(control_text: str, owner: str = "", evidence: str = "") -> tuple[bool, list[str]]:
    """통제 기술만으로 설계를 판단하기 어려운가. (is_vague, reasons)

    모호 판단 = 모호 표현이 있고 (수행자 또는 수행방법/증빙이 불명확) 하거나, 매우 짧고 수행자가 없는 경우.
    """
    text = control_text or ""
    reasons: list[str] = []
    vague_hits = find_terms(text, VAGUE_TERMS)
    no_actor = not has_role(text) and not has_role(owner or "") and not (owner or "").strip()
    no_method = not has_any(text, METHOD_TERMS) and not (evidence or "").strip()
    if vague_hits:
        reasons.append(f"모호한 표현: {', '.join(vague_hits)}")
    if no_actor:
        reasons.append("수행자(승인권자·담당자)가 기술되지 않음")
    if no_method:
        reasons.append("수행 방법·검토 대상·증빙이 기술되지 않음")
    is_vague = (bool(vague_hits) and (no_actor or no_method)) or (len(squash(text)) < 15 and no_actor)
    return is_vague, reasons if is_vague else []


# ─── 금액 ───
_AMOUNT_RE = re.compile(
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<unit>조\s*원|억\s*원|천만\s*원|백만\s*원|만\s*원|원|억)?"
)


def parse_amounts(text: str) -> list[tuple[float, str]]:
    """'12억원' → 1.2e9, '1,234,000,000' → 1.234e9. 단위 없는 숫자는 7자리 이상(콤마 포함)만 금액으로 본다."""
    out = []
    mult = {"조원": 1e12, "억원": 1e8, "억": 1e8, "천만원": 1e7, "백만원": 1e6, "만원": 1e4, "원": 1}
    for m in _AMOUNT_RE.finditer(text or ""):
        num = float(m.group("num").replace(",", ""))
        unit = re.sub(r"\s+", "", m.group("unit") or "")
        if unit:
            out.append((num * mult[unit], m.group(0)))
        elif "," in m.group("num") and num >= 1_000_000:
            out.append((num, m.group(0)))
    return out
