"""DART 이미지 첨부 시각 판독 작업 목록 (Phase 2 확장).

배경: 감사보고서·내부회계관리제도 운영실태보고서는 사업보고서에 스캔 이미지로 첨부되며, 이 이미지는
OpenDART API(document.xml)로 받을 수 없다. 그래서 OpenDART 를 거치지 않고
  1) 감사인이 확정한 peer × 사업연도로 작업 목록(visual_queue)을 만들고,
  2) Claude Code 가 DART 화면에서 회사 → 사업보고서 → 감사보고서·운영실태보고서 첨부를 직접 열어
     확대·판독한 내용을 원문 위치(접수번호·페이지)와 함께 기록(visual_findings)하며,
  3) 기록을 Phase 1 candidates.jsonl 형식으로 내보내 기존 Phase 2 흐름(사례 구조화 → 시나리오 → coverage)에 연결한다.

CLI (Claude Code 가 Bash 로 호출 — 절차는 .claude/skills/dart-visual-extract/SKILL.md):
  python scripts/fraud-scan/phase2/visual_queue.py plan    --engagement 1
  python scripts/fraud-scan/phase2/visual_queue.py next    --engagement 1
  python scripts/fraud-scan/phase2/visual_queue.py record  --engagement 1 < finding.json
  python scripts/fraud-scan/phase2/visual_queue.py done    --engagement 1 --item <id> --status done_nothing --rcept <접수번호> [--note ...]
  python scripts/fraud-scan/phase2/visual_queue.py status  --engagement 1
  python scripts/fraud-scan/phase2/visual_queue.py import  --engagement 1     # 내보내기 + Phase 2 적재 + 사례 구조화
  python scripts/fraud-scan/phase2/visual_queue.py ocr     <image|pdf>        # (선택) tesseract 교차 확인
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from db import DEFAULT_DB_PATH, REPO_ROOT, connect, now, rows, upsert  # noqa: E402
from engagement import load_engagement  # noqa: E402
from text_utils import quote_in_source  # noqa: E402

VISUAL_OUT_ROOT = REPO_ROOT / "fraud-scan-output" / "visual"
STATUSES = ("todo", "in_progress", "done_found", "done_nothing", "not_available", "blocked")
DART_HOME = "https://dart.fss.or.kr/"
TARGET_DOCUMENTS = "감사보고서(연결 포함) / 내부회계관리제도 운영실태보고서·평가보고서 / 내부회계 감사(검토)보고서"
RCEPT_RE = re.compile(r"^\d{14}$")


@lru_cache(maxsize=1)
def _company_industry() -> dict:
    p = REPO_ROOT / "data" / "company-industry.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def fiscal_years(period_from: str, period_to: str) -> list[int]:
    """분석기간 안에 '제출된' 사업보고서의 사업연도. 사업보고서는 결산 다음 해 3월 말까지 제출된다고 본다.
    예: 2024-01-01 ~ 2026-09-30 → [2023, 2024, 2025]"""
    f, t = date.fromisoformat(period_from), date.fromisoformat(period_to)
    last = t.year - 1 if t >= date(t.year, 3, 31) else t.year - 2
    return list(range(f.year - 1, last + 1))


def plan(conn, engagement_id: int, years: list[int] | None = None) -> dict:
    """선택 peer × 사업연도 작업 목록 생성. 기존 진행 상태·접수번호는 유지."""
    p = load_engagement(conn, engagement_id)
    if not p:
        raise ValueError(f"engagement 없음: {engagement_id}")
    peers = rows(conn, "SELECT * FROM peer_candidates WHERE engagement_id=? AND selected=1 ORDER BY company_name", [engagement_id])
    years = years or fiscal_years(p.period_from, p.period_to)
    ci = _company_industry()
    n = 0
    with conn:
        for peer in peers:
            stock = (peer.get("stock_code") or "").zfill(6) if peer.get("stock_code") else ""
            acc = ((ci.get(stock) or {}).get("accMonth") or "12").zfill(2)
            for fy in years:
                item_id = f"{peer['corp_code']}:FY{fy}"
                upsert(conn, "visual_queue", {
                    "engagement_id": engagement_id, "item_id": item_id, "rcept_no": "",
                    "corp_code": peer["corp_code"], "corp_name": peer["company_name"],
                    "report_nm": f"사업보고서 ({fy}.{acc})", "rcept_dt": "",
                    "document_name": TARGET_DOCUMENTS, "document_file": "",
                    "image_count": None, "text_chars": None,
                    "reason": f"선택 peer × 사업연도 {fy} — 첨부 이미지는 API로 받을 수 없어 DART 화면에서 판독",
                    "priority": 1 if fy == years[-1] else 2,  # 최근 사업연도 먼저
                    "viewer_url": DART_HOME, "updated_at": now(),
                }, ["engagement_id", "item_id"], preserve=("status", "status_note", "rcept_no", "rcept_dt", "viewer_url", "report_nm"))
                n += 1
    return {"peers": len(peers), "fiscal_years": years, "items": n}


def queue(conn, engagement_id: int, status: str | None = None) -> list[dict]:
    q = "SELECT * FROM visual_queue WHERE engagement_id=?" + (" AND status=?" if status else "") + " ORDER BY priority, corp_name, item_id"
    return rows(conn, q, [engagement_id, status] if status else [engagement_id])


def next_item(conn, engagement_id: int) -> dict | None:
    for st in ("in_progress", "todo"):  # 중단된 항목부터
        r = queue(conn, engagement_id, st)
        if r:
            return r[0]
    return None


def _attach_filing(conn, engagement_id: int, item_id: str, rcept_no: str | None, report_nm: str | None = None, viewer_url: str | None = None) -> None:
    """Claude 가 DART 화면에서 찾은 실제 공시(접수번호)를 작업 항목에 연결"""
    if not rcept_no:
        return
    if not RCEPT_RE.match(rcept_no):
        raise ValueError(f"접수번호는 14자리 숫자여야 합니다 (뷰어 주소의 rcpNo): {rcept_no}")
    conn.execute(
        "UPDATE visual_queue SET rcept_no=?, rcept_dt=?, report_nm=COALESCE(?, report_nm), viewer_url=? WHERE engagement_id=? AND item_id=?",
        (rcept_no, f"{rcept_no[:4]}-{rcept_no[4:6]}-{rcept_no[6:8]}", report_nm or None,
         viewer_url or f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}", engagement_id, item_id),
    )


def set_status(conn, engagement_id: int, item_id: str, status: str, note: str = "", rcept_no: str | None = None,
               report_nm: str | None = None, viewer_url: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"status 는 {STATUSES} 중 하나")
    item = rows(conn, "SELECT rcept_no FROM visual_queue WHERE engagement_id=? AND item_id=?", [engagement_id, item_id])
    if not item:
        raise ValueError(f"작업 항목 없음: {item_id}")
    if status in ("done_found", "done_nothing") and not (rcept_no or item[0]["rcept_no"]):
        raise ValueError("확인을 마친 항목은 열람한 공시의 접수번호(--rcept)가 필요합니다 — 무엇을 검토했는지 남기기 위해")
    with conn:
        _attach_filing(conn, engagement_id, item_id, rcept_no, report_nm, viewer_url)
        conn.execute("UPDATE visual_queue SET status=?, status_note=?, updated_at=? WHERE engagement_id=? AND item_id=?",
                     (status, note, now(), engagement_id, item_id))


def record(conn, engagement_id: int, finding: dict, recorded_by: str = "claude-code") -> int:
    """판독 결과 1건 저장. evidence 는 transcription 안에 그대로 있어야 한다(전사 밖 문장 금지)."""
    for k in ("item_id", "transcription", "evidence", "page"):
        if not str(finding.get(k) or "").strip():
            raise ValueError(f"필수 항목 누락: {k}")
    item = rows(conn, "SELECT * FROM visual_queue WHERE engagement_id=? AND item_id=?", [engagement_id, finding["item_id"]])
    if not item:
        raise ValueError(f"작업 목록에 없는 항목: {finding['item_id']}")
    rcept_no = finding.get("rcept_no") or item[0]["rcept_no"]
    if not rcept_no:
        raise ValueError("rcept_no(뷰어 주소의 rcpNo, 14자리)가 필요합니다")
    if not quote_in_source(finding["evidence"], finding["transcription"]):
        raise ValueError("evidence 문장이 transcription 안에 없습니다 — 근거는 전사한 원문에서 그대로 발췌해야 합니다")
    conf = finding.get("transcription_confidence") or "medium"
    if conf not in ("high", "medium", "low"):
        raise ValueError("transcription_confidence 는 high / medium / low")
    with conn:
        _attach_filing(conn, engagement_id, finding["item_id"], rcept_no, finding.get("report_nm"), finding.get("viewer_url"))
        cur = conn.execute(
            "INSERT INTO visual_findings (engagement_id, item_id, rcept_no, page, section, transcription, evidence, transcription_confidence, method, notes, recorded_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (engagement_id, finding["item_id"], rcept_no, finding["page"], finding.get("section") or "", finding["transcription"],
             finding["evidence"], conf, finding.get("method") or "vision", finding.get("notes") or "", recorded_by, now()),
        )
    return int(cur.lastrowid)


def export(conn, engagement_id: int, out_dir: Path | None = None) -> Path:
    """visual_findings → Phase 1 형식(candidates.jsonl, filings.csv). Phase 2 import_output 으로 그대로 적재."""
    out_dir = Path(out_dir or VISUAL_OUT_ROOT / f"engagement_{engagement_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    items = {q["item_id"]: q for q in queue(conn, engagement_id)}
    stock = {r["corp_code"]: r["stock_code"] for r in rows(conn, "SELECT corp_code, stock_code FROM peer_candidates WHERE engagement_id=?", [engagement_id])}
    with (out_dir / "candidates.jsonl").open("w", encoding="utf-8") as fh:
        for fd in rows(conn, "SELECT * FROM visual_findings WHERE engagement_id=? ORDER BY id", [engagement_id]):
            q = items.get(fd["item_id"], {})
            cid = hashlib.sha1(f"{fd['item_id']}|{fd['rcept_no']}|{fd['page']}|{fd['evidence']}".encode()).hexdigest()[:16]
            ev_lines = [l.strip() for l in fd["evidence"].split("\n") if l.strip()]
            context = "\n".join(("▶ " if any(e in line for e in ev_lines) else "  ") + line for line in fd["transcription"].split("\n") if line.strip())
            fh.write(json.dumps({
                "candidate_id": cid, "corp_name": q.get("corp_name"), "stock_code": stock.get(q.get("corp_code")), "corp_code": q.get("corp_code"),
                "rcept_no": fd["rcept_no"], "rcept_dt": q.get("rcept_dt"), "report_nm": q.get("report_nm"),
                "document_name": fd["section"] or "첨부 문서", "document_file": fd["page"],
                "section": fd["section"], "score": None, "category_hints": [], "matched_keywords": [],
                "evidence_text": fd["evidence"], "context": context,
                "source_url": q.get("viewer_url") or f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={fd['rcept_no']}", "is_latest": True,
                "source": f"DART 화면 판독 ({fd['method']}, 전사 신뢰도 {fd['transcription_confidence']})",
            }, ensure_ascii=False) + "\n")
    with (out_dir / "filings.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["corp_name", "stock_code", "corp_code", "rcept_no", "rcept_dt", "report_nm", "status", "is_latest", "original_rcept_no", "source_url"])
        for q in items.values():
            if q["rcept_no"] and q["status"] in ("done_found", "done_nothing"):
                w.writerow([q["corp_name"], stock.get(q["corp_code"]), q["corp_code"], q["rcept_no"], q["rcept_dt"], q["report_nm"], "ok", "true", q["rcept_no"], q["viewer_url"]])
    return out_dir


def import_to_phase2(conn, engagement_id: int) -> dict:
    import evidence_trace
    import phase1_bridge

    out = export(conn, engagement_id)
    loaded = phase1_bridge.import_output(conn, engagement_id, out)
    cases = evidence_trace.run_cases(conn, engagement_id)
    return {"export_dir": str(out), "loaded": loaded, "cases": {k: cases[k] for k in ("fraud_incident", "control_deficiency_disclosure", "keyword_hit")}}


def status_summary(conn, engagement_id: int) -> dict:
    by = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM visual_queue WHERE engagement_id=? GROUP BY status", [engagement_id])}
    n_find = rows(conn, "SELECT COUNT(*) n FROM visual_findings WHERE engagement_id=?", [engagement_id])[0]["n"]
    return {"total": sum(by.values()), **{s: by.get(s, 0) for s in STATUSES}, "findings": n_find}


def ocr(path: Path) -> str:
    """(선택) 로컬 tesseract 로 이미지/PDF OCR — Claude 판독과 교차 확인용. macOS: brew install tesseract tesseract-lang poppler"""
    if not shutil.which("tesseract"):
        raise RuntimeError("tesseract 미설치 (macOS: brew install tesseract tesseract-lang)")
    if path.suffix.lower() == ".pdf":
        if not shutil.which("pdftoppm"):
            raise RuntimeError("PDF 는 poppler 필요 (brew install poppler)")
        tmp = path.with_suffix("")
        subprocess.run(["pdftoppm", "-r", "300", "-png", str(path), str(tmp)], check=True)
        pages = sorted(tmp.parent.glob(tmp.name + "-*.png"))
        return "\n\n".join(f"[p.{i + 1}]\n" + ocr(p) for i, p in enumerate(pages))
    r = subprocess.run(["tesseract", str(path), "-", "-l", "kor+eng", "--psm", "6"], capture_output=True, text=True, check=True)
    return r.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["plan", "next", "list", "record", "done", "status", "export", "import", "ocr"])
    ap.add_argument("path", nargs="?", help="ocr 대상 파일")
    ap.add_argument("--engagement", type=int)
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--years", help="사업연도 직접 지정 (예: 2024,2025). 기본: 분석기간에서 계산")
    ap.add_argument("--item")
    ap.add_argument("--status")
    ap.add_argument("--note", default="")
    ap.add_argument("--rcept", help="열람한 공시의 접수번호 (뷰어 주소 rcpNo, 14자리)")
    ap.add_argument("--report", help="실제 보고서명 (예: '사업보고서 (2025.12)')")
    ap.add_argument("--url", help="열람한 뷰어 주소")
    a = ap.parse_args(argv)
    out = lambda o: print(json.dumps(o, ensure_ascii=False, indent=2))  # noqa: E731
    if a.cmd == "ocr":
        print(ocr(Path(a.path)))
        return 0
    if not a.engagement:
        ap.error("--engagement 필요")
    conn = connect(a.db)
    if a.cmd == "plan":
        out(plan(conn, a.engagement, [int(y) for y in a.years.split(",")] if a.years else None))
    elif a.cmd == "next":
        item = next_item(conn, a.engagement)
        if item:
            set_status(conn, a.engagement, item["item_id"], "in_progress")
            item["status"] = "in_progress"
        out(item or {"message": "남은 작업 없음"})
    elif a.cmd == "list":
        out(queue(conn, a.engagement, a.status))
    elif a.cmd == "record":
        out({"finding_id": record(conn, a.engagement, json.loads(sys.stdin.read()))})
    elif a.cmd == "done":
        set_status(conn, a.engagement, a.item, a.status, a.note, a.rcept, a.report, a.url)
        out(status_summary(conn, a.engagement))
    elif a.cmd == "status":
        out(status_summary(conn, a.engagement))
    elif a.cmd == "export":
        out({"export_dir": str(export(conn, a.engagement))})
    elif a.cmd == "import":
        out(import_to_phase2(conn, a.engagement))
    return 0


if __name__ == "__main__":
    sys.exit(main())
