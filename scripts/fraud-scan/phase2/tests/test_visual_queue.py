"""선택 peer × 사업연도 판독 작업 목록 → 판독 기록 → Phase 2 적재 (OpenDART 미사용)."""
import json

import pytest

import run_sample
import visual_queue as vq
from db import rows

RNO = "20250318000123"
TRANS = ("Ⅲ. 자금 부정 통제\n"
         "평가 결과 재무팀 직원이 인터넷뱅킹 OTP를 단독 보관하여 승인 없이 이체할 수 있는 통제 미비가 확인되어 중요한 취약점으로 판단하였습니다.\n"
         "회사는 OTP를 승인권자가 직접 보관하도록 개선할 예정입니다.")
EVID = "평가 결과 재무팀 직원이 인터넷뱅킹 OTP를 단독 보관하여 승인 없이 이체할 수 있는 통제 미비가 확인되어 중요한 취약점으로 판단하였습니다."


@pytest.fixture
def env(tmp_path):
    out = run_sample.run(str(tmp_path / "v.db"), None, quiet=True)
    return out["conn"], out["engagement_id"], str(tmp_path / "v.db")


def test_fiscal_years():
    assert vq.fiscal_years("2024-01-01", "2026-09-30") == [2023, 2024, 2025]
    assert vq.fiscal_years("2024-01-01", "2026-02-28") == [2023, 2024]  # 2025 사업보고서는 아직 제출 전


def test_plan_from_selected_peers(env):
    conn, eid, _ = env
    st = vq.plan(conn, eid)
    assert st == {"peers": 4, "fiscal_years": [2023, 2024, 2025], "items": 12}
    q = vq.queue(conn, eid)
    assert q[0]["priority"] == 1 and q[0]["item_id"].endswith("FY2025")  # 최근 사업연도 먼저
    assert all(x["rcept_no"] == "" and x["status"] == "todo" for x in q)
    vq.set_status(conn, eid, q[0]["item_id"], "done_nothing", "확인", rcept_no=RNO)
    vq.plan(conn, eid)  # 재생성해도 진행 상태·접수번호 유지
    again = next(x for x in vq.queue(conn, eid) if x["item_id"] == q[0]["item_id"])
    assert again["status"] == "done_nothing" and again["rcept_no"] == RNO and again["viewer_url"].endswith(RNO)


def test_done_requires_rcept(env):
    conn, eid, _ = env
    vq.plan(conn, eid)
    item = vq.next_item(conn, eid)
    with pytest.raises(ValueError):  # 무엇을 열람했는지 없이 완료 처리 불가
        vq.set_status(conn, eid, item["item_id"], "done_nothing")
    with pytest.raises(ValueError):
        vq.set_status(conn, eid, item["item_id"], "done_nothing", rcept_no="12345")
    vq.set_status(conn, eid, item["item_id"], "blocked", "보안문자")  # 차단은 접수번호 없이 가능


def test_record_validation_and_import(env):
    conn, eid, _ = env
    vq.plan(conn, eid)
    item = vq.next_item(conn, eid)
    base = {"item_id": item["item_id"], "page": "운영실태보고서 p.2", "section": "내부회계관리제도 운영실태보고서",
            "transcription": TRANS, "evidence": EVID, "transcription_confidence": "high"}
    with pytest.raises(ValueError):  # 접수번호 없음
        vq.record(conn, eid, base)
    with pytest.raises(ValueError):  # 근거가 전사 안에 없음
        vq.record(conn, eid, {**base, "rcept_no": RNO, "evidence": "OTP 공동관리로 횡령이 발생했다"})
    vq.record(conn, eid, {**base, "rcept_no": RNO, "report_nm": "사업보고서 (2025.12)"})
    vq.set_status(conn, eid, item["item_id"], "done_found")  # record 에서 접수번호가 연결됨
    assert vq.status_summary(conn, eid)["done_found"] == 1

    before = rows(conn, "SELECT COUNT(*) n FROM fraud_cases WHERE engagement_id=?", [eid])[0]["n"]
    res = vq.import_to_phase2(conn, eid)
    assert res["loaded"] == {"filings": 1, "candidates": 1}
    assert rows(conn, "SELECT COUNT(*) n FROM fraud_cases WHERE engagement_id=?", [eid])[0]["n"] == before + 1
    c = rows(conn, "SELECT * FROM fraud_cases WHERE engagement_id=? AND rcept_no=?", [eid, RNO])[0]
    assert c["case_kind"] == "control_deficiency_disclosure" and c["report_name"] == "사업보고서 (2025.12)"
    assert any("운영실태보고서 p.2" in e["page_or_location"] for e in json.loads(c["source_evidence"]))  # 원문 위치 추적
    assert c["source_url"].endswith(RNO)


def test_cli(env, capsys):
    conn, eid, db_path = env
    assert vq.main(["plan", "--engagement", str(eid), "--db", db_path]) == 0
    assert vq.main(["next", "--engagement", str(eid), "--db", db_path]) == 0
    out = capsys.readouterr().out
    assert '"status": "in_progress"' in out and "감사보고서" in out
    vq.main(["status", "--engagement", str(eid), "--db", db_path])
    assert '"in_progress": 1' in capsys.readouterr().out
