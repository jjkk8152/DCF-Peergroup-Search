"""이미지 전용 첨부 탐지 → 작업 목록 → 판독 기록 → Phase 2 적재."""
import json
import zipfile

import pytest

import run_sample
import visual_queue as vq
from db import rows

IMG_DOC = """<?xml version="1.0" encoding="utf-8"?><DOCUMENT><DOCUMENT-NAME>내부회계관리제도 운영실태보고서</DOCUMENT-NAME>
<BODY><P><IMG USERMAP="N">image1.jpg</IMG></P><P><IMG USERMAP="N">image2.jpg</IMG></P><P>1/2</P></BODY></DOCUMENT>"""
TEXT_AUDIT = """<?xml version="1.0" encoding="utf-8"?><DOCUMENT><DOCUMENT-NAME>감사보고서</DOCUMENT-NAME><BODY>""" + "<P>회사의 재무제표는 중요성의 관점에서 공정하게 표시하고 있습니다.</P>" * 30 + "<IMG>sign.jpg</IMG></BODY></DOCUMENT>"
IMG_OTHER = """<DOCUMENT><DOCUMENT-NAME>정관</DOCUMENT-NAME><BODY><IMG>a.jpg</IMG></BODY></DOCUMENT>"""
TRANS = ("Ⅲ. 자금 부정 통제\n"
         "평가 결과 재무팀 직원이 인터넷뱅킹 OTP를 단독 보관하여 승인 없이 이체할 수 있는 통제 미비가 확인되어 중요한 취약점으로 판단하였습니다.\n"
         "회사는 OTP를 승인권자가 직접 보관하도록 개선할 예정입니다.")
EVID = "평가 결과 재무팀 직원이 인터넷뱅킹 OTP를 단독 보관하여 승인 없이 이체할 수 있는 통제 미비가 확인되어 중요한 취약점으로 판단하였습니다."


@pytest.fixture
def env(tmp_path):
    out = run_sample.run(str(tmp_path / "v.db"), None, quiet=True)
    conn, eid = out["conn"], out["engagement_id"]
    cache = tmp_path / "cache"
    cache.mkdir()
    f = rows(conn, "SELECT rcept_no FROM dart_filings WHERE engagement_id=? AND report_nm LIKE '사업보고서%' ORDER BY rcept_no", [eid])
    rno = f[0]["rcept_no"]
    with zipfile.ZipFile(cache / f"{rno}.zip", "w") as z:
        z.writestr(f"{rno}.xml", TEXT_AUDIT.replace("감사보고서", "사업보고서"))
        z.writestr(f"{rno}_00760.xml", IMG_DOC)
        z.writestr(f"{rno}_00761.xml", TEXT_AUDIT)
        z.writestr(f"{rno}_00999.xml", IMG_OTHER)
    (cache / "99990901000009.zip").write_bytes(b"not a zip")
    return conn, eid, cache, rno


def test_analyze_and_scan(env):
    conn, eid, cache, rno = env
    docs = {d["document_name"]: d for d in vq.analyze_zip(cache / f"{rno}.zip")}
    assert docs["내부회계관리제도 운영실태보고서"]["image_only"] and docs["내부회계관리제도 운영실태보고서"]["priority"] == 1
    assert not docs["감사보고서"]["image_only"]  # 텍스트 있는 첨부는 Phase 1 이 이미 처리
    assert docs["정관"]["image_only"] and not docs["정관"]["is_target"]
    st = vq.scan(conn, eid, cache)
    assert st["queued"] == 1 and st["zip_broken"] == 1 and st["text_available_targets"] >= 1
    q = vq.queue(conn, eid)
    assert q[0]["document_name"] == "내부회계관리제도 운영실태보고서" and q[0]["viewer_url"].endswith(rno)
    vq.set_status(conn, eid, q[0]["item_id"], "done_nothing", "확인")
    vq.scan(conn, eid, cache)  # 재스캔해도 진행 상태 유지
    assert vq.queue(conn, eid)[0]["status"] == "done_nothing"


def test_record_validation_and_import(env, capsys):
    conn, eid, cache, rno = env
    vq.scan(conn, eid, cache)
    item = vq.next_item(conn, eid)
    with pytest.raises(ValueError):  # 근거가 전사 안에 없으면 거부
        vq.record(conn, eid, {"item_id": item["item_id"], "page": "p.1", "transcription": TRANS, "evidence": "OTP 공동관리로 횡령이 발생했다"})
    with pytest.raises(ValueError):
        vq.record(conn, eid, {"item_id": "없는항목", "page": "p.1", "transcription": TRANS, "evidence": EVID})
    vq.record(conn, eid, {"item_id": item["item_id"], "page": "운영실태보고서 p.2", "section": "Ⅲ. 자금 부정 통제",
                          "transcription": TRANS, "evidence": EVID, "transcription_confidence": "high"})
    vq.set_status(conn, eid, item["item_id"], "done_found")
    assert vq.status_summary(conn, eid)["done_found"] == 1

    before = rows(conn, "SELECT COUNT(*) n FROM fraud_cases WHERE engagement_id=?", [eid])[0]["n"]
    res = vq.import_to_phase2(conn, eid)
    assert res["loaded"]["candidates"] == 1
    cases = rows(conn, "SELECT * FROM fraud_cases WHERE engagement_id=? AND rcept_no=?", [eid, rno])
    assert rows(conn, "SELECT COUNT(*) n FROM fraud_cases WHERE engagement_id=?", [eid])[0]["n"] == before + 1
    c = next(x for x in cases if "OTP" in (x["source_evidence"] or ""))
    assert c["case_kind"] == "control_deficiency_disclosure"
    ev = json.loads(c["source_evidence"])
    assert any("운영실태보고서 p.2" in e["page_or_location"] for e in ev)  # 원문 위치 추적


def test_cli_next_and_status(env, capsys):
    conn, eid, cache, rno = env
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    assert vq.main(["scan", "--engagement", str(eid), "--db", db_path, "--cache", str(cache)]) == 0
    assert vq.main(["next", "--engagement", str(eid), "--db", db_path]) == 0
    out = capsys.readouterr().out
    assert "내부회계관리제도 운영실태보고서" in out
    vq.main(["status", "--engagement", str(eid), "--db", db_path])
    assert '"in_progress": 1' in capsys.readouterr().out
