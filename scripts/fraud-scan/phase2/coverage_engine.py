"""Coverage Evaluation (요구사항 11·12) — 결정론 판정 + (선택) LLM 검토, 가드레일 적용.

판정 규칙 (설계 존재 여부만, 운영효과성은 평가하지 않음):
  관련 통제 없음                                       → Not Covered
  핵심요소(core) 전부가 '구체적' 통제 기술로 확인         → Covered
      단, critical 우회경로가 RCM상 별도로 대응되지 않으면 → Partially Covered
  핵심요소 일부만 구체적으로 확인                        → Partially Covered
  핵심요소가 '모호한' 통제 기술에서만 확인                → Cannot Determine
  관련 통제는 있으나 핵심요소 대응 없음
      관련 통제가 모두 모호                             → Cannot Determine
      그 외(일반 통제만 존재)                           → Not Covered (일반 통제는 rationale 에 명시)
부족한 정보를 추정해 Covered 로 만들지 않는다. LLM 은 결과를 더 유리하게 바꿀 수 없다(guardrails.enforce_coverage).
"""
from __future__ import annotations

from db import REVIEW_PRESERVE, j, rows, unj, upsert
from guardrails import enforce_coverage, sanitize
from rcm_mapper import map_scenario, save_matches
from scenario_lib import scenario as get_scenario
from text_utils import quote_in_source, squash

DESIGN_NOTE = "RCM 기술(design)에 근거한 대응 여부 판단이며, 통제가 효과적으로 운영되는지(operating effectiveness)는 평가하지 않음."
GENERIC_OVERRIDE = "management override / collusion: 통제가 설계되어 있어도 경영진 무력화·공모로 우회될 수 있음 (ISA 240) — 통제 존재만으로 위험이 대응되었다고 보지 않음"


