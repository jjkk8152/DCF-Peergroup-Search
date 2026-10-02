"""Fraud Scenario 분류·clustering (요구사항 7·8).

- 개별 사례를 그대로 RCM과 비교하지 않고 공통 fraud mechanism(Scenario)에 매핑한다.
- 고정 taxonomy(scenario_library.json) 우선. 어떤 시나리오에도 맞지 않는 실제 사례는
  'New Scenario Candidate' 로만 표시하고 확정하지 않는다(감사인 검토 후 승인).
- 공시 빈도 ≠ 발생 확률: 사례 수는 참고 정보로만 표시하고 위험등급을 매기지 않는다.
"""
from __future__ import annotations

from collections import defaultdict

from db import REVIEW_PRESERVE, j, rows, unj, upsert
from fraud_case_engine import load_cases
from scenario_lib import scenario, scenarios
from text_utils import find_terms

STRONG_W, SUPPORT_W, THRESHOLD = 2.0, 0.5, 2.0


def _case_text(case: dict) -> str:
    ev = " ".join(e["text"] for e in case.get("source_evidence") or [])
    return " ".join(filter(None, [case.get("fraud_scheme"), ev, " ".join(case.get("control_weakness") or []), case.get("cash_mechanism"), case.get("transaction_type")]))


def classify_case(case: dict) -> list[dict]:
    """사례 → [{scenario_id, score, matched_terms, rationale}] (점수 내림차순, 최대 3개)"""
    text = _case_text(case)
    out = []
    for s in scenarios():
        strong = find_terms(text, s["case_terms"]["strong"])
        support = find_terms(text, s["case_terms"]["support"])
        score = STRONG_W * len(strong) + SUPPORT_W * len(support)
        # FS010: 행위자가 경영진이면 경영진 무력화 시나리오에 추가 근거
        # (행위자 기준. 근거 문장에 경영진 직함이 '피해·소유자'로만 나오는 경우는 제외 — 예: "CFO의 OTP")
        if s["scenario_id"] == "FS010":
            if case.get("actor") == "경영진":
                score += STRONG_W
                strong = strong + ["행위자=경영진"]
        if score >= THRESHOLD and strong:
            out.append({
                "scenario_id": s["scenario_id"],
                "score": round(score, 1),
                "matched_terms": strong + support,
                "rationale": f"{s['title_ko']} 핵심 용어({', '.join(strong[:4])})가 사례 근거 문장에 등장",
            })
    out.sort(key=lambda r: -r["score"])
    return out[:3]


def classify_all(conn, engagement_id: int) -> dict:
    cases = load_cases(conn, engagement_id)
    unmatched = []
    n_links = 0
    with conn:
        conn.execute("DELETE FROM case_scenarios WHERE engagement_id=? AND review_status='Not Reviewed'", [engagement_id])
        conn.execute("DELETE FROM scenario_candidates WHERE engagement_id=? AND review_status='Not Reviewed'", [engagement_id])
        for c in cases:
            res = classify_case(c)
            if not res and c["case_kind"] == "fraud_incident":
                unmatched.append(c)
            for r in res:
                upsert(conn, "case_scenarios", {
                    "engagement_id": engagement_id, "case_id": c["case_id"], "scenario_id": r["scenario_id"],
                    "match_score": r["score"], "matched_terms": j(r["matched_terms"]), "rationale": r["rationale"],
                    "method": "rule", "ai_generated": 1,
                }, ["engagement_id", "case_id", "scenario_id"], preserve=REVIEW_PRESERVE)
                n_links += 1
        # 고정 taxonomy 밖 실제 사례 → 신규 시나리오 후보 (메커니즘 단서별로 묶음, 확정 아님)
        groups: dict[str, list[dict]] = defaultdict(list)
        for c in unmatched:
            key = c.get("transaction_type") if c.get("transaction_type") not in (None, "", "Not disclosed") else (c.get("fraud_type") or "기타")
            groups[key].append(c)
        for key, cs in groups.items():
            conn.execute(
                "INSERT INTO scenario_candidates (engagement_id, proposed_title, mechanism, case_ids, status, ai_generated) VALUES (?,?,?,?,?,1)",
                (engagement_id, f"[후보] {key} 관련 자금 유출", f"고정 taxonomy 에 매핑되지 않은 사례 {len(cs)}건 — 메커니즘 단서: {key}",
                 j([c["case_id"] for c in cs]), "New Scenario Candidate"),
            )
    return {"cases": len(cases), "links": n_links, "new_scenario_candidates": len({k for k in groups})}


def cluster(conn, engagement_id: int) -> list[dict]:
    """시나리오별 묶음: 사례 수·회사·시기·대표 사례·근거. 사례 수는 참고용(위험도 아님)."""
    cases = {c["case_id"]: c for c in load_cases(conn, engagement_id)}
    links = rows(conn, "SELECT * FROM case_scenarios WHERE engagement_id=? AND review_status != 'Rejected'", [engagement_id])
    by_s: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for l in links:
        if l["case_id"] in cases:
            by_s[l["scenario_id"]].append((cases[l["case_id"]], l))
    out = []
    for s in scenarios():
        items = by_s.get(s["scenario_id"], [])
        incidents = [c for c, _ in items if c["case_kind"] == "fraud_incident"]
        defs = [c for c, _ in items if c["case_kind"] == "control_deficiency_disclosure"]
        reps = sorted(items, key=lambda t: (t[0]["case_kind"] != "fraud_incident", -(t[0].get("confidence") or 0), -(t[0].get("amount") or 0)))[:3]
        dates = sorted(c.get("filing_date") or "" for c, _ in items if c.get("filing_date"))
        out.append({
            "scenario_id": s["scenario_id"],
            "title": s["title"],
            "title_ko": s["title_ko"],
            "description": s["description"],
            "mechanism": s["mechanism"],
            "accounts": s["accounts"],
            "processes": s["processes"],
            "assertions": s["assertions"],
            "peer_incident_count": len(incidents),
            "peer_deficiency_disclosure_count": len(defs),
            "companies": sorted({c.get("company_name") or "" for c, _ in items}),
            "filing_period": f"{dates[0]} ~ {dates[-1]}" if dates else "",
            "case_ids": [c["case_id"] for c, _ in items],
            "representative_cases": [{"case_id": c["case_id"], "company_name": c.get("company_name"), "fraud_scheme": c.get("fraud_scheme"), "rcept_no": c.get("rcept_no"), "source_url": c.get("source_url"), "why": l.get("rationale")} for c, l in reps],
            "source_evidence": [e for c, _ in reps for e in (c.get("source_evidence") or [])[:1]],
            "frequency_note": "사례 수는 공시된 건수일 뿐 발생 확률·위험등급이 아님",
        })
    return out


def new_scenario_candidates(conn, engagement_id: int) -> list[dict]:
    out = rows(conn, "SELECT * FROM scenario_candidates WHERE engagement_id=?", [engagement_id])
    for r in out:
        r["case_ids"] = unj(r["case_ids"], [])
    return out


def scenario_title(sid: str) -> str:
    s = scenario(sid)
    return f"{sid} {s['title']} ({s['title_ko']})" if s else sid
