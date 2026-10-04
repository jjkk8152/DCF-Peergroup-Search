"""Phase 2 — External Fraud Benchmarking & RCM Coverage (감사인용, ISA 240 지원)

실행:  streamlit run scripts/fraud-scan/phase2/app.py
DB:    fraud-scan-output/phase2.db (환경변수 PHASE2_DB 로 변경)
"""
from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

import contradiction_engine  # noqa: E402
import evidence_trace  # noqa: E402
import fraud_case_engine  # noqa: E402
import fraud_scenario_engine  # noqa: E402
import peer_selector  # noqa: E402
import phase1_bridge  # noqa: E402
import rcm_parser  # noqa: E402
import visual_queue  # noqa: E402
from db import DEFAULT_DB_PATH, REPO_ROOT, REVIEW_STATUSES, connect, rows, set_review, unj  # noqa: E402
from engagement import EngagementProfile, list_engagements, load_engagement, save_engagement  # noqa: E402
from export_excel import build_workbook  # noqa: E402
from guardrails import DISCLAIMER, GUARDRAIL_RULES  # noqa: E402
from llm_client import DEFAULT_MODEL, LLMClient, llm_available  # noqa: E402

st.set_page_config(page_title="Fraud Benchmarking · RCM Coverage", layout="wide")

# 상태(coverage) 표시: 색만으로 의미를 전달하지 않도록 아이콘+라벨. 위험등급이 아니라 RCM 대응 상태.
COVERAGE_STYLE = {
    "Covered": ("✓", "#0ca30c"),
    "Partially Covered": ("△", "#fab219"),
    "Not Covered": ("✕", "#ec835a"),
    "Cannot Determine": ("?", "#8a8a85"),
}
COVERAGE_ORDER = ["Not Covered", "Cannot Determine", "Partially Covered", "Covered"]


@st.cache_resource
def get_conn(path: str):
    return connect(path)


conn = get_conn(str(DEFAULT_DB_PATH))


def cov_label(c: str | None) -> str:
    if not c:
        return "—"
    return f"{COVERAGE_STYLE[c][0]} {c}"


def cov_badge(c: str) -> str:
    icon, color = COVERAGE_STYLE[c]
    return f"<span style='border:1px solid {color};border-left:6px solid {color};padding:2px 8px;border-radius:4px;'>{icon} {c}</span>"


def bullet(items) -> str:
    return "\n".join(f"- {x}" for x in items) if items else "_(없음)_"


# ─── Sidebar ───
with st.sidebar:
    st.header("Engagement")
    engs = list_engagements(conn)
    options = {f"#{e['id']} {e['company_name']} ({e['period_from']}~{e['period_to']})": e["id"] for e in engs}
    choice = st.selectbox("감사 대상", ["(새 engagement)"] + list(options))
    eid = options.get(choice)
    st.session_state["eid"] = eid
    reviewer = st.text_input("Reviewer (검토자)", value=st.session_state.get("reviewer", ""), placeholder="이름")
    st.session_state["reviewer"] = reviewer
    up = rcm_parser.latest_upload(conn, eid) if eid else None
    upload_id = up["id"] if up else None
    if up:
        st.caption(f"RCM: {up['filename']} (upload #{up['id']})")

    st.divider()
    can_llm = llm_available()
    use_llm = st.toggle("LLM 보조 사용 (선택)", value=False, disabled=not can_llm,
                        help="사례 구조화·coverage 검토 의견에 Claude 사용. 결정론 결과를 더 유리하게 바꾸지 못함.")
    if not can_llm:
        st.caption("ANTHROPIC_API_KEY 미설정 — 결정론 모드로 동작")
    elif use_llm:
        st.warning("후보 공시 문맥과 관련 RCM 행이 Anthropic API로 전송됩니다. 고객 정보 반출 정책을 확인하세요.")
    llm = LLMClient(conn, model=DEFAULT_MODEL) if (use_llm and can_llm) else None

    st.divider()
    page = st.radio("화면", ["Dashboard", "1. Engagement Profile", "2. Peer Selection", "3. Fraud Cases", "4. Fraud Scenarios",
                             "5. RCM Upload", "6. Additional Evidence", "7. Coverage & Audit Response", "8. Review & Export"])
    st.divider()
    if st.button("샘플 데이터로 데모 생성", help="가상 회사·가상 공시·샘플 RCM으로 전체 흐름을 생성 (DART·LLM 호출 없음)"):
        import run_sample

        r = run_sample.run(str(DEFAULT_DB_PATH), None, quiet=True)
        st.success(f"샘플 engagement #{r['engagement_id']} 생성 — 상단에서 선택하세요")
        st.rerun()
    st.caption(f"DB: {DEFAULT_DB_PATH}")