def evaluate(scenario: dict, rcm_rows: list[dict], case_ids: list[str], contradicted: list[dict] | tuple = ()) -> tuple[dict, list[dict]]:
    """(coverage_result, top_matches)

    contradicted: contradiction_engine 결과 중 RCM 진술이 포함된 건. 해당 RCM 문장만으로 확인되는 요소는
    '상충 정보'로 분류해 Covered 근거로 쓰지 않는다 (어느 쪽이 사실인지 판단하지 않음).
    """
    top, relevant = map_scenario(scenario, rcm_rows)
    core = [e for e in scenario["control_elements"] if e["core"]]
    contra_stmts = [(squash(c["statement"]), c) for c in contradicted]

    def contra_of(m: dict) -> dict | None:
        desc = squash(m.get("control_description") or "")
        return next((c for st, c in contra_stmts if st and st in desc), None)

    for m in relevant:
        m["contradiction"] = contra_of(m)
    specific = [m for m in relevant if not m["is_vague"] and not m["contradiction"]]
    contra_rows = [m for m in relevant if not m["is_vague"] and m["contradiction"]]
    vague = [m for m in relevant if m["is_vague"]]

    def elems(ms: list[dict]) -> dict[str, list[tuple[dict, dict]]]:
        out: dict[str, list[tuple[dict, dict]]] = {}
        for m in ms:
            for e in m["matched_elements"]:
                out.setdefault(e["element_id"], []).append((m, e))
        return out

    spec_e, vague_e, contra_e = elems(specific), elems(vague), elems(contra_rows)
    covered_core = [e for e in core if e["id"] in spec_e]
    contra_core = [e for e in core if e["id"] not in spec_e and e["id"] in contra_e]
    vague_core = [e for e in core if e["id"] not in spec_e and e["id"] not in contra_e and e["id"] in vague_e]
    missing_core = [e for e in core if e["id"] not in spec_e and e["id"] not in contra_e and e["id"] not in vague_e]
    supporting = [e for e in scenario["control_elements"] if not e["core"] and e["id"] in spec_e]

    unmitigated_critical = [p for p in scenario["circumvention_paths"] if p.get("critical") and not any(x in spec_e for x in p.get("mitigated_by", []))]
    circumvention = []
    for p in scenario["circumvention_paths"]:
        ok = any(x in spec_e for x in p.get("mitigated_by", []))
        circumvention.append(f"{p['method']}: {p['description']} — " + ("RCM상 관련 통제 기술 있음(운영 여부 별도 확인)" if ok else "RCM상 별도 대응 통제 확인되지 않음"))
    circumvention.append(GENERIC_OVERRIDE)

    def cite(e: dict, pool: dict) -> str:
        m, me = pool[e["id"]][0]
        return f"{e['label']} — {m['control_label']}: \"{me['quote'][:150]}\""

    info_needed: list[str] = []
    for e in contra_core:
        m, _ = contra_e[e["id"]][0]
        c = m["contradiction"]
        info_needed.append(f"Contradictory Information – Auditor Follow-up Required: {e['label']} — {c['source_1']} \"{c['statement_1']}\" vs {c['source_2']} \"{c['statement_2']}\"")
    if not relevant:
        coverage = "Not Covered"
        rationale = f"RCM에서 '{scenario['title_ko']}' 메커니즘({scenario['mechanism']})에 대응하는 위험·통제 기술을 확인하지 못함."
        info_needed.append("해당 프로세스가 RCM 범위 밖인지, 다른 문서(규정·매뉴얼)에 통제가 있는지 확인 필요")
    elif len(covered_core) == len(core) and not unmitigated_critical and not contra_core:
        coverage = "Covered"
        rationale = "핵심 메커니즘에 대응하는 통제가 RCM에 구체적으로 기술됨: " + "; ".join(cite(e, spec_e) for e in covered_core)
    elif covered_core:
        coverage = "Partially Covered"
        parts = ["관련 통제는 존재: " + "; ".join(cite(e, spec_e) for e in covered_core)]
        if missing_core or vague_core:
            parts.append("미확인 핵심요소: " + ", ".join(e["label"] for e in missing_core + vague_core))
        if contra_core:
            parts.append("상충 정보로 확인 보류된 요소: " + ", ".join(e["label"] for e in contra_core))
        if unmitigated_critical:
            parts.append("우회 경로가 RCM상 별도로 대응되지 않음: " + "; ".join(f"{p['method']}({p['description']})" for p in unmitigated_critical))
        rationale = ". ".join(parts) + "."
    elif vague_core or contra_core or (relevant and not specific):
        coverage = "Cannot Determine"
        reasons = sorted({r for m in vague for r in m["vague_reasons"]}) + (["RCM 기술과 다른 자료의 진술이 상충"] if contra_core else [])
        rationale = ("관련 통제 기술은 있으나 설계 판단에 필요한 정보가 부족함: " + "; ".join(reasons) + ". "
                     + "모호한 기술: " + "; ".join(f"{m['control_label']} \"{(m['control_description'] or '')[:100]}\"" for m in vague[:3]))
        info_needed += [f"{m['control_label']}: 승인권자·수행방법·검토 대상·증빙·권한 범위를 구체적으로 확인" for m in vague[:3]]
    else:
        coverage = "Not Covered"
        sup = "; ".join(cite(e, spec_e) for e in supporting) if supporting else ", ".join(m["control_label"] for m in specific[:3])
        rationale = (f"관련 영역의 일반 통제는 있으나({sup}) '{scenario['title_ko']}' 메커니즘의 핵심요소"
                     f"({', '.join(e['label'] for e in core)})에 직접 대응하는 통제는 RCM에서 확인되지 않음.")

    for e in missing_core + vague_core:
        info_needed.append(e["question"])
    if coverage == "Covered":
        info_needed.append("통제 운영효과성은 평가되지 않음 — 통제 의존 여부·운영 테스트 필요성은 감사인 판단")

    confidence = {"Covered": 0.6, "Partially Covered": 0.6, "Not Covered": 0.5, "Cannot Determine": 0.4}[coverage]
    if len(rcm_rows) < 5:
        confidence -= 0.1  # RCM 범위가 좁으면 '확인되지 않음'의 신뢰도도 낮다
    result = {
        "scenario_id": scenario["scenario_id"],
        "scenario_title": f"{scenario['title']} ({scenario['title_ko']})",
        "external_cases": case_ids,
        "matched_risks": [{"row_index": m["row_index"], "risk_id": m["risk_id"], "risk_description": m["risk_description"]} for m in top if m["risk_description"]],
        "matched_controls": [{"row_index": m["row_index"], "control_id": m["control_label"], "control_description": m["control_description"],
                              "score": m["score"], "elements": [e["element_id"] for e in m["matched_elements"]], "is_vague": m["is_vague"]} for m in top],
        "coverage": coverage,
        "rationale": rationale,
        "covered_elements": [cite(e, spec_e) for e in covered_core] + [f"(보조) {cite(e, spec_e)}" for e in supporting],
        "uncovered_elements": [e["label"] + (" (모호한 기술로만 언급)" if e in vague_core else "") for e in missing_core + vague_core]
                              + [e["label"] + " (상충 정보 — Auditor follow-up required)" for e in contra_core],
        "possible_circumvention": circumvention,
        "information_needed": list(dict.fromkeys(info_needed)),
        "design_vs_operating_note": DESIGN_NOTE,
        "confidence": round(max(0.2, confidence), 2),
        "method": "rule",
        "llm_notes": "",
    }
    return result, top


