import io

import pandas as pd
import pytest

import fraud_case_engine as fce
import peer_selector
import rcm_parser
from coverage_engine import llm_review
from db import REVIEW_STATUSES, connect, current_version, migrate, rows, set_review
from engagement import EngagementProfile, load_engagement, save_engagement
from guardrails import check_text, enforce_coverage, sanitize
from scenario_lib import scenario
from text_utils import parse_amounts, quote_in_source, vagueness


# ─── DB ───
def test_migration_idempotent(tmp_path):
    p = tmp_path / "x.db"
    c1 = connect(p)
    v = current_version(c1)
    c1.close()
    c2 = connect(p)  # 기존 DB 재오픈 시 추가 migration 없음
    assert current_version(c2) == v and migrate(c2) == v


def test_review_status_and_log(conn):
    eid = save_engagement(conn, EngagementProfile(company_name="X", period_from="2024-01-01", period_to="2024-12-31"))
    conn.execute("INSERT INTO contradictions (engagement_id, topic, statement_1, statement_2) VALUES (?,?,?,?)", (eid, "t", "a", "b"))
    cid = rows(conn, "SELECT id FROM contradictions")[0]["id"]
    assert rows(conn, "SELECT review_status FROM contradictions")[0]["review_status"] == "Not Reviewed"
    set_review(conn, "contradictions", {"id": cid}, "auditor", "Accepted", "확인", eid)
    assert rows(conn, "SELECT review_status, reviewer FROM contradictions")[0] == {"review_status": "Accepted", "reviewer": "auditor"}
    assert rows(conn, "SELECT action FROM review_log")[0]["action"] == "review"
    with pytest.raises(ValueError):
        set_review(conn, "contradictions", {"id": cid}, "a", "Approved")
    assert set(REVIEW_STATUSES) == {"Not Reviewed", "Accepted", "Modified", "Rejected"}


def test_coverage_check_constraint(conn):
    eid = save_engagement(conn, EngagementProfile(company_name="X", period_from="2024-01-01", period_to="2024-12-31"))
    with pytest.raises(Exception):
        conn.execute("INSERT INTO coverage_results (engagement_id, upload_id, scenario_id, coverage) VALUES (?,?,?,?)", (eid, 1, "FS001", "Mostly Covered"))


def test_engagement_roundtrip(conn):
    p = EngagementProfile(company_name="ABC전자", period_from="2024-01-01", period_to="2026-09-30", ksic_codes=["303"], has_overseas_subsidiary=True)
    eid = save_engagement(conn, p)
    assert load_engagement(conn, eid) == p


# ─── RCM parser ───
def _xlsx(df: pd.DataFrame, title_rows: int = 2) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame([["회사 RCM"]]).to_excel(xw, index=False, header=False)
        df.to_excel(xw, index=False, startrow=title_rows)
    return buf.getvalue()


def test_rcm_header_detection_and_auto_map():
    df = pd.DataFrame([["R1", "지급 위험", "C1", "CFO가 승인한다.", "자금", "CFO"]],
                      columns=["Risk ID", "Risk Description", "Control ID", "Control Activity", "Cycle", "Control Owner"])
    out = rcm_parser.read_table(_xlsx(df), "rcm.xlsx")
    m = rcm_parser.auto_map(list(out.columns))
    assert out.attrs["header_row"] == 1
    assert m["risk_description"] == "Risk Description" and m["control_description"] == "Control Activity"
    assert m["process"] == "Cycle" and m["control_owner"] == "Control Owner"
    assert rcm_parser.validate_mapping(m) == []


def test_rcm_csv_cp949_and_required(conn):
    csv_bytes = "위험,통제 내용,담당자\n지급 위험,재무팀장이 승인한다.,재무팀장\n".encode("cp949")
    df = rcm_parser.read_table(csv_bytes, "rcm.csv")
    m = rcm_parser.auto_map(list(df.columns))
    assert m["risk_description"] == "위험" and m["control_description"] == "통제 내용"
    eid = save_engagement(conn, EngagementProfile(company_name="X", period_from="2024-01-01", period_to="2024-12-31"))
    uid = rcm_parser.save_upload(conn, eid, "rcm.csv", csv_bytes, df, m)
    saved = rcm_parser.load_rcm(conn, uid)
    assert saved[0]["raw"] == {"위험": "지급 위험", "통제 내용": "재무팀장이 승인한다.", "담당자": "재무팀장"}  # 원본 행 보존
    assert rows(conn, "SELECT raw_bytes FROM rcm_uploads")[0]["raw_bytes"] == csv_bytes  # 원본 파일 보존
    with pytest.raises(ValueError):
        rcm_parser.save_upload(conn, eid, "rcm.csv", csv_bytes, df, {**m, "control_description": None})


# ─── 사례 추출: 원문에 없는 사실을 만들지 않음 ───
CTX_INCIDENT = "▶ | 1. 사고발생내용 | 당사 재무팀장이 회사 자금을 무단 이체한 횡령 혐의를 확인하였습니다.\n▶ | 2. 횡령등 금액 | 1,850,000,000"


def test_case_rule_extraction_grounded():
    rec = fce.extract_rule({"context": CTX_INCIDENT, "document_name": "횡령ㆍ배임혐의발생", "section": "s"})
    assert rec["case_kind"] == "fraud_incident" and rec["is_actual_case"] == 1
    assert rec["amount"] == 1_850_000_000
    assert rec["actor_position"] == "재무팀장"
    assert rec["period_of_fraud"] == "Not disclosed"  # 원문에 기간 없음 → 만들지 않음
    assert rec["related_party_involved"] is None  # 언급 없음 → False 로 단정하지 않음
    for ev in rec["source_evidence"]:
        assert quote_in_source(ev["text"], CTX_INCIDENT)
    for f, s in rec["field_evidence"].items():
        assert quote_in_source(s, CTX_INCIDENT), f