def need_engagement() -> bool:
    if not eid:
        st.info("왼쪽에서 engagement 를 선택하거나 '1. Engagement Profile'에서 새로 만드세요.")
        return False
    return True


def review_widget(table: str, key: dict, current: dict, label: str = "검토") -> None:
    with st.form(f"rv-{table}-{json.dumps(key, sort_keys=True)}"):
        c1, c2 = st.columns([1, 3])
        status = c1.selectbox(f"{label} Review Status", REVIEW_STATUSES, index=REVIEW_STATUSES.index(current.get("review_status") or "Not Reviewed"))
        comment = c2.text_input("Reviewer Comment", value=current.get("reviewer_comment") or "")
        if st.form_submit_button("검토 저장"):
            if not reviewer:
                st.error("사이드바에 Reviewer 이름을 입력하세요.")
            else:
                set_review(conn, table, key, reviewer, status, comment, eid)
                st.success("저장됨")
                st.rerun()
        if current.get("reviewer"):
            st.caption(f"현재: {current.get('review_status')} · {current.get('reviewer')} — {current.get('reviewer_comment') or ''}")


def render_trace(scenario_id: str) -> None:
    """Scenario → Peer case → DART filing → 원문 근거 → 분류 → RCM 위험·통제 → coverage → 감사 대응"""
    t = evidence_trace.trace(conn, eid, upload_id, scenario_id)
    s = t["scenario"]
    d = s["definition"]
    st.markdown(f"#### {scenario_id} {d['title']} ({d['title_ko']})")
    st.write(d["description"])
    st.caption(f"Mechanism: {d['mechanism']}  ·  계정: {', '.join(d['accounts'])}  ·  Process: {', '.join(d['processes'])}  ·  Assertion: {', '.join(d['assertions'])}")
    cov = t["coverage"]
    if cov:
        st.markdown(cov_badge(cov["coverage"]) + f" &nbsp; confidence {cov['confidence']}", unsafe_allow_html=True)

    with st.expander(f"View source filing / View evidence — peer 사례 {len(t['cases'])}건 (사례 수 ≠ 발생 확률)", expanded=False):
        for c in t["cases"]:
            case, f = c["case"], c["filing"]
            kind = {"fraud_incident": "실제 부정 사례", "control_deficiency_disclosure": "통제 미비 공시"}.get(case["case_kind"], case["case_kind"])
            st.markdown(f"**{case['company_name']}** · {kind} · [{f.get('report_nm') or case.get('report_name')} ({case['rcept_no']})]({f.get('source_url') or case.get('source_url')})")
            for e in c["evidence"]:
                st.markdown(f"> {e['text']}  \n<sub>{e.get('section','')} · {e.get('page_or_location','')}</sub>", unsafe_allow_html=True)
            fe = c["field_evidence"]
            st.dataframe(pd.DataFrame([
                {"필드": k, "값": case.get(k) if not isinstance(case.get(k), list) else ", ".join(map(str, case.get(k))), "근거 문장": fe.get(k, "— (근거 없음 → Not disclosed)")}
                for k in ["fraud_type", "actor_position", "amount", "period_of_fraud", "cash_mechanism", "affected_accounts", "related_party_involved", "control_circumvention_method", "detection_method"]
            ]), hide_index=True, use_container_width=True)
            cl = c["classification"]
            st.caption(f"Scenario classification: {cl['rationale']} (method {cl['method']}, 용어 {', '.join(cl['matched_terms'][:6])})")
            st.divider()

    with st.expander(f"View matching controls / Why was this matched? — 상위 {len(t['controls'])}개", expanded=False):
        if not t["controls"]:
            st.write("관련 RCM 통제가 확인되지 않음")
        for m in t["controls"]:
            st.markdown(f"**#{m['rank']} {m['control_id'] or 'row ' + str(m['row_index'] + 1)}** (score {m['match_score']}){' · ⚠ 모호한 기술' if m['is_vague'] else ''}")
            st.markdown(f"- Risk ({m['risk_id'] or '-'}): {m['risk_description']}\n- Control: {m['control_description']}")
            st.text(m["why_matched"] or "")
            if m["vague_reasons"]:
                st.caption("모호 사유: " + "; ".join(m["vague_reasons"]))
            with st.popover("12개 요소 점수"):
                st.json(m["factor_scores"])

    if cov:
        with st.expander(f"Why is this {cov['coverage']}?", expanded=cov["coverage"] != "Covered"):
            st.write(cov["rationale"])
            c1, c2 = st.columns(2)
            c1.markdown("**Covered elements**\n" + bullet(cov["covered_elements"]))
            c2.markdown("**Uncovered elements**\n" + bullet(cov["uncovered_elements"]))
            st.markdown("**Possible circumvention**\n" + bullet(cov["possible_circumvention"]))
            st.markdown("**Information needed**\n" + bullet(cov["information_needed"]))
            st.caption(cov["design_vs_operating_note"])
            if cov.get("llm_notes"):
                st.caption(cov["llm_notes"])
            review_widget("coverage_results", {"engagement_id": eid, "upload_id": upload_id, "scenario_id": scenario_id}, cov, "Coverage")

    a = t["audit_consideration"]
    if a:
        with st.expander("Suggested audit response", expanded=cov is not None and cov["coverage"] != "Covered"):
            st.markdown(f"**Why this matters**  \n{a['why_this_matters']}")
            c1, c2 = st.columns(2)
            c1.markdown(f"**Control Coverage Gap** (통제 관점 — deficiency 판정 아님)  \n{a['control_coverage_gap']}")
            c2.markdown(f"**Audit Response Consideration** (감사 관점)  \n{a['audit_response_consideration']}")
            st.markdown("**Auditor Follow-up Questions**\n" + bullet(a["followup_questions"]))
            st.markdown("**Suggested Audit Procedures**\n" + bullet(a["suggested_procedures"]))
            st.markdown("**Potential Evidence**\n" + bullet(a["potential_evidence"]))
            st.markdown(f"**Possible Effect on Risk Assessment**: {a['risk_assessment_effect']}")
            st.markdown(f"**Conclusion: {a['conclusion']}**")
            review_widget("audit_considerations", {"engagement_id": eid, "upload_id": upload_id, "scenario_id": scenario_id}, a, "Audit consideration")