# ─── (선택) LLM 검토 ───
def _llm_schema() -> dict:
    from llm_client import obj, str_list

    return obj({
        "suggested_coverage": {"type": "string", "enum": ["Covered", "Partially Covered", "Not Covered", "Cannot Determine"]},
        "rationale": {"type": "string"},
        "rcm_quotes": str_list(),
        "additional_uncovered_elements": str_list(),
        "additional_followup_questions": str_list(),
        "possible_circumvention": str_list(),
        "confidence": {"type": "number"},
    })


_LLM_ROLE = """[작업] 외부 부정 시나리오의 핵심 메커니즘이 감사대상회사 RCM 통제 '설계'로 대응되는지 검토 의견을 냅니다.
- suggested_coverage 정의: Covered=핵심 메커니즘을 직접 예방/적시 적발하는 통제가 RCM에 명확히 존재 / Partially Covered=일부만 대응하거나 중요한 우회경로가 RCM상 별도 대응 안 됨 / Not Covered=대응 통제 확인 안 됨 / Cannot Determine=RCM 기술만으로 판단 정보 부족.
- 정보가 부족하면 절대 Covered 로 추정하지 않습니다. RCM에 적히지 않은 통제를 있다고 가정하지 않습니다.
- rcm_quotes 에는 판단 근거가 된 RCM 문장을 글자 그대로 복사합니다(자동 대조됨).
- 통제 존재와 운영 효과성을 구분하고, 결론이 아닌 감사인 확인 사항으로 서술합니다."""