def test_case_kinds():
    assert fce.classify_kind({"context": "▶ 회사는 횡령 등 자금 부정을 예방하기 위해 법인카드 사용 승인을 매월 수행합니다."})[0] == "keyword_hit"
    assert fce.classify_kind({"context": "▶ OTP를 공동 관리하여 1인이 이체할 수 있는 통제 미비가 확인되어 중요한 취약점으로 판단하였습니다."})[0] == "control_deficiency_disclosure"
    assert fce.classify_kind({"context": "▶ 횡령ㆍ배임혐의발생"})[0] != "fraud_incident"  # 서식 제목만으로는 사건 아님


class FakeLLM:
    def __init__(self, data):
        self.data = data

    def structured(self, purpose, role, user, schema):
        return self.data


def test_llm_extraction_drops_unquoted_fields():
    base = fce.extract_rule({"context": CTX_INCIDENT, "section": "s"})
    fake = {k: {"value": "Not disclosed", "quote": ""} for k in fce._LLM_FIELDS}
    fake.update({"case_kind": "fraud_incident", "case_kind_quote": "횡령 혐의를 확인하였습니다", "confidence": 0.9,
                 "amount": {"value": 9_999_999_999, "quote": "횡령 금액은 99억원이다"},  # 원문에 없는 인용 → 버림
                 "period_of_fraud": {"value": "2023년", "quote": ""}})  # 인용 없음 → 버림
    merged, notes = fce.extract_llm({"context": CTX_INCIDENT, "section": "s"}, base, FakeLLM(fake))
    assert merged["amount"] == 1_850_000_000 and merged["period_of_fraud"] == "Not disclosed"
    assert any("amount" in n for n in notes)


# ─── 가드레일 ───
def test_enforce_coverage_never_upgrades():
    assert enforce_coverage("Partially Covered", "Covered")[0] == "Partially Covered"
    assert enforce_coverage("Cannot Determine", "Covered")[0] == "Cannot Determine"
    assert enforce_coverage("Covered", "Partially Covered")[0] == "Partially Covered"
    assert enforce_coverage("Not Covered", "Cannot Determine")[0] == "Cannot Determine"


def test_sanitize_forbidden_conclusions():
    text, removed = sanitize("통제 기술상 OTP 보관자가 불명확하다. 따라서 이는 유의적 위험으로 판단한다. 한정의견 검토가 필요하다.")
    assert "유의적 위험" not in text and "한정" not in text and "OTP" in text
    assert len(removed) == 2
    assert check_text("감사인의 추가 확인이 필요하다") == []


def test_llm_review_cannot_upgrade_and_checks_quotes():
    from coverage_engine import evaluate
    from conftest import rcm

    rows_ = rcm(("지급", "자금 지급을 적절히 승인한다."))
    base, top = evaluate(scenario("FS003"), rows_, [])
    fake = FakeLLM({"suggested_coverage": "Covered", "rationale": "승인 통제가 있으므로 충분하다.", "rcm_quotes": ["자금 지급을 적절히 승인한다."],
                    "additional_uncovered_elements": [], "additional_followup_questions": ["OTP 보관자는 누구인가?"], "possible_circumvention": [], "confidence": 0.9})
    out = llm_review(base, scenario("FS003"), top, fake)
    assert out["coverage"] == "Cannot Determine"  # LLM의 Covered 제안 미채택
    assert "OTP 보관자는 누구인가?" in out["information_needed"]
    fake2 = FakeLLM({**fake.data, "suggested_coverage": "Not Covered", "rcm_quotes": ["RCM에 없는 문장"]})
    out2 = llm_review(base, scenario("FS003"), top, fake2)
    assert out2["coverage"] == "Cannot Determine" and "일치하지 않아" in out2["llm_notes"]


# ─── 텍스트 유틸 ───
def test_amounts_and_vagueness():
    assert parse_amounts("관련 금액은 약 35억원")[0][0] == 3.5e9
    assert parse_amounts("4,200백만원")[0][0] == 4.2e9
    assert parse_amounts("2024년") == []
    assert vagueness("자금 지급을 적절히 승인한다.")[0] is True
    assert vagueness("자금담당자가 지급전표를 작성하고 CFO가 승인한다.")[0] is False


# ─── Peer 선택 (저장소 실제 스냅샷 사용) ───
def test_peer_candidates_real_snapshot(conn):
    p = EngagementProfile(company_name="ABC전자", period_from="2024-01-01", period_to="2026-09-30", industry="제조업", subindustry="자동차 전장부품",
                          ksic_codes=["303"], market="KOSDAQ", size_basis="market_cap", size_value=3.5e11, has_overseas_subsidiary=True,
                          business_description="자동차용 전장부품을 생산하여 완성차 업체에 납품")
    snap, cands = peer_selector.generate_candidates(p, limit=10)
    assert len(cands) == 10 and all(c["reasons"] for c in cands)
    assert cands == sorted(cands, key=lambda c: -c["similarity_score"])
    assert all(c["industry_code"].startswith("30") for c in cands[:5])
    eid = save_engagement(conn, p)
    peer_selector.save_candidates(conn, eid, snap, cands)
    assert peer_selector.selected_peers(conn, eid) == []  # AI가 확정하지 않음
    peer_selector.set_selection(conn, eid, {cands[0]["corp_code"]}, "auditor")
    peer_selector.save_candidates(conn, eid, snap, cands)  # 재생성해도 선택 유지
    assert [r["corp_code"] for r in peer_selector.selected_peers(conn, eid)] == [cands[0]["corp_code"]]