# ─── Pages ───
st.caption(DISCLAIMER)

if page == "Dashboard":
    st.title("Auditor Review Dashboard")
    if need_engagement():
        p = load_engagement(conn, eid)
        st.markdown(f"**{p.company_name}** · {p.industry} / {p.subindustry} · {p.period_from} ~ {p.period_to}")
        s = evidence_trace.summary(conn, eid, upload_id)
        st.subheader("동종업계 실제 자금부정 사례의 fraud mechanism 중 현재 RCM에서 충분히 대응되지 않는 것은?")
        if not upload_id or not s["coverage"]:
            st.info("RCM 업로드 후 '7. Coverage & Audit Response'에서 분석을 실행하면 여기에 표시됩니다.")
        else:
            cv = s["coverage"]
            n = sum(cv.values())
            st.markdown(
                f"### {n} Fraud Scenarios identified\n"
                + "  ·  ".join(f"{COVERAGE_STYLE[k][0]} {cv.get(k, 0)} {k}" for k in ["Covered", "Partially Covered", "Not Covered", "Cannot Determine"])
            )
            areas = evidence_trace.attention_areas(conn, eid, upload_id)
            st.markdown("**Areas requiring auditor attention** (검토 순서 — 위험 순위 아님)")
            for i, a in enumerate(areas, 1):
                with st.expander(f"{i}. {a['scenario_title']} — {cov_label(a['coverage'])} · peer 사례 {len(a['external_cases'])}건"):
                    render_trace(a["scenario_id"])

        st.divider()
        st.subheader("Summary")
        m = st.columns(5)
        m[0].metric("Benchmark companies selected", s["benchmark_companies_selected"])
        m[1].metric("DART filings reviewed", s["dart_filings_reviewed"])
        m[2].metric("Relevant fraud cases", s["relevant_fraud_cases"], help=f"통제 미비 공시 {s['deficiency_disclosures']}건 별도 · 단순 키워드 hit {s['keyword_hits_excluded']}건 제외")
        m[3].metric("Fraud scenarios identified", s["fraud_scenarios_identified"])
        m[4].metric("RCM controls reviewed", s["rcm_controls_reviewed"])

        if upload_id and s["coverage"]:
            st.subheader("Scenario Table")
            res = sorted(rows(conn, "SELECT * FROM coverage_results WHERE engagement_id=? AND upload_id=?", [eid, upload_id]),
                         key=lambda r: COVERAGE_ORDER.index(r["coverage"]))
            aud = {a["scenario_id"]: a for a in rows(conn, "SELECT scenario_id, risk_assessment_effect, review_status FROM audit_considerations WHERE engagement_id=? AND upload_id=?", [eid, upload_id])}
            table = pd.DataFrame([{
                "Scenario": r["scenario_title"],
                "External cases": len(unj(r["external_cases"], [])),
                "Matched controls": ", ".join(m["control_id"] for m in unj(r["matched_controls"], [])) or "—",
                "Coverage": cov_label(r["coverage"]),
                "Audit consideration": aud.get(r["scenario_id"], {}).get("risk_assessment_effect", ""),
                "Review": r["review_status"],
                "_sid": r["scenario_id"],
            } for r in res])
            ev = st.dataframe(table.drop(columns=["_sid"]), hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row", key="scen_table")
            sel = ev.selection.rows if ev and hasattr(ev, "selection") else []
            if sel:
                render_trace(table.iloc[sel[0]]["_sid"])

            st.subheader("Fraud Scenario × RCM Coverage")
            st.caption("행 = 시나리오. 표시는 RCM 기술 기준 대응 상태(설계)이며 위험등급이 아님. peer 사례 수가 많다고 해서 fraud risk 가 더 높다는 뜻이 아님.")
            mat = pd.DataFrame([{
                "Scenario": r["scenario_title"],
                "Peer case count": len(unj(r["external_cases"], [])),
                "Coverage": r["coverage"],
                "Relevant controls": ", ".join(m["control_id"] for m in unj(r["matched_controls"], [])) or "—",
            } for r in res])

            def _style(v):
                # 옅은 상태색 배경 + 진한 본문색 텍스트(아이콘+라벨) — 색만으로 의미를 전달하지 않음
                if v in COVERAGE_STYLE:
                    return f"background-color: {COVERAGE_STYLE[v][1]}33; color: #1a1a19;"
                return ""

            styled = mat.assign(Coverage=mat["Coverage"]).style.map(_style, subset=["Coverage"]).format({"Coverage": cov_label})
            st.dataframe(styled, hide_index=True, use_container_width=True)

elif page == "1. Engagement Profile":
    st.title("Engagement Profile")
    p = load_engagement(conn, eid) if eid else EngagementProfile(company_name="", period_from="2024-01-01", period_to="2026-09-30")
    q = st.text_input("KSIC 업종 검색 (업종명 일부 또는 코드)", value="")
    found = peer_selector.search_ksic(q) if q else []
    with st.form("profile"):
        c1, c2 = st.columns(2)
        company = c1.text_input("대상 회사명 *", p.company_name)
        market = c2.selectbox("상장시장", ["KOSPI", "KOSDAQ", "KONEX", "비상장"], index=["KOSPI", "KOSDAQ", "KONEX", "비상장"].index(p.market) if p.market in ["KOSPI", "KOSDAQ", "KONEX", "비상장"] else 1)
        industry = c1.text_input("업종", p.industry)
        sub = c2.text_input("세부 업종", p.subindustry)
        ksic_opts = sorted(set(p.ksic_codes) | {c for c, _ in found})
        names = peer_selector.ksic_names()
        ksic = st.multiselect("KSIC 업종코드 (peer 선정 기준, 접두 일치)", ksic_opts, default=p.ksic_codes, format_func=lambda c: f"{c} {names.get(c, '')}")
        c3, c4, c5 = st.columns(3)
        basis = c3.selectbox("규모 기준", ["market_cap", "revenue", "assets"], index=["market_cap", "revenue", "assets"].index(p.size_basis) if p.size_basis in ["market_cap", "revenue", "assets"] else 0,
                             help="peer 측은 시가총액 데이터만 보유 → 규모 유사도는 시가총액 기준일 때만 계산")
        size = c4.number_input("규모 (원)", value=float(p.size_value or 0), step=1e9, format="%.0f")
        ovs = c5.selectbox("해외법인", ["모름", "있음", "없음"], index={None: 0, True: 1, False: 2}[p.has_overseas_subsidiary])
        c6, c7 = st.columns(2)
        pf = c6.text_input("분석기간 From (YYYY-MM-DD) *", p.period_from)
        pt = c7.text_input("분석기간 To (YYYY-MM-DD) *", p.period_to)
        desc = st.text_area("주요 사업 설명 (optional — peer 텍스트 유사도에 사용)", p.business_description)
        tre = st.text_area("자금 관련 주요 특징 (optional)", p.treasury_features)
        if st.form_submit_button("저장"):
            if not company:
                st.error("회사명을 입력하세요")
            else:
                prof = EngagementProfile(company_name=company, period_from=pf, period_to=pt, industry=industry, subindustry=sub, ksic_codes=ksic, market=market,
                                         size_basis=basis, size_value=size or None, has_overseas_subsidiary={"모름": None, "있음": True, "없음": False}[ovs],
                                         business_description=desc, treasury_features=tre)
                new_id = save_engagement(conn, prof, eid)
                st.success(f"저장됨 (engagement #{new_id}) — 사이드바에서 선택하세요" if not eid else "저장됨")

elif page == "2. Peer Selection":
    st.title("Peer / Benchmark Company Selection")
    if need_engagement():
        p = load_engagement(conn, eid)
        st.caption("후보·점수·이유는 자동 생성(AI Generated)이며, 최종 peer 는 감사인이 체크박스로 확정합니다.")
        c1, c2 = st.columns([1, 3])
        limit = c1.number_input("후보 수", 5, 100, 30)
        if c2.button("후보 생성 / 갱신"):
            snap, cands = peer_selector.generate_candidates(p, limit=int(limit))
            peer_selector.save_candidates(conn, eid, snap, cands)
            st.success(f"{len(cands)}개 후보 (스냅샷 {snap}) — 기존 선택은 유지")
        pcs = rows(conn, "SELECT * FROM peer_candidates WHERE engagement_id=? ORDER BY selected DESC, similarity_score DESC", [eid])
        if pcs:
            df = pd.DataFrame([{
                "선택": bool(r["selected"]), "회사명": r["company_name"], "corp_code": r["corp_code"], "업종": f"{r['industry_code'] or ''} {r['industry_name'] or ''}",
                "시장": r["market"], "similarity": r["similarity_score"],
                "선정 이유·유사점": " / ".join((unj(r["reasons"], {}) or {}).get("similar", [])),
                "차이점": " / ".join((unj(r["reasons"], {}) or {}).get("different", [])),
                "AI": "Yes" if r["ai_generated"] else "No (직접 추가)",
            } for r in pcs])
            edited = st.data_editor(df, hide_index=True, use_container_width=True, disabled=[c for c in df.columns if c != "선택"],
                                    column_config={"선택": st.column_config.CheckboxColumn(help="감사인이 최종 peer 로 확정"), "similarity": st.column_config.NumberColumn(format="%.3f")})
            if st.button("선택 저장"):
                if not reviewer:
                    st.error("사이드바에 Reviewer 이름을 입력하세요.")
                else:
                    peer_selector.set_selection(conn, eid, set(edited.loc[edited["선택"], "corp_code"]), reviewer)
                    st.success(f"{int(edited['선택'].sum())}개 peer 선택 저장")
            notes = (unj(pcs[0]["reasons"], {}) or {}).get("notes")
            if notes:
                st.caption(" ".join(notes))
        with st.expander("후보 외 회사 직접 추가"):
            name = st.text_input("회사명 검색")
            if name:
                cc = json.loads((REPO_ROOT / "data" / "corp-codes.json").read_text(encoding="utf-8"))
                hits = [c for c in cc if name in c["corp_name"] and c.get("stock_code")][:20]
                pick = st.selectbox("회사", hits, format_func=lambda c: f"{c['corp_name']} ({c['stock_code']}, {c['corp_code']})") if hits else None
                if pick and st.button("추가(선택됨)"):
                    peer_selector.add_manual_peer(conn, eid, pick["corp_code"], pick["stock_code"].zfill(6), pick["corp_name"], reviewer or "auditor")
                    st.rerun()

elif page == "3. Fraud Cases":
    st.title("Fraud Case Collection")
    if need_engagement():
        p = load_engagement(conn, eid)
        peers = peer_selector.selected_peers(conn, eid)
        st.markdown(f"선택된 peer **{len(peers)}개** · 기간 {p.period_from} ~ {p.period_to}")
        t1, t2, t3 = st.tabs(["Phase 1 수집 실행 (OpenDART)", "기존 Phase 1 산출물 가져오기", "이미지 첨부 판독 목록"])
        with t1:
            st.caption("기존 Phase 1 엔진(scripts/fraud-scan/collect.ts)을 그대로 호출 — 제목이 아니라 공시 원문(본문+첨부)을 분석. DART_API_KEY(또는 OPENDART_API_KEY) 필요, Node.js 필요. 중단 후 재실행 시 이어서 처리.")
            if st.button("수집 실행", disabled=not peers):
                box = st.empty()
                lines: list[str] = []

                def on_line(line: str) -> None:
                    lines.append(line)
                    box.code("\n".join(lines[-25:]))

                code, out = phase1_bridge.run_collect(conn, eid, [r["corp_code"] for r in peers], p.period_from, p.period_to, on_line=on_line)
                if code == 0:
                    st.success(f"수집 완료 → 적재 {phase1_bridge.import_output(conn, eid, out)}")
                else:
                    st.error(f"collect.ts 종료 코드 {code} — 로그 확인 후 재실행하면 이어서 처리됩니다. 처리된 분량은 적재합니다.")
                    phase1_bridge.import_output(conn, eid, out)
        with t2:
            path = st.text_input("Phase 1 --out 폴더 (filings.csv, candidates.jsonl)", str(phase1_bridge.DEFAULT_OUT_ROOT / f"engagement_{eid}"))
            if st.button("가져오기"):
                st.success(phase1_bridge.import_output(conn, eid, Path(path)))
        with t3:
            st.caption("감사보고서·운영실태보고서는 스캔 이미지로 첨부되어 OpenDART API로 받을 수 없습니다. 선택한 peer × 사업연도로 판독 작업 목록을 만들고, "
                       "판독은 맥에서 Claude Code 가 DART 화면에서 직접 열어 확대·전사합니다 (.claude/skills/dart-visual-extract). OpenDART 수집 불필요.")
            c1, c2 = st.columns([1, 3])
            if c1.button("판독 작업 목록 생성 (선택 peer × 사업연도)", disabled=not peers):
                st.success(visual_queue.plan(conn, eid))
            if c2.button("판독 기록 → 사례 구조화로 가져오기"):
                st.success(visual_queue.import_to_phase2(conn, eid))
            vs = visual_queue.status_summary(conn, eid)
            st.markdown(f"작업 {vs['total']}건 · 대기 {vs['todo']} · 진행 {vs['in_progress']} · 발견 {vs['done_found']} · 없음 {vs['done_nothing']} · "
                        f"열람불가 {vs['not_available']} · 중단 {vs['blocked']} · 판독 기록 {vs['findings']}건")
            vqs = visual_queue.queue(conn, eid)
            if vqs:
                st.dataframe(pd.DataFrame([{"우선순위": q["priority"], "회사": q["corp_name"], "보고서": q["report_nm"], "접수번호": q["rcept_no"] or "—",
                                            "상태": q["status"], "메모": q["status_note"], "뷰어": q["viewer_url"]} for q in vqs]),
                             hide_index=True, use_container_width=True, column_config={"뷰어": st.column_config.LinkColumn()})
                st.code(f"python scripts/fraud-scan/phase2/visual_queue.py next --engagement {eid}", language="bash")
            for fd in rows(conn, "SELECT * FROM visual_findings WHERE engagement_id=? ORDER BY id DESC", [eid]):
                with st.expander(f"{fd['rcept_no']} · {fd['page']} · {fd['section']} · 전사 신뢰도 {fd['transcription_confidence']}"):
                    st.text(fd["transcription"])
                    st.markdown(f"> {fd['evidence']}")
                    st.caption(f"{fd['method']} · {fd['recorded_by']} · {fd['created_at']} {fd['notes'] or ''}")
        st.divider()
        if st.button("사례 구조화 · 시나리오 분류 실행" + (" (LLM 보조)" if llm else "")):
            with st.spinner("처리 중"):
                stats = evidence_trace.run_cases(conn, eid, llm=llm)
            st.success(f"실제 부정 {stats['fraud_incident']} / 통제 미비 공시 {stats['control_deficiency_disclosure']} / 단순 키워드 hit {stats['keyword_hit']} · 분류 {stats['classification']}")
            for n in stats.get("notes", [])[:10]:
                st.caption(n)
        kinds = st.multiselect("표시 유형", ["fraud_incident", "control_deficiency_disclosure", "keyword_hit"], default=["fraud_incident", "control_deficiency_disclosure"])
        cases = fraud_case_engine.load_cases(conn, eid, tuple(kinds) or ("fraud_incident",)) if kinds else []
        for c in cases:
            with st.expander(f"{c['company_name']} · {c['filing_date']} · {c['report_name']} · {c['case_kind']} · {c['fraud_type']} · {c['review_status']}"):
                st.caption(f"판정 근거: {c['case_basis']} · 추출: {c['extraction_method']} · confidence {c['confidence']} · [원문]({c['source_url']})")
                st.markdown(f"**Scheme**: {c['fraud_scheme']}")
                st.json({k: c[k] for k in ["actor", "actor_position", "period_of_fraud", "amount", "affected_accounts", "cash_mechanism", "transaction_type",
                                            "related_party_involved", "control_weakness", "control_circumvention_method", "detection_method", "remediation"]}, expanded=False)
                st.markdown("**Source evidence**")
                for e in c["source_evidence"]:
                    st.markdown(f"> {e['text']}")
                review_widget("fraud_cases", {"engagement_id": eid, "case_id": c["case_id"]}, c, "Case")

elif page == "4. Fraud Scenarios":
    st.title("Fraud Scenario Library & Clustering")
    if need_engagement():
        st.caption("사례 수는 공시된 건수일 뿐 발생 확률·위험등급이 아닙니다.")
        for c in fraud_scenario_engine.cluster(conn, eid):
            if not c["case_ids"]:
                continue
            with st.expander(f"{c['scenario_id']} {c['title']} ({c['title_ko']}) — 실제 사례 {c['peer_incident_count']} · 통제미비 공시 {c['peer_deficiency_disclosure_count']} · {len(c['companies'])}개사"):
                st.write(c["description"])
                st.caption(f"Mechanism: {c['mechanism']}")
                st.markdown(f"계정: {', '.join(c['accounts'])} · Process: {', '.join(c['processes'])} · Assertion: {', '.join(c['assertions'])} · 공시 시기: {c['filing_period']}")
                st.markdown("**Representative cases**")
                for r in c["representative_cases"]:
                    st.markdown(f"- {r['company_name']} [{r['rcept_no']}]({r['source_url']}): {(r['fraud_scheme'] or '')[:160]}  \n  <sub>분류 근거: {r['why']}</sub>", unsafe_allow_html=True)
        st.subheader("New Scenario Candidates (확정 아님)")
        nsc = fraud_scenario_engine.new_scenario_candidates(conn, eid)
        if not nsc:
            st.caption("고정 taxonomy 에 매핑되지 않은 실제 사례 없음")
        for n in nsc:
            st.markdown(f"**{n['proposed_title']}** — {n['mechanism']} · 사례 {', '.join(n['case_ids'])} · `{n['status']}`")
            review_widget("scenario_candidates", {"id": n["id"]}, n, "New scenario")

elif page == "5. RCM Upload":
    st.title("RCM Upload")
    if need_engagement():
        f = st.file_uploader("감사대상회사 RCM (xlsx / csv)", type=["xlsx", "xlsm", "csv"])
        if f:
            data = f.getvalue()
            sheets = rcm_parser.sheet_names(data, f.name)
            sheet = st.selectbox("시트", sheets) if sheets else None
            try:
                df = rcm_parser.read_table(data, f.name, sheet)
            except Exception as e:
                st.error(f"읽기 실패: {e}")
                st.stop()
            st.caption(f"헤더 행 자동 탐지: {df.attrs.get('header_row', 0) + 1}번째 행 · {len(df)}행")
            auto = rcm_parser.auto_map(list(df.columns))
            st.subheader("Column mapping (자동 제안 — 수정 가능)")
            cols = ["(없음)"] + list(df.columns)
            mapping = {}
            grid = st.columns(3)
            for i, (fld, label) in enumerate(rcm_parser.FIELD_LABELS.items()):
                cur = auto.get(fld)
                mapping[fld] = grid[i % 3].selectbox(label, cols, index=cols.index(cur) if cur in cols else 0, key=f"map-{fld}")
            mapping = {k: (None if v == "(없음)" else v) for k, v in mapping.items()}
            errs = rcm_parser.validate_mapping(mapping)
            for e in errs:
                st.error(e)
            st.dataframe(df.head(20), use_container_width=True)
            if st.button("RCM 저장 (원본 파일·원본 행 그대로 보관)", disabled=bool(errs)):
                uid = rcm_parser.save_upload(conn, eid, f.name, data, df, mapping, sheet)
                st.success(f"저장됨 (upload #{uid}) — '7. Coverage & Audit Response'에서 분석 실행")
                st.rerun()
        if upload_id:
            st.subheader(f"현재 RCM (upload #{upload_id})")
            st.dataframe(pd.DataFrame([r["raw"] for r in rcm_parser.load_rcm(conn, upload_id)]), use_container_width=True)

elif page == "6. Additional Evidence":
    st.title("Additional Evidence & Contradictions")
    if need_engagement():
        st.caption("인터뷰·walkthrough·문서의 진술을 기록하면 RCM 기술과 상충하는 사항을 찾아 표시합니다. 어느 진술이 사실인지는 판단하지 않습니다.")
        with st.form("ev"):
            c1, c2 = st.columns(2)
            stype = c1.selectbox("유형", ["interview", "walkthrough", "document", "other"])
            sname = c2.text_input("출처 (예: 재무팀 인터뷰 2026-09-15)")
            stmt = st.text_area("진술 / 기재 내용")
            if st.form_submit_button("추가") and stmt:
                contradiction_engine.add_evidence(conn, eid, stype, sname, stmt, reviewer)
                st.rerun()
        up_ev = st.file_uploader("CSV 일괄 추가 (source_type, source_name, statement)", type=["csv"])
        if up_ev and st.button("CSV 추가"):
            for r in csv.DictReader(io.StringIO(up_ev.getvalue().decode("utf-8-sig"))):
                contradiction_engine.add_evidence(conn, eid, r.get("source_type", "other"), r.get("source_name", ""), r["statement"], reviewer)
            st.rerun()
        st.dataframe(pd.DataFrame(rows(conn, "SELECT source_type, source_name, statement, recorded_by, recorded_at FROM additional_evidence WHERE engagement_id=?", [eid])), use_container_width=True)
        if st.button("상충 정보 탐지 실행"):
            st.success(f"{len(contradiction_engine.run(conn, eid, upload_id))}건")
        for c in rows(conn, "SELECT * FROM contradictions WHERE engagement_id=?", [eid]):
            st.warning(f"**{c['status']} – Auditor Follow-up Required** · {c['topic']}")
            st.markdown(f"- {c['source_1']}: \"{c['statement_1']}\"\n- {c['source_2']}: \"{c['statement_2']}\"")
            st.caption(c["auditor_follow_up"])
            review_widget("contradictions", {"id": c["id"]}, c, "Contradiction")

elif page == "7. Coverage & Audit Response":
    st.title("Scenario ↔ RCM Mapping · Coverage · Audit Response")
    if need_engagement():
        if not upload_id:
            st.info("먼저 RCM 을 업로드하세요.")
        else:
            include_all = st.checkbox("peer 사례가 없는 taxonomy 시나리오도 평가", value=False)
            if st.button("분석 실행" + (" (LLM 검토 포함)" if llm else "")):
                with st.spinner("매핑·coverage·감사 고려사항 생성 중"):
                    r = evidence_trace.run_analysis(conn, eid, upload_id, llm=llm, include_all=include_all)
                st.success(r)
            res = rows(conn, "SELECT scenario_id, scenario_title, coverage FROM coverage_results WHERE engagement_id=? AND upload_id=?", [eid, upload_id])
            if res:
                res.sort(key=lambda r: COVERAGE_ORDER.index(r["coverage"]))
                sid = st.selectbox("시나리오", [r["scenario_id"] for r in res], format_func=lambda s: next(f"{r['scenario_title']} — {cov_label(r['coverage'])}" for r in res if r["scenario_id"] == s))
                render_trace(sid)

elif page == "8. Review & Export":
    st.title("Review & Export")
    if need_engagement():
        st.markdown("**Guardrails (모든 자동 결과에 적용)**\n" + "\n".join(f"{i}. {g}" for i, g in enumerate(GUARDRAIL_RULES, 1)))
        if upload_id:
            cov = rows(conn, "SELECT scenario_id, scenario_title, coverage, reviewer, review_status, reviewer_comment FROM coverage_results WHERE engagement_id=? AND upload_id=?", [eid, upload_id])
            if cov:
                st.subheader("Coverage 일괄 검토")
                df = pd.DataFrame(cov)
                ed = st.data_editor(df, hide_index=True, use_container_width=True, disabled=["scenario_id", "scenario_title", "coverage", "reviewer"],
                                    column_config={"review_status": st.column_config.SelectboxColumn(options=list(REVIEW_STATUSES))})
                if st.button("검토 결과 저장"):
                    if not reviewer:
                        st.error("사이드바에 Reviewer 이름을 입력하세요.")
                    else:
                        n = 0
                        for before, after in zip(cov, ed.to_dict(orient="records")):
                            if (before["review_status"], before["reviewer_comment"] or "") != (after["review_status"], after["reviewer_comment"] or ""):
                                set_review(conn, "coverage_results", {"engagement_id": eid, "upload_id": upload_id, "scenario_id": after["scenario_id"]},
                                           reviewer, after["review_status"], after["reviewer_comment"] or "", eid)
                                n += 1
                        st.success(f"{n}건 저장")
        st.subheader("Excel export (감사조서 초안)")
        if st.button("Workbook 생성"):
            data = build_workbook(conn, eid, upload_id)
            st.download_button("다운로드", data, file_name=f"fraud_benchmark_engagement_{eid}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.subheader("Review Log")
        st.dataframe(pd.DataFrame(rows(conn, "SELECT at, entity, entity_key, action, reviewer, review_status, comment FROM review_log WHERE engagement_id=? ORDER BY id DESC", [eid])), use_container_width=True)
