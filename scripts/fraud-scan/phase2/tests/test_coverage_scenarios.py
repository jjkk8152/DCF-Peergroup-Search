"""요구사항 25 — Test A~E."""
from conftest import rcm

from contradiction_engine import detect, extract_statements
from coverage_engine import evaluate
from scenario_lib import scenario


def test_a_covered_fictitious_vendor():
    """Fraud: 허위거래처 지급 / RCM: 독립 담당자의 신규 거래처 실재성·통장 확인 승인 → Covered"""
    r, top = evaluate(scenario("FS001"), rcm(("허위 거래처 지급 위험", "신규 거래처 등록 시 독립적 담당자가 사업자등록증, 통장사본, 거래실재성을 확인하고 승인.")), ["C-1"])
    assert r["coverage"] == "Covered"
    assert not r["uncovered_elements"]
    assert all("C1" in c for c in r["covered_elements"])  # 근거 통제 인용
    assert top and top[0]["matched_elements"]


def test_b_partial_cfo_otp_override():
    """Fraud: CFO OTP를 이용한 승인 우회 / RCM: CFO 지급승인 → Partially Covered"""
    r, _ = evaluate(scenario("FS003"), rcm(("승인되지 않은 지급", "자금담당자가 지급전표를 작성하고 CFO가 승인한다.")), ["C-1"])
    assert r["coverage"] == "Partially Covered"
    assert any("OTP" in u for u in r["uncovered_elements"])
    assert any("OTP / 인증서 공유" in c and "대응 통제 확인되지 않음" in c for c in r["possible_circumvention"])


def test_c_not_covered_related_party_loan():
    """Fraud: 관계회사 허위 대여금 유출 / RCM: 일반 지급승인만 → Not Covered (일반 통제는 근거와 함께 명시)"""
    r, _ = evaluate(scenario("FS004"), rcm(("부적절한 자금 지급", "자금 지급 시 재무팀장이 지급요청서와 증빙을 검토하여 승인한다.")), ["C-1"])
    assert r["coverage"] in ("Not Covered", "Partially Covered")
    assert r["coverage"] == "Not Covered"
    assert "일반" in r["rationale"] and "C1" in r["rationale"]
    assert len(r["uncovered_elements"]) == 3


def test_d_cannot_determine_vague_rcm():
    """RCM: '자금 지급을 적절히 승인한다.' → Cannot Determine + 정보 부족 사유"""
    r, top = evaluate(scenario("FS003"), rcm(("부적절한 지급", "자금 지급을 적절히 승인한다.")), ["C-1"])
    assert r["coverage"] == "Cannot Determine"
    assert "적절히" in r["rationale"]
    assert "수행자" in r["rationale"] and ("수행 방법" in r["rationale"] or "증빙" in r["rationale"])
    assert top[0]["is_vague"]


def test_e_contradictory_evidence():
    """RCM: OTP는 CFO가 관리 / 추가 증거: OTP를 Finance Manager가 보관 → Potential Contradiction"""
    stmts = extract_statements([("RCM C1", "OTP는 CFO가 관리한다."), ("interview", "OTP를 Finance Manager가 보관한다.")])
    found = detect(stmts)
    assert len(found) == 1
    c = found[0]
    assert c["status"] == "Potential Contradiction"
    assert "Contradictory Information – Auditor Follow-up Required" in c["auditor_follow_up"]
    assert "판단하지 않음" in c["auditor_follow_up"]  # 어느 쪽이 사실인지 정하지 않음


def test_e2_contradiction_blocks_covered():
    """상충 정보가 걸린 RCM 문장만으로는 요소를 충족한 것으로 보지 않는다"""
    rows = rcm(("지급", "자금담당자가 지급전표를 작성하고 CFO가 승인한다."), ("OTP", "인터넷뱅킹 OTP는 CFO가 직접 보관한다."),
               ("뱅킹", "인터넷뱅킹 이체 Maker와 Approver 권한을 시스템에서 분리하고 재무팀장이 권한 목록을 검토한다."))
    base, _ = evaluate(scenario("FS003"), rows, [])
    assert base["coverage"] == "Covered"
    contra = [{"source_1": "RCM C2", "statement_1": "인터넷뱅킹 OTP는 CFO가 직접 보관한다.", "source_2": "interview", "statement_2": "OTP는 Finance Manager가 보관",
               "statement": "인터넷뱅킹 OTP는 CFO가 직접 보관한다."}]
    r, _ = evaluate(scenario("FS003"), rows, [], contra)
    assert r["coverage"] != "Covered"
    assert any("상충" in u for u in r["uncovered_elements"])
    assert any(i.startswith("Contradictory Information") for i in r["information_needed"])


def test_consistent_statements_not_flagged():
    stmts = extract_statements([("RCM C1", "법인인감은 재무팀장이 보관한다."), ("interview", "법인 인감은 재무팀장이 직접 보관합니다.")])
    assert detect(stmts) == []


def test_owner_vs_holder():
    """'자금담당자가 CFO OTP를 보관' — 보관자는 자금담당자 (CFO는 소유자)"""
    s = extract_statements([("walkthrough", "자금담당자가 CFO OTP를 보관하고 있음.")])
    assert s[0]["holder"] == "자금담당자"


def test_no_relevant_controls_not_covered():
    r, _ = evaluate(scenario("FS007"), rcm(("재고 실사", "창고 담당자가 매월 재고 실사를 수행한다.", "창고 담당자")), [])
    assert r["coverage"] == "Not Covered"
