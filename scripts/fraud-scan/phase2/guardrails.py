"""AI Safety / Audit Methodology Guardrails (요구사항 21).

1) 모든 LLM 호출의 system prompt 에 GUARDRAIL_RULES 를 그대로 넣는다.
2) LLM 이 돌려준 서술은 FORBIDDEN_CONCLUSIONS 로 검사해 확정적 감사결론 문장을 제거·표시한다.
3) coverage 는 결정론 결과보다 '더 유리하게'(Covered 쪽으로) 바뀔 수 없다 — enforce_coverage.
"""
from __future__ import annotations

import re

GUARDRAIL_RULES = [
    "공시가 존재한다는 이유만으로 감사대상회사의 fraud risk가 높다고 판단하지 않는다.",
    "Peer 회사의 fraud가 감사대상회사에서도 발생했다고 추론하지 않는다.",
    "AI는 fraud 발생 여부를 확정하지 않는다.",
    "AI는 internal control deficiency의 최종 등급을 결정하지 않는다.",
    "AI는 significant risk 여부를 최종 확정하지 않는다.",
    "AI는 audit opinion을 제안하지 않는다.",
    "원문 evidence가 없으면 사실로 생성하지 않는다.",
    "RCM에 없는 통제를 존재한다고 추론하지 않는다.",
    "'통제가 존재함'과 '통제가 효과적으로 운영됨'을 구분한다.",
    "Design effectiveness와 operating effectiveness를 구분한다.",
    "외부사례를 감사증거 그 자체로 취급하지 않는다.",
    "외부사례는 fraud risk identification 및 auditor consideration을 지원하는 input으로 취급한다.",
    "최종 결론은 반드시 auditor judgment가 필요하다.",
]

GUARDRAIL_TEXT = "\n".join(f"{i}. {r}" for i, r in enumerate(GUARDRAIL_RULES, 1))

DISCLAIMER = (
    "본 결과는 외부 공시 사례를 이용한 fraud risk identification 보조자료이며 감사증거가 아니다. "
    "부정 발생 여부, 내부통제 미비 등급, 유의적 위험, 감사의견은 감사인이 판단한다."
)

# 확정적 결론으로 읽히는 표현 (LLM 출력 검사용)
FORBIDDEN_CONCLUSIONS = [
    (r"유의적\s*위험(으로|에)\s*(해당|확정|판단|분류)(한다|된다|함|됨|이다)", "유의적 위험 확정 표현"),
    (r"significant\s+risk\s+(is|exists|confirmed)", "significant risk 확정 표현"),
    (r"(중요한\s*취약점|유의한\s*미비점|material\s+weakness|significant\s+deficiency)(에|으로|이)\s*(해당|판단|확정)", "통제 미비 등급 확정 표현"),
    (r"(부정|횡령|배임)(이|가)\s*(발생하였|발생했|존재한다|있었다)", "감사대상회사 부정 발생 단정 표현"),
    (r"(한정|부적정|의견거절)\s*의견", "감사의견 언급"),
    (r"감사의견(을|에)\s*(표명|변형|제시)", "감사의견 언급"),
    (r"반드시\s*(통제를\s*)?(신설|도입|구축)해야", "내부통제 컨설팅식 단정 표현"),
]


def check_text(text: str) -> list[str]:
    """금지 결론 표현 목록 반환 (없으면 빈 리스트)"""
    hits = []
    for pat, label in FORBIDDEN_CONCLUSIONS:
        if re.search(pat, text or "", re.IGNORECASE):
            hits.append(label)
    return hits


def sanitize(text: str) -> tuple[str, list[str]]:
    """금지 표현이 있는 문장을 제거하고 제거 사유를 돌려준다."""
    if not text:
        return text, []
    kept, removed = [], []
    for sent in re.split(r"(?<=[.다])\s+", text):
        hits = check_text(sent)
        if hits:
            removed.extend(hits)
        else:
            kept.append(sent)
    return " ".join(kept).strip(), removed


# coverage 의 '보수성' 순서: 숫자가 클수록 감사인 주의가 더 필요한 쪽
_CONSERVATISM = {"Covered": 0, "Partially Covered": 1, "Cannot Determine": 2, "Not Covered": 2}


def enforce_coverage(rule_result: str, llm_result: str | None) -> tuple[str, str]:
    """결정론 결과 대비 LLM 제안을 채택할지 결정. (final, note)

    - LLM 이 더 유리한 판단(예: Partially → Covered)을 내리면 채택하지 않는다 (부족한 정보로 Covered 금지).
    - LLM 이 더 보수적인 판단을 내리면 채택한다.
    - Not Covered ↔ Cannot Determine 사이 변경은 정보 부족 판단이 갈리는 것이므로 Cannot Determine 으로 둔다.
    """
    if not llm_result or llm_result == rule_result:
        return rule_result, ""
    r, l = _CONSERVATISM[rule_result], _CONSERVATISM[llm_result]
    if l < r:
        return rule_result, f"LLM 제안({llm_result})은 규칙 기반 결과({rule_result})보다 유리한 판단이므로 채택하지 않음"
    if l == r:
        return "Cannot Determine", f"규칙({rule_result})과 LLM({llm_result}) 판단이 갈려 Cannot Determine 으로 표시 — 감사인 확인 필요"
    return llm_result, f"LLM 제안({llm_result})이 더 보수적이어서 채택 (규칙 기반: {rule_result})"
