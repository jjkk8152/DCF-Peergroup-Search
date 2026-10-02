"""Fraud Scenario ↔ RCM 매핑 (요구사항 10) — 결정론 다요소 매칭(단순 cosine 아님).

12개 요소: 1 fraud mechanism · 2 actor · 3 transaction type · 4 affected account · 5 control objective
          6 control activity(시나리오 통제요소) · 7 authorization · 8 segregation of duties · 9 access rights
          10 monitoring · 11 preventive/detective 정합 · 12 management override 고려
원칙:
- 통제요소(6)는 '통제 기술'(control_description·evidence·owner)에서만 찾는다. 위험 기술(risk_description)은
  통제가 아니므로 control objective(5) 판단에만 쓴다 → RCM에 없는 통제를 있다고 추론하지 않는다.
- 각 요소 매칭에는 RCM 원문 인용(quote)을 남긴다 (Why was this matched?).
"""
from __future__ import annotations

from db import REVIEW_PRESERVE, j, rows, unj, upsert
from rcm_parser import control_label
from text_utils import find_terms, has_any, split_sentences, term_regex, vagueness

WEIGHTS = {
    "control_activity": 0.30, "mechanism": 0.12, "control_objective": 0.10, "affected_account": 0.08, "transaction_type": 0.08,
    "actor": 0.04, "authorization": 0.06, "segregation_of_duties": 0.05, "access_rights": 0.06, "monitoring": 0.05,
    "preventive_detective": 0.03, "management_override": 0.03,
}
AUTH_TERMS = ["승인", "결재", "권한", "이사회", "품의"]
SOD_TERMS = ["분리", "독립", "별도", "구분된", "겸직", "다른 담당", "제3의"]
ACCESS_TERMS = ["권한", "접근", "ID", "OTP", "인증서", "보안카드", "관리자", "비밀번호", "로그인"]
MONITOR_TERMS = ["모니터링", "검토", "대사", "리뷰", "점검", "분석", "비교"]
OVERRIDE_TERMS = ["경영진", "대표이사", "이사회", "감사위원회", "예외", "긴급", "override", "우회", "수기", "수동"]
PREVENTIVE_WORDS = ["예방", "preventive", "prevent"]
DETECTIVE_WORDS = ["적발", "detective", "detect"]
MIN_SCORE = 0.12


def element_match(element: dict, control_text: str) -> str | None:
    """요소의 모든 용어 그룹이 통제 기술에 있으면 근거 인용(가장 많이 겹치는 문장) 반환"""
    if not all(has_any(control_text, g) for g in element["all_of"]):
        return None
    sents = split_sentences(control_text) or [control_text]
    best = max(sents, key=lambda s: sum(1 for g in element["all_of"] if has_any(s, g)))
    return best.strip()[:300]


