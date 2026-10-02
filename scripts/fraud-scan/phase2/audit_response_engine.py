"""Audit Consideration / Suggested Audit Procedures (요구사항 13·14·15).

감사인 관점의 산출물이다 (내부통제 컨설팅 결론 아님).
- Control Coverage Gap: RCM에서 해당 메커니즘 대응 통제를 확인하지 못한 내용 — deficiency 판정이 아니다.
- Audit Response Consideration: 위험평가·추가 감사절차에 줄 수 있는 영향 — 유의적 위험을 자동 확정하지 않는다.
모든 항목의 conclusion = 'Auditor judgment required'.
"""
from __future__ import annotations

from db import REVIEW_PRESERVE, j, rows, unj, upsert
from scenario_lib import scenario as get_scenario


def build(cov: dict, cluster_row: dict | None) -> dict:
    s = get_scenario(cov["scenario_id"])
    coverage = cov["coverage"]
    n_inc = (cluster_row or {}).get("peer_incident_count", 0)
    n_def = (cluster_row or {}).get("peer_deficiency_disclosure_count", 0)
    comps = (cluster_row or {}).get("companies", [])
    reps = (cluster_row or {}).get("representative_cases", [])
    rep_txt = f" 대표 사례: {reps[0]['company_name']} — \"{(reps[0]['fraud_scheme'] or '')[:120]}\"" if reps and reps[0].get("fraud_scheme") else ""

    why = (
        f"동종·유사업종 peer {len(comps)}개사 공시에서 '{s['title_ko']}' 메커니즘 관련 사례 {n_inc}건"
        f"(통제 미비 공시 {n_def}건 별도)이 확인되었다.{rep_txt} "
        f"핵심 메커니즘: {s['mechanism']}. 관련 계정 {', '.join(s['accounts'])}, 경영자 주장 {', '.join(s['assertions'])}. "
        "이는 감사대상회사에서 동일한 부정이 발생했다는 의미가 아니며, 공시 건수는 발생 확률을 뜻하지 않는다. "
        "ISA 240 위험식별·평가 시 고려할 외부 input 이다."
    )

    unc = cov.get("uncovered_elements") or []
    if coverage == "Covered":
        gap = "RCM에 핵심요소 대응 통제가 기술되어 있음(설계 존재). 운영 효과성은 평가되지 않음."
        response = ("현재 정보로는 위험평가 변경 필요성이 표시되지 않음. 통제에 의존하려는 경우 운영 효과성 테스트 범위를 감사인이 결정. "
                    "management override 위험(ISA 240)은 통제 존재와 무관하게 고려.")
        effect = "표시 없음 — 운영효과성 미평가 (Auditor judgment required)"
    else:
        gap = {
            "Partially Covered": "RCM상 관련 통제는 있으나 다음 요소·우회경로에 대한 기술을 확인하지 못함: " + ", ".join(unc),
            "Not Covered": f"RCM에서 '{s['title_ko']}' 메커니즘에 대응하는 통제를 확인하지 못함 (미확인: {', '.join(unc)})",
            "Cannot Determine": "통제 기술이 모호하거나 정보가 부족하여 설계 대응 여부를 판단할 수 없음: " + ", ".join(unc),
        }[coverage] + " — 이는 RCM 기술 기준 관찰이며 내부통제 미비(deficiency)·중요한 취약점 판정이 아님(RCM 범위·기술 수준의 한계일 수 있음)."
        response = (
            f"관련 계정({', '.join(s['accounts'][:3])})의 fraud risk assessment 재검토 필요성 고려. "
            "추가 질문·walkthrough 결과에 따라 실증절차의 성격·시기·범위 조정 여부를 감사인이 판단. "
            "유의적 위험 여부는 자동으로 확정하지 않음."
        )
        effect = "Fraud risk assessment 재검토 고려 — 유의적 위험 해당 여부는 감사인 판단 (자동 확정 아님)"

    elem_q = [e["question"] for e in s["control_elements"] if e["label"] in " ".join(unc)]
    questions = list(dict.fromkeys(elem_q + s["followup_questions"] + [q for q in cov.get("information_needed") or [] if q.endswith("?")]))
    procedures = list(s["procedures"])
    if coverage == "Covered":
        procedures = ["통제 의존 시 관련 통제의 운영 효과성 테스트 고려"] + procedures[:2]
    return {
        "scenario_id": cov["scenario_id"],
        "coverage": coverage,
        "control_coverage_gap": gap,
        "audit_response_consideration": response,
        "why_this_matters": why,
        "followup_questions": questions,
        "suggested_procedures": procedures,
        "potential_evidence": s["potential_evidence"],
        "risk_assessment_effect": effect,
        "conclusion": "Auditor judgment required",
    }


def run(conn, engagement_id: int, upload_id: int, coverage_results: list[dict], clusters: list[dict]) -> list[dict]:
    by_sid = {c["scenario_id"]: c for c in clusters}
    out = []
    with conn:
        for cov in coverage_results:
            a = build(cov, by_sid.get(cov["scenario_id"]))
            upsert(conn, "audit_considerations", {
                "engagement_id": engagement_id, "upload_id": upload_id, "scenario_id": a["scenario_id"], "coverage": a["coverage"],
                "control_coverage_gap": a["control_coverage_gap"], "audit_response_consideration": a["audit_response_consideration"],
                "why_this_matters": a["why_this_matters"], "followup_questions": j(a["followup_questions"]),
                "suggested_procedures": j(a["suggested_procedures"]), "potential_evidence": j(a["potential_evidence"]),
                "risk_assessment_effect": a["risk_assessment_effect"], "conclusion": a["conclusion"], "ai_generated": 1,
            }, ["engagement_id", "upload_id", "scenario_id"], preserve=REVIEW_PRESERVE)
            out.append(a)
    return out


def load(conn, engagement_id: int, upload_id: int) -> list[dict]:
    out = rows(conn, "SELECT * FROM audit_considerations WHERE engagement_id=? AND upload_id=? ORDER BY scenario_id", [engagement_id, upload_id])
    for r in out:
        for k in ("followup_questions", "suggested_procedures", "potential_evidence"):
            r[k] = unj(r[k], [])
    return out
