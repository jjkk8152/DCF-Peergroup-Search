"""이미지 전용 첨부 탐지 + 시각 판독 작업 목록 (Phase 2 확장).

배경: 감사보고서·내부회계관리제도 운영실태보고서 등은 사업보고서에 스캔 이미지로 첨부되는 경우가 많아
OpenDART document.xml 의 텍스트로는 내용을 얻을 수 없다. 이 모듈은
  1) Phase 1 이 이미 받아 둔 원문 ZIP 캐시(fraud-scan-output/_cache/zip)를 다시 열어 — 추가 API 호출 없음 —
     '대상 문서이면서 이미지 태그는 있고 텍스트는 거의 없는' 첨부를 찾아 작업 목록(visual_queue)을 만들고,
  2) Claude Code 가 브라우저로 해당 공시를 열어 확대·판독한 내용을 원문 위치와 함께 기록(visual_findings)하며,
  3) 기록을 Phase 1 candidates.jsonl 형식으로 내보내 기존 Phase 2 흐름(사례 구조화 → 시나리오 → coverage)에 연결한다.

CLI (Claude Code 가 Bash 로 호출 — 절차는 .claude/skills/dart-visual-extract/SKILL.md):
  python scripts/fraud-scan/phase2/visual_queue.py scan    --engagement 1
  python scripts/fraud-scan/phase2/visual_queue.py next    --engagement 1
  python scripts/fraud-scan/phase2/visual_queue.py record  --engagement 1 < finding.json
  python scripts/fraud-scan/phase2/visual_queue.py done    --engagement 1 --item <id> --status done_nothing [--note ...]
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
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from db import DEFAULT_DB_PATH, REPO_ROOT, connect, now, rows, upsert  # noqa: E402
from text_utils import quote_in_source  # noqa: E402

DEFAULT_CACHE = REPO_ROOT / "fraud-scan-output" / "_cache" / "zip"
VISUAL_OUT_ROOT = REPO_ROOT / "fraud-scan-output" / "visual"
STATUSES = ("todo", "in_progress", "done_found", "done_nothing", "not_available", "blocked")

# 시각 판독 대상 문서 (우선순위 1 → 3)
TARGET_DOCS = [
    (1, re.compile(r"운영\s*실태|내부\s*회계|평가\s*보고서|자금\s*부정")),
    (2, re.compile(r"감사\s*보고서|검토\s*보고서|감사인의")),
]
IMG_RE = re.compile(r"<(?:IMG|IMAGE)\b", re.IGNORECASE)
IMAGE_ONLY_MAX_CHARS = 500        # 이미지가 있고 본문 텍스트가 이보다 적으면 이미지 전용
IMAGE_ONLY_MAX_CHARS_PER_IMG = 150


def _decode(b: bytes) -> str:
    head = b[:1024].decode("latin1")
    if re.search(r"euc-?kr|cp949|ks_c_5601", head, re.I):
        return b.decode("euc-kr", errors="replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("euc-kr", errors="replace")


def _plain_text(markup: str) -> str:
    t = re.sub(r"<!--[\s\S]*?-->|<(script|style)\b[^>]*>[\s\S]*?</\1>", " ", markup, flags=re.I)
    t = re.sub(r"<DOCUMENT-NAME[^>]*>[\s\S]*?</DOCUMENT-NAME>", " ", t, flags=re.I)  # 문서명은 본문 아님
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"&[#\w]+;", " ", t)
    return re.sub(r"\s+", "", t)


def analyze_zip(path: Path) -> list[dict]:
    """ZIP 내 문서별: 문서명, 이미지 수, 텍스트 글자 수, 대상 여부, 이미지 전용 여부"""
    out = []
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir() or not re.search(r"\.(xml|html?)$", info.filename, re.I):
                continue
            markup = _decode(z.read(info))
            m = re.search(r"<DOCUMENT-NAME[^>]*>([^<]+)<", markup, re.I) or re.search(r"<title[^>]*>([^<]+)<", markup, re.I)
            name = (m.group(1).strip() if m else info.filename)
            n_img = len(IMG_RE.findall(markup))
            chars = len(_plain_text(markup))
            priority = next((p for p, rx in TARGET_DOCS if rx.search(name)), 3)
            image_only = n_img > 0 and (chars < IMAGE_ONLY_MAX_CHARS or chars / n_img < IMAGE_ONLY_MAX_CHARS_PER_IMG)
            out.append({"document_file": info.filename, "document_name": name, "image_count": n_img, "text_chars": chars,
                        "priority": priority, "is_target": priority < 3, "image_only": image_only})
    return out


def scan(conn, engagement_id: int, cache_dir: Path = DEFAULT_CACHE, include_non_target: bool = False) -> dict:
    """dart_filings(Phase 1 적재분)의 ZIP 캐시를 분석해 visual_queue 생성. 기존 진행 상태는 유지."""
    filings = rows(conn, "SELECT * FROM dart_filings WHERE engagement_id=?", [engagement_id])
    stats = {"filings": len(filings), "zip_missing": 0, "zip_broken": 0, "documents": 0, "queued": 0, "text_available_targets": 0}
    with conn:
        for f in filings:
            zp = Path(cache_dir) / f"{f['rcept_no']}.zip"
            if not zp.exists():
                stats["zip_missing"] += 1
                continue
            try:
                docs = analyze_zip(zp)
            except zipfile.BadZipFile:
                stats["zip_broken"] += 1
                continue
            for d in docs:
                stats["documents"] += 1
                if d["is_target"] and not d["image_only"]:
                    stats["text_available_targets"] += 1  # 텍스트로 Phase 1 이 이미 처리
                if not d["image_only"] or not (d["is_target"] or include_non_target):
                    continue
                item_id = f"{f['rcept_no']}:{d['document_file']}"
                upsert(conn, "visual_queue", {
                    "engagement_id": engagement_id, "item_id": item_id, "rcept_no": f["rcept_no"], "corp_code": f["corp_code"],
                    "corp_name": f["corp_name"], "report_nm": f["report_nm"], "rcept_dt": f["rcept_dt"],
                    "document_name": d["document_name"], "document_file": d["document_file"], "image_count": d["image_count"],
                    "text_chars": d["text_chars"],
                    "reason": f"이미지 {d['image_count']}개, 본문 텍스트 {d['text_chars']}자 — 텍스트로 판독 불가",
                    "priority": d["priority"], "viewer_url": f.get("source_url") or f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={f['rcept_no']}",
                    "updated_at": now(),
                }, ["engagement_id", "item_id"], preserve=("status", "status_note"))
                stats["queued"] += 1
    return stats


def queue(conn, engagement_id: int, status: str | None = None) -> list[dict]:
    q = "SELECT * FROM visual_queue WHERE engagement_id=?" + (" AND status=?" if status else "") + " ORDER BY priority, corp_name, rcept_dt"
    return rows(conn, q, [engagement_id, status] if status else [engagement_id])


def next_item(conn, engagement_id: int) -> dict | None:
    for st in ("in_progress", "todo"):  # 중단된 항목부터
        r = queue(conn, engagement_id, st)
        if r:
            return r[0]
    return None


def set_status(conn, engagement_id: int, item_id: str, status: str, note: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"status 는 {STATUSES} 중 하나")
    with conn:
        cur = conn.execute("UPDATE visual_queue SET status=?, status_note=?, updated_at=? WHERE engagement_id=? AND item_id=?",
                           (status, note, now(), engagement_id, item_id))
        if cur.rowcount == 0:
            raise ValueError(f"작업 항목 없음: {item_id}")


def record(conn, engagement_id: int, finding: dict, recorded_by: str = "claude-code") -> int:
    """판독 결과 1건 저장. evidence 는 transcription 안에 그대로 있어야 한다(전사 밖 문장 금지)."""
    for k in ("item_id", "transcription", "evidence", "page"):
        if not str(finding.get(k) or "").strip():
            raise ValueError(f"필수 항목 누락: {k}")
    item = rows(conn, "SELECT * FROM visual_queue WHERE engagement_id=? AND item_id=?", [engagement_id, finding["item_id"]])
    if not item:
        raise ValueError(f"작업 목록에 없는 항목: {finding['item_id']}")
    if not quote_in_source(finding["evidence"], finding["transcription"]):
        raise ValueError("evidence 문장이 transcription 안에 없습니다 — 근거는 전사한 원문에서 그대로 발췌해야 합니다")
    conf = finding.get("transcription_confidence") or "medium"
    if conf not in ("high", "medium", "low"):
        raise ValueError("transcription_confidence 는 high / medium / low")
    with conn:
        cur = conn.execute(
            "INSERT INTO visual_findings (engagement_id, item_id, rcept_no, page, section, transcription, evidence, transcription_confidence, method, notes, recorded_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (engagement_id, finding["item_id"], item[0]["rcept_no"], finding["page"], finding.get("section") or "", finding["transcription"],
             finding["evidence"], conf, finding.get("method") or "vision", finding.get("notes") or "", recorded_by, now()),
        )
    return int(cur.lastrowid)


def export(conn, engagement_id: int, out_dir: Path | None = None) -> Path:
    """visual_findings → Phase 1 형식(candidates.jsonl, filings.csv). Phase 2 import_output 으로 그대로 적재."""
    out_dir = Path(out_dir or VISUAL_OUT_ROOT / f"engagement_{engagement_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    items = {q["item_id"]: q for q in queue(conn, engagement_id)}
    filings_meta = {f["rcept_no"]: f for f in rows(conn, "SELECT * FROM dart_filings WHERE engagement_id=?", [engagement_id])}
    with (out_dir / "candidates.jsonl").open("w", encoding="utf-8") as fh:
        for fd in rows(conn, "SELECT * FROM visual_findings WHERE engagement_id=? ORDER BY id", [engagement_id]):
            q = items.get(fd["item_id"], {})
            meta = filings_meta.get(fd["rcept_no"], {})
            cid = hashlib.sha1(f"{fd['item_id']}|{fd['page']}|{fd['evidence']}".encode()).hexdigest()[:16]
            ev_lines = [l.strip() for l in fd["evidence"].split("\n") if l.strip()]
            context = "\n".join(("▶ " if any(e in line for e in ev_lines) else "  ") + line for line in fd["transcription"].split("\n") if line.strip())
            fh.write(json.dumps({
                "candidate_id": cid, "corp_name": q.get("corp_name"), "stock_code": meta.get("stock_code"), "corp_code": q.get("corp_code"),
                "rcept_no": fd["rcept_no"], "rcept_dt": q.get("rcept_dt"), "report_nm": q.get("report_nm"),
                "document_name": q.get("document_name"), "document_file": f"{q.get('document_file')} · {fd['page']}",
                "section": fd["section"] or q.get("document_name"), "score": None, "category_hints": [], "matched_keywords": [],
                "evidence_text": fd["evidence"], "context": context, "source_url": q.get("viewer_url"), "is_latest": True,
                "source": f"visual transcription ({fd['method']}, confidence {fd['transcription_confidence']})",
            }, ensure_ascii=False) + "\n")
    with (out_dir / "filings.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["corp_name", "stock_code", "corp_code", "rcept_no", "rcept_dt", "report_nm", "status", "is_latest", "original_rcept_no", "source_url"])
        for rno in sorted({q["rcept_no"] for q in items.values() if q["status"] in ("done_found", "done_nothing")}):
            q = next(x for x in items.values() if x["rcept_no"] == rno)
            meta = filings_meta.get(rno, {})
            w.writerow([q["corp_name"], meta.get("stock_code"), q["corp_code"], rno, q["rcept_dt"], q["report_nm"], "ok", "true", meta.get("original_rcept_no") or rno, q["viewer_url"]])
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
    ap.add_argument("cmd", choices=["scan", "next", "list", "record", "done", "status", "export", "import", "ocr"])
    ap.add_argument("path", nargs="?", help="ocr 대상 파일")
    ap.add_argument("--engagement", type=int)
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    ap.add_argument("--item")
    ap.add_argument("--status")
    ap.add_argument("--note", default="")
    ap.add_argument("--include-non-target", action="store_true", help="대상 문서명이 아니어도 이미지 전용이면 목록에 포함")
    a = ap.parse_args(argv)
    out = lambda o: print(json.dumps(o, ensure_ascii=False, indent=2))  # noqa: E731
    if a.cmd == "ocr":
        print(ocr(Path(a.path)))
        return 0
    if not a.engagement:
        ap.error("--engagement 필요")
    conn = connect(a.db)
    if a.cmd == "scan":
        out(scan(conn, a.engagement, Path(a.cache), a.include_non_target))
    elif a.cmd == "next":
        item = next_item(conn, a.engagement)
        if item:
            set_status(conn, a.engagement, item["item_id"], "in_progress")
        out(item or {"message": "남은 작업 없음"})
    elif a.cmd == "list":
        out(queue(conn, a.engagement, a.status))
    elif a.cmd == "record":
        out({"finding_id": record(conn, a.engagement, json.loads(sys.stdin.read()))})
    elif a.cmd == "done":
        set_status(conn, a.engagement, a.item, a.status, a.note)
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
