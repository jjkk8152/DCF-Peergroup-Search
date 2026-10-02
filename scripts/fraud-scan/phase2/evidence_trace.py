"""Evidence Traceability (요구사항 16) + 분석 파이프라인.

trace(): Fraud Scenario → Peer Fraud Case → DART Filing → Exact Source Evidence → Scenario Classification
         → Target RCM Risk → Target Control → Coverage Evaluation → Suggested Audit Procedure
run_analysis(): 사례 구조화 → 시나리오 분류·clustering → RCM 매핑·coverage → 감사 고려사항 → 상충 정보
"""
from __future__ import annotations

import audit_response_engine
import contradiction_engine
import coverage_engine
import fraud_case_engine
import fraud_scenario_engine
from db import rows, unj
from rcm_mapper import load_matches
from rcm_parser import load_rcm
from scenario_lib import scenario as get_scenario
from scenario_lib import sync_to_db


def scenario_case_map(clusters: list[dict], include_all: bool = False) -> dict[str, list[str]]:
    """평가 대상 시나리오 = peer 사례가 있는 시나리오 (include_all 이면 taxonomy 전체)"""
    return {c["scenario_id"]: c["case_ids"] for c in clusters if include_all or c["case_ids"]}


def run_cases(conn, engagement_id: int, llm=None) -> dict:
    sync_to_db(conn)
    stats = fraud_case_engine.build_cases(conn, engagement_id, llm=llm)
    stats["classification"] = fraud_scenario_engine.classify_all(conn, engagement_id)
    return stats


def run_analysis(conn, engagement_id: int, upload_id: int, llm=None, include_all: bool = False) -> dict:
    sync_to_db(conn)
    clusters = fraud_scenario_engine.cluster(conn, engagement_id)
    rcm_rows = load_rcm(conn, upload_id)
    contra = contradiction_engine.run(conn, engagement_id, upload_id)  # 상충 정보 먼저 → coverage 판단에 반영
    cov = coverage_engine.run_coverage(conn, engagement_id, upload_id, scenario_case_map(clusters, include_all), rcm_rows, llm=llm, contradictions=contra)
    audit_response_engine.run(conn, engagement_id, upload_id, cov, clusters)
    counts = {k: sum(1 for r in cov if r["coverage"] == k) for k in ("Covered", "Partially Covered", "Not Covered", "Cannot Determine")}
    return {"scenarios_evaluated": len(cov), "coverage": counts, "contradictions": len(contra), "rcm_rows": len(rcm_rows)}


def summary(conn, engagement_id: int, upload_id: int | None) -> dict:
    one = lambda sql, p: rows(conn, sql, p)[0]["n"]  # noqa: E731
    s = {
        "benchmark_companies_selected": one("SELECT COUNT(*) n FROM peer_candidates WHERE engagement_id=? AND selected=1", [engagement_id]),
        "dart_filings_reviewed": one("SELECT COUNT(*) n FROM dart_filings WHERE engagement_id=? AND status='ok'", [engagement_id]),
        "relevant_fraud_cases": one("SELECT COUNT(*) n FROM fraud_cases WHERE engagement_id=? AND case_kind='fraud_incident' AND review_status!='Rejected'", [engagement_id]),
        "deficiency_disclosures": one("SELECT COUNT(*) n FROM fraud_cases WHERE engagement_id=? AND case_kind='control_deficiency_disclosure' AND review_status!='Rejected'", [engagement_id]),
        "keyword_hits_excluded": one("SELECT COUNT(*) n FROM fraud_cases WHERE engagement_id=? AND case_kind='keyword_hit'", [engagement_id]),
        "fraud_scenarios_identified": one("SELECT COUNT(DISTINCT scenario_id) n FROM case_scenarios WHERE engagement_id=? AND review_status!='Rejected'", [engagement_id]),
        "rcm_controls_reviewed": one("SELECT COUNT(*) n FROM rcm_rows WHERE upload_id=?", [upload_id or -1]),
        "coverage": {},
    }
    if upload_id:
        for r in rows(conn, "SELECT coverage, COUNT(*) n FROM coverage_results WHERE engagement_id=? AND upload_id=? GROUP BY coverage", [engagement_id, upload_id]):
            s["coverage"][r["coverage"]] = r["n"]
    return s


def attention_areas(conn, engagement_id: int, upload_id: int) -> list[dict]:
    """'동종업계 사례의 메커니즘 중 RCM에서 충분히 대응되지 않는 것' — 대시보드 상단 목록.
    정렬: coverage(Not Covered → Cannot Determine → Partially) 후 peer 사례 수. 위험 순위가 아니라 검토 순서."""
    order = {"Not Covered": 0, "Cannot Determine": 1, "Partially Covered": 2}
    res = [r for r in coverage_engine.load_results(conn, engagement_id, upload_id) if r["coverage"] in order]
    res.sort(key=lambda r: (order[r["coverage"]], -len(r["external_cases"])))
    return res


def trace(conn, engagement_id: int, upload_id: int, scenario_id: str) -> dict:
    s = get_scenario(scenario_id)
    clusters = {c["scenario_id"]: c for c in fraud_scenario_engine.cluster(conn, engagement_id)}
    links = {l["case_id"]: l for l in rows(conn, "SELECT * FROM case_scenarios WHERE engagement_id=? AND scenario_id=?", [engagement_id, scenario_id])}
    cases = [c for c in fraud_case_engine.load_cases(conn, engagement_id) if c["case_id"] in links]
    filings = {f["rcept_no"]: f for f in rows(conn, "SELECT * FROM dart_filings WHERE engagement_id=?", [engagement_id])}
    cov = next((r for r in coverage_engine.load_results(conn, engagement_id, upload_id) if r["scenario_id"] == scenario_id), None)
    aud = next((a for a in audit_response_engine.load(conn, engagement_id, upload_id) if a["scenario_id"] == scenario_id), None)
    return {
        "scenario": {**(clusters.get(scenario_id) or {}), "definition": s},
        "cases": [
            {
                "case": c,
                "filing": filings.get(c["rcept_no"]) or {"rcept_no": c["rcept_no"], "report_nm": c.get("report_name"), "source_url": c.get("source_url")},
                "evidence": c.get("source_evidence") or [],
                "field_evidence": c.get("field_evidence") or {},
                "classification": {**links[c["case_id"]], "matched_terms": unj(links[c["case_id"]]["matched_terms"], [])},
            }
            for c in cases
        ],
        "controls": load_matches(conn, engagement_id, upload_id, scenario_id) if upload_id else [],
        "coverage": cov,
        "audit_consideration": aud,
    }