def match_row(scenario: dict, r: dict) -> dict:
    control_text = " ".join(filter(None, [r.get("control_description"), r.get("evidence"), r.get("control_owner")]))
    risk_text = r.get("risk_description") or ""
    full_text = " ".join(filter(None, [risk_text, control_text, r.get("process"), r.get("sub_process")]))

    matched = []
    for e in scenario["control_elements"]:
        q = element_match(e, control_text)
        if q:
            matched.append({"element_id": e["id"], "label": e["label"], "core": e["core"], "type": e["type"], "quote": q})
    core_total = sum(1 for e in scenario["control_elements"] if e["core"]) or 1
    core_hit = sum(1 for m in matched if m["core"])

    mech_hits = find_terms(full_text, scenario["case_terms"]["strong"] + scenario["relevance_terms"])
    obj_hits = find_terms(risk_text, scenario["relevance_terms"] + scenario["case_terms"]["strong"])
    pd_field = (r.get("preventive_detective") or "").lower()
    if matched and pd_field:
        types = {m["type"] for m in matched}
        aligned = ("preventive" in types and any(w in pd_field for w in PREVENTIVE_WORDS)) or ("detective" in types and any(w in pd_field for w in DETECTIVE_WORDS))
        pd_score = 1.0 if aligned else 0.0
    else:
        pd_score = 0.5 if matched else 0.0

    factors = {
        "control_activity": (core_hit + 0.3 * (len(matched) - core_hit)) / core_total,
        "mechanism": min(1.0, len(mech_hits) / 2),
        "control_objective": 1.0 if obj_hits else 0.0,
        "affected_account": 1.0 if has_any(full_text, scenario["account_terms"]) else 0.0,
        "transaction_type": 1.0 if has_any(full_text, scenario["transaction_terms"]) else 0.0,
        "actor": 1.0 if has_any(full_text, scenario["actors"]) else 0.0,
        "authorization": 1.0 if has_any(control_text, AUTH_TERMS) else 0.0,
        "segregation_of_duties": 1.0 if has_any(control_text, SOD_TERMS) else 0.0,
        "access_rights": 1.0 if has_any(control_text, ACCESS_TERMS) else 0.0,
        "monitoring": 1.0 if has_any(control_text, MONITOR_TERMS) else 0.0,
        "preventive_detective": pd_score,
        "management_override": 1.0 if has_any(full_text, OVERRIDE_TERMS) else 0.0,
    }
    factors = {k: round(min(1.0, v), 3) for k, v in factors.items()}
    score = round(sum(WEIGHTS[k] * v for k, v in factors.items()), 3)
    relevant = bool(matched) or (factors["mechanism"] >= 0.5 and factors["control_objective"] == 1.0)
    is_vague, vague_reasons = vagueness(r.get("control_description") or "", r.get("control_owner") or "", r.get("evidence") or "")

    why = []
    for m in matched:
        why.append(f"[{m['element_id']}] {m['label']} ← 통제 기술 \"{m['quote'][:120]}\"")
    if obj_hits:
        why.append(f"위험 기술이 시나리오 관련 용어 포함: {', '.join(obj_hits[:5])}")
    if mech_hits and not matched:
        why.append(f"메커니즘 관련 용어: {', '.join(mech_hits[:5])} (통제요소 직접 대응은 확인되지 않음)")
    return {
        "row_index": r["row_index"], "control_label": control_label(r), "risk_id": r.get("risk_id"),
        "risk_description": risk_text, "control_description": r.get("control_description"),
        "score": score, "factors": factors, "matched_elements": matched, "relevant": relevant and score >= MIN_SCORE,
        "is_vague": is_vague, "vague_reasons": vague_reasons, "why": why,
    }


def map_scenario(scenario: dict, rcm_rows: list[dict], top_n: int = 5) -> tuple[list[dict], list[dict]]:
    """(상위 top_n 관련 통제, 관련 통제 전체)"""
    results = [match_row(scenario, r) for r in rcm_rows]
    relevant = sorted([x for x in results if x["relevant"]], key=lambda x: -x["score"])
    return relevant[:top_n], relevant


def save_matches(conn, engagement_id: int, upload_id: int, scenario_id: str, top: list[dict]) -> None:
    with conn:
        conn.execute(
            "DELETE FROM scenario_control_matches WHERE engagement_id=? AND upload_id=? AND scenario_id=? AND review_status='Not Reviewed'",
            (engagement_id, upload_id, scenario_id),
        )
        for rank, m in enumerate(top, 1):
            upsert(conn, "scenario_control_matches", {
                "engagement_id": engagement_id, "upload_id": upload_id, "scenario_id": scenario_id, "row_index": m["row_index"],
                "rank": rank, "match_score": m["score"], "factor_scores": j(m["factors"]),
                "matched_elements": j(m["matched_elements"]), "why_matched": "\n".join(m["why"]),
                "is_vague": int(m["is_vague"]), "vague_reasons": j(m["vague_reasons"]), "ai_generated": 1,
            }, ["engagement_id", "upload_id", "scenario_id", "row_index"], preserve=REVIEW_PRESERVE)


def load_matches(conn, engagement_id: int, upload_id: int, scenario_id: str) -> list[dict]:
    out = rows(conn, """SELECT m.*, r.control_id, r.control_description, r.risk_id, r.risk_description, r.control_owner, r.process
                        FROM scenario_control_matches m JOIN rcm_rows r ON r.upload_id=m.upload_id AND r.row_index=m.row_index
                        WHERE m.engagement_id=? AND m.upload_id=? AND m.scenario_id=? ORDER BY m.rank""", [engagement_id, upload_id, scenario_id])
    for r in out:
        r["factor_scores"] = unj(r["factor_scores"], {})
        r["matched_elements"] = unj(r["matched_elements"], [])
        r["vague_reasons"] = unj(r["vague_reasons"], [])
    return out


# term_regex 재노출(테스트 편의)
__all__ = ["map_scenario", "match_row", "save_matches", "load_matches", "element_match", "term_regex"]