def llm_review(result: dict, scenario: dict, top: list[dict], llm) -> dict:
    rcm_text = "\n".join(f"[{m['control_label']}] 위험: {m['risk_description']} / 통제: {m['control_description']}" for m in top)
    user = (
        f"[시나리오] {scenario['scenario_id']} {scenario['title_ko']}\n메커니즘: {scenario['mechanism']}\n"
        f"핵심 통제요소: {', '.join(e['label'] for e in scenario['control_elements'] if e['core'])}\n"
        f"우회 경로: {', '.join(p['method'] + '(' + p['description'] + ')' for p in scenario['circumvention_paths'])}\n\n"
        f"[관련 RCM 행]\n<rcm>\n{rcm_text or '(관련 행 없음)'}\n</rcm>\n\n[규칙 기반 판정] {result['coverage']}: {result['rationale']}"
    )
    data = llm.structured("coverage_review", _LLM_ROLE, user, _llm_schema())
    quotes_ok = all(quote_in_source(q, rcm_text) for q in data.get("rcm_quotes") or []) if data.get("rcm_quotes") else not top
    final, note = enforce_coverage(result["coverage"], data["suggested_coverage"] if quotes_ok else None)
    text, removed = sanitize(data.get("rationale") or "")
    notes = [note] if note else []
    if not quotes_ok:
        notes.append("LLM 인용문이 RCM 원문과 일치하지 않아 LLM 판정 미채택")
    if removed:
        notes.append("가드레일로 제거된 표현: " + ", ".join(sorted(set(removed))))
    out = dict(result)
    out["coverage"] = final
    out["method"] = "rule+llm"
    out["llm_notes"] = " / ".join(filter(None, [f"LLM 검토 의견: {text}" if text and quotes_ok else "", *notes]))
    extra_q = [sanitize(q)[0] for q in data.get("additional_followup_questions") or []]
    out["information_needed"] = list(dict.fromkeys(result["information_needed"] + [q for q in extra_q if q]))
    out["possible_circumvention"] = list(dict.fromkeys(result["possible_circumvention"] + [sanitize(c)[0] for c in data.get("possible_circumvention") or [] if sanitize(c)[0]]))
    if final != result["coverage"]:
        out["rationale"] = result["rationale"] + f" [LLM 검토 반영: {note}]"
    return out


def save_result(conn, engagement_id: int, upload_id: int, r: dict) -> None:
    with conn:
        upsert(conn, "coverage_results", {
            "engagement_id": engagement_id, "upload_id": upload_id, "scenario_id": r["scenario_id"], "scenario_title": r["scenario_title"],
            "external_cases": j(r["external_cases"]), "matched_risks": j(r["matched_risks"]), "matched_controls": j(r["matched_controls"]),
            "coverage": r["coverage"], "rationale": r["rationale"], "covered_elements": j(r["covered_elements"]),
            "uncovered_elements": j(r["uncovered_elements"]), "possible_circumvention": j(r["possible_circumvention"]),
            "information_needed": j(r["information_needed"]), "design_vs_operating_note": r["design_vs_operating_note"],
            "confidence": r["confidence"], "method": r["method"], "llm_notes": r["llm_notes"], "ai_generated": 1,
        }, ["engagement_id", "upload_id", "scenario_id"], preserve=REVIEW_PRESERVE)


def run_coverage(conn, engagement_id: int, upload_id: int, scenario_cases: dict[str, list[str]], rcm_rows: list[dict], llm=None,
                 contradictions: list[dict] | None = None) -> list[dict]:
    """scenario_cases: {scenario_id: [case_id…]} — 평가 대상 시나리오와 근거 사례"""
    # RCM 진술이 포함된 상충 건 → 해당 RCM 문장 목록
    contra = []
    for c in contradictions or []:
        for k in ("1", "2"):
            if (c.get(f"source_{k}") or "").startswith("RCM"):
                contra.append({**c, "statement": c[f"statement_{k}"]})
    results = []
    for sid, case_ids in scenario_cases.items():
        s = get_scenario(sid)
        if not s:
            continue
        r, top = evaluate(s, rcm_rows, case_ids, contra)
        if llm is not None:
            try:
                r = llm_review(r, s, top, llm)
            except Exception as e:  # LLM 실패 시 규칙 결과 유지
                r["llm_notes"] = f"LLM 검토 실패 — 규칙 결과 유지: {e}"
        save_matches(conn, engagement_id, upload_id, sid, top)
        save_result(conn, engagement_id, upload_id, r)
        results.append(r)
    return results


def load_results(conn, engagement_id: int, upload_id: int) -> list[dict]:
    out = rows(conn, "SELECT * FROM coverage_results WHERE engagement_id=? AND upload_id=? ORDER BY scenario_id", [engagement_id, upload_id])
    for r in out:
        for k in ("external_cases", "matched_risks", "matched_controls", "covered_elements", "uncovered_elements", "possible_circumvention", "information_needed"):
            r[k] = unj(r[k], [])
    return out
