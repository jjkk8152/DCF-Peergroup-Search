"""Phase 1(TypeScript) 수집 엔진 연동 — 코드를 다시 쓰지 않고 기존 CLI를 호출하고 산출물을 적재한다.

  run_collect  : npx tsx scripts/fraud-scan/collect.ts --corp … --from … --to … --out …
                 (list.json → document.xml 원문 ZIP → chunk → fraud/cash/control 조합 채점 → 후보+문맥)
  import_output: Phase 1 산출물(filings.csv, candidates.jsonl)을 SQLite 로 적재. 파일은 수정하지 않는다.
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Iterable

from db import REPO_ROOT, j, now, rows, upsert

COLLECT_TS = REPO_ROOT / "scripts" / "fraud-scan" / "collect.ts"
DEFAULT_OUT_ROOT = REPO_ROOT / "fraud-scan-output" / "phase2"


def collect_command(corp_codes: Iterable[str], period_from: str, period_to: str, out_dir: Path, extra: list[str] | None = None) -> list[str]:
    npx = shutil.which("npx") or "npx"  # 윈도우는 npx.cmd 경로가 잡힌다
    return [
        npx, "--yes", "tsx", str(COLLECT_TS),
        "--corp", ",".join(corp_codes),
        "--from", period_from.replace("-", ""),
        "--to", period_to.replace("-", ""),
        "--out", str(out_dir),
        *(extra or []),
    ]


def run_collect(conn, engagement_id: int, corp_codes: list[str], period_from: str, period_to: str,
                out_dir: Path | None = None, extra: list[str] | None = None,
                on_line: Callable[[str], None] | None = None) -> tuple[int, Path]:
    """Phase 1 collect.ts 실행. (exit code, out_dir). 중단 후 재실행하면 Phase 1이 이어서 처리한다."""
    out_dir = out_dir or DEFAULT_OUT_ROOT / f"engagement_{engagement_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = collect_command(corp_codes, period_from, period_to, out_dir, extra)
    with conn:
        cur = conn.execute(
            "INSERT INTO collection_runs (engagement_id, out_dir, command, status, started_at) VALUES (?,?,?,?,?)",
            (engagement_id, str(out_dir), " ".join(cmd), "running", now()),
        )
        run_id = cur.lastrowid
    tail: list[str] = []
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-40:]
        if on_line:
            on_line(line)
    code = proc.wait()
    with conn:
        conn.execute(
            "UPDATE collection_runs SET status=?, log_tail=?, finished_at=? WHERE id=?",
            ("ok" if code == 0 else f"exit {code}", "\n".join(tail), now(), run_id),
        )
    return code, out_dir


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass  # 중단으로 잘린 줄
    return out


def import_output(conn, engagement_id: int, out_dir: Path) -> dict:
    """Phase 1 산출물 적재. 같은 out_dir 를 다시 적재해도 중복되지 않는다(upsert)."""
    out_dir = Path(out_dir)
    filings = _read_csv(out_dir / "filings.csv")
    cands = _read_jsonl(out_dir / "candidates.jsonl")
    with conn:
        for f in filings:
            upsert(conn, "dart_filings", {
                "engagement_id": engagement_id,
                "rcept_no": f.get("rcept_no"),
                "corp_code": f.get("corp_code"),
                "corp_name": f.get("corp_name"),
                "stock_code": f.get("stock_code"),
                "report_nm": f.get("report_nm"),
                "rcept_dt": f.get("rcept_dt"),
                "status": f.get("status"),
                "is_latest": 1 if str(f.get("is_latest")).lower() == "true" else 0,
                "original_rcept_no": f.get("original_rcept_no"),
                "source_url": f.get("source_url"),
            }, ["engagement_id", "rcept_no"])
        for c in cands:
            upsert(conn, "source_candidates", {
                "engagement_id": engagement_id,
                "candidate_id": c["candidate_id"],
                "rcept_no": c["rcept_no"],
                "corp_code": c.get("corp_code"),
                "corp_name": c.get("corp_name"),
                "stock_code": c.get("stock_code"),
                "rcept_dt": c.get("rcept_dt"),
                "report_nm": c.get("report_nm"),
                "document_name": c.get("document_name"),
                "document_file": c.get("document_file"),
                "section": c.get("section"),
                "score": c.get("score"),
                "category_hints": j(c.get("category_hints", [])),
                "matched_keywords": j(c.get("matched_keywords", [])),
                "evidence_text": c.get("evidence_text"),
                "context": c.get("context"),
                "source_url": c.get("source_url"),
                "is_latest": 1 if c.get("is_latest", True) else 0,
                "raw": j(c),
            }, ["engagement_id", "candidate_id"])
    return {"filings": len(filings), "candidates": len(cands)}


def last_run(conn, engagement_id: int) -> dict | None:
    r = rows(conn, "SELECT * FROM collection_runs WHERE engagement_id=? ORDER BY id DESC LIMIT 1", [engagement_id])
    return r[0] if r else None
