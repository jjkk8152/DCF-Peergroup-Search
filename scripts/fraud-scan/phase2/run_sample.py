"""샘플 end-to-end 실행 (DART·LLM 호출 없음).

  python scripts/fraud-scan/phase2/run_sample.py [--db fraud-scan-output/phase2_sample.db] [--xlsx out.xlsx]

1) Engagement Profile 생성 → 2) 실제 스냅샷 기반 peer 후보 생성 → 3) 감사인 선택(샘플: 가상 peer 4개 직접 추가)
4) Phase 1 산출물(samples/phase1_output — 가상 공시) 적재 → 5) 사례 구조화·시나리오 분류
6) 샘플 RCM 업로드(자동 컬럼 매핑) → 7·8) 매핑·coverage → 9) 감사 고려사항 → 상충 정보(샘플 인터뷰) → 11) Excel
샘플 회사·공시·접수번호는 모두 가상([SAMPLE])이며 실제 회사의 부정 사례가 아니다.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import contradiction_engine  # noqa: E402
import evidence_trace  # noqa: E402
import peer_selector  # noqa: E402
import phase1_bridge  # noqa: E402
import rcm_parser  # noqa: E402
from db import REPO_ROOT, connect  # noqa: E402
from engagement import EngagementProfile, save_engagement  # noqa: E402
from export_excel import build_workbook  # noqa: E402

SAMPLES = HERE / "samples"
SAMPLE_PEERS = [("99000001", "990001", "[SAMPLE] 가상전장(주)"), ("99000002", "990002", "[SAMPLE] 예시모빌리티(주)"),
                ("99000003", "990003", "[SAMPLE] 테스트부품(주)"), ("99000004", "990004", "[SAMPLE] 샘플오토텍(주)")]


def run(db_path: str, xlsx_path: Path | None = None, quiet: bool = False) -> dict:
    log = (lambda *a: None) if quiet else print
    conn = connect(db_path)
    profile = EngagementProfile(
        company_name="[SAMPLE] ABC전자(가상)", industry="제조업", subindustry="자동차 전장부품", ksic_codes=["303"], market="KOSDAQ",
        size_basis="market_cap", size_value=3.5e11, has_overseas_subsidiary=True, period_from="2024-01-01", period_to="2026-09-30",
        business_description="자동차용 전장부품(와이어링 하네스, 전자제어장치)을 생산하여 완성차 업체에 납품. 중국·멕시코 현지법인 보유",
        treasury_features="인터넷뱅킹 지급, 해외법인 자금 대여",
    )
    eid = save_engagement(conn, profile)
    snap, cands = peer_selector.generate_candidates(profile, limit=15)
    peer_selector.save_candidates(conn, eid, snap, cands)
    log(f"[4] peer 후보 {len(cands)}개 (스냅샷 {snap}) — 상위: " + ", ".join(f"{c['company_name']}({c['similarity_score']})" for c in cands[:3]))
    # 감사인 선택: 이 샘플은 가상 공시를 쓰므로 가상 peer 4개를 직접 추가·선택 (실제 후보는 미선택으로 남김)
    for corp, stock, name in SAMPLE_PEERS:
        peer_selector.add_manual_peer(conn, eid, corp, stock, name, reviewer="sample-auditor")
    stats = phase1_bridge.import_output(conn, eid, SAMPLES / "phase1_output")
    log(f"[5] Phase 1 산출물 적재: {stats}")
    cs = evidence_trace.run_cases(conn, eid)
    log(f"[5] 사례: 실제 부정 {cs['fraud_incident']} / 통제미비 공시 {cs['control_deficiency_disclosure']} / 단순 키워드 {cs['keyword_hit']} — 시나리오 연결 {cs['classification']}")

    data = (SAMPLES / "sample_rcm.xlsx").read_bytes()
    df = rcm_parser.read_table(data, "sample_rcm.xlsx")
    mapping = rcm_parser.auto_map(list(df.columns))
    upload_id = rcm_parser.save_upload(conn, eid, "sample_rcm.xlsx", data, df, mapping)
    log(f"[6] RCM {len(df)}행 (헤더 행 {df.attrs['header_row']}), 자동 매핑: " + ", ".join(f"{k}←{v}" for k, v in mapping.items() if v))

    with (SAMPLES / "sample_additional_evidence.csv").open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            contradiction_engine.add_evidence(conn, eid, r["source_type"], r["source_name"], r["statement"], "sample-auditor")
    res = evidence_trace.run_analysis(conn, eid, upload_id)
    log(f"[7-9] {res}")
    summ = evidence_trace.summary(conn, eid, upload_id)
    log(f"[18] summary: {summ}")
    for a in evidence_trace.attention_areas(conn, eid, upload_id):
        log(f"    ▶ {a['scenario_title']}: {a['coverage']} (peer 사례 {len(a['external_cases'])}건)")
    if xlsx_path:
        xlsx_path.write_bytes(build_workbook(conn, eid, upload_id))
        log(f"[20] Excel: {xlsx_path}")
    return {"engagement_id": eid, "upload_id": upload_id, "summary": summ, "conn": conn}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(REPO_ROOT / "fraud-scan-output" / "phase2_sample.db"))
    ap.add_argument("--xlsx", default=str(REPO_ROOT / "fraud-scan-output" / "phase2_sample.xlsx"))
    a = ap.parse_args()
    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    if Path(a.db).exists():
        Path(a.db).unlink()  # 샘플은 매번 새로
    run(a.db, Path(a.xlsx))
