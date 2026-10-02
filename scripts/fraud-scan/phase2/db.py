"""Phase 2 SQLite 저장소.

- Phase 1(TypeScript collect/classify)의 파일 산출물(JSONL/CSV)은 그대로 두고, Phase 2는 별도 DB에 적재한다.
  → Phase 1 동작·산출물에 영향 없음 (backward compatible).
- 스키마는 schema_version 기반 증분 migration. 기존 DB를 열면 부족한 migration만 순서대로 적용한다.
- AI가 생성한 모든 결과 테이블에는 검토 컬럼(ai_generated, reviewer, review_status, reviewer_comment)이 있다.
  review_status: Not Reviewed / Accepted / Modified / Rejected — 최종 판단은 reviewer가 한다.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REVIEW_STATUSES = ("Not Reviewed", "Accepted", "Modified", "Rejected")
COVERAGE_VALUES = ("Covered", "Partially Covered", "Not Covered", "Cannot Determine")

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = Path(os.environ.get("PHASE2_DB", REPO_ROOT / "fraud-scan-output" / "phase2.db"))

REVIEW_COLS = """
    ai_generated INTEGER NOT NULL DEFAULT 1,
    reviewer TEXT,
    review_status TEXT NOT NULL DEFAULT 'Not Reviewed',
    reviewer_comment TEXT
"""

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        f"""
        CREATE TABLE engagements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            industry TEXT,
            subindustry TEXT,
            ksic_codes TEXT,             -- JSON 배열 (KSIC 접두)
            market TEXT,
            size_basis TEXT,             -- revenue / assets / market_cap
            size_value REAL,             -- 원
            has_overseas_subsidiary INTEGER,
            period_from TEXT NOT NULL,   -- YYYY-MM-DD
            period_to TEXT NOT NULL,
            business_description TEXT,
            treasury_features TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE peer_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            corp_code TEXT NOT NULL,
            stock_code TEXT,
            company_name TEXT,
            industry_code TEXT,
            industry_name TEXT,
            market TEXT,
            similarity_score REAL,
            score_breakdown TEXT,        -- JSON
            reasons TEXT,                -- JSON 배열 (선정 이유·유사점·차이점)
            snapshot_date TEXT,
            selected INTEGER NOT NULL DEFAULT 0,   -- 감사인이 체크박스로 확정
            selected_by TEXT,
            selected_at TEXT,
            {REVIEW_COLS},
            UNIQUE(engagement_id, corp_code)
        );

        CREATE TABLE collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            out_dir TEXT NOT NULL,
            command TEXT,
            status TEXT,
            log_tail TEXT,
            started_at TEXT,
            finished_at TEXT
        );

        CREATE TABLE dart_filings (
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            rcept_no TEXT NOT NULL,
            corp_code TEXT,
            corp_name TEXT,
            stock_code TEXT,
            report_nm TEXT,
            rcept_dt TEXT,
            status TEXT,
            is_latest INTEGER,
            original_rcept_no TEXT,
            source_url TEXT,
            PRIMARY KEY (engagement_id, rcept_no)
        );

        -- Phase 1 candidates.jsonl 적재본 (키워드 조합 hit — 실제 사례 여부는 아직 미판정)
        CREATE TABLE source_candidates (
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            candidate_id TEXT NOT NULL,
            rcept_no TEXT NOT NULL,
            corp_code TEXT,
            corp_name TEXT,
            stock_code TEXT,
            rcept_dt TEXT,
            report_nm TEXT,
            document_name TEXT,
            document_file TEXT,
            section TEXT,
            score REAL,
            category_hints TEXT,         -- JSON
            matched_keywords TEXT,       -- JSON
            evidence_text TEXT,
            context TEXT,
            source_url TEXT,
            is_latest INTEGER,
            raw TEXT,                    -- 원본 JSON 행 그대로
            PRIMARY KEY (engagement_id, candidate_id)
        );

        CREATE TABLE fraud_cases (
            case_id TEXT NOT NULL,
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            candidate_id TEXT,
            is_actual_case INTEGER NOT NULL,       -- 1 실제 부정 사례 / 0 그 외
            case_kind TEXT NOT NULL,               -- fraud_incident / control_deficiency_disclosure / keyword_hit
            case_basis TEXT,                       -- 판정 근거
            company_name TEXT,
            corp_code TEXT,
            industry TEXT,
            rcept_no TEXT,
            filing_date TEXT,
            report_name TEXT,
            source_url TEXT,
            fraud_type TEXT,
            fraud_scheme TEXT,
            actor TEXT,
            actor_position TEXT,
            period_of_fraud TEXT,
            amount REAL,
            affected_accounts TEXT,                -- JSON
            cash_mechanism TEXT,
            transaction_type TEXT,
            related_party_involved INTEGER,        -- NULL = 미공시
            control_weakness TEXT,                 -- JSON
            control_circumvention_method TEXT,     -- JSON
            detection_method TEXT,
            remediation TEXT,                      -- JSON
            audit_relevance TEXT,
            source_evidence TEXT NOT NULL,         -- JSON [(text, section, page_or_location)] — 비어 있으면 저장 금지
            field_evidence TEXT,                   -- JSON (field → evidence text) 필드별 근거
            confidence REAL,
            extraction_method TEXT,                -- rule / llm
            {REVIEW_COLS},
            PRIMARY KEY (engagement_id, case_id)
        );

        CREATE TABLE scenario_library (
            scenario_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            title_ko TEXT,
            description TEXT,
            mechanism TEXT,
            definition TEXT NOT NULL,              -- JSON 전체 정의(요소·질문·절차 등)
            status TEXT NOT NULL DEFAULT 'Fixed Taxonomy',   -- Fixed Taxonomy / New Scenario Candidate / Approved Candidate
            created_at TEXT
        );

        CREATE TABLE case_scenarios (
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            case_id TEXT NOT NULL,
            scenario_id TEXT NOT NULL,
            match_score REAL,
            matched_terms TEXT,                    -- JSON
            rationale TEXT,
            method TEXT,
            {REVIEW_COLS},
            PRIMARY KEY (engagement_id, case_id, scenario_id)
        );

        CREATE TABLE scenario_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            proposed_title TEXT,
            mechanism TEXT,
            case_ids TEXT,                          -- JSON
            status TEXT NOT NULL DEFAULT 'New Scenario Candidate',
            {REVIEW_COLS}
        );

        CREATE TABLE rcm_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            filename TEXT,
            sha256 TEXT,
            raw_bytes BLOB,                         -- 업로드 원본 파일 그대로
            sheet_name TEXT,
            column_mapping TEXT,                    -- JSON (standard_field → source_column)
            uploaded_at TEXT
        );

        CREATE TABLE rcm_rows (
            upload_id INTEGER NOT NULL REFERENCES rcm_uploads(id) ON DELETE CASCADE,
            row_index INTEGER NOT NULL,             -- 원본 파일 기준 0부터 (헤더 제외)
            raw TEXT NOT NULL,                      -- 원본 행 JSON (모든 컬럼)
            risk_id TEXT, risk_description TEXT, control_id TEXT, control_description TEXT,
            process TEXT, sub_process TEXT, control_owner TEXT, frequency TEXT,
            preventive_detective TEXT, manual_automated TEXT, evidence TEXT, ipe TEXT, key_control TEXT,
            PRIMARY KEY (upload_id, row_index)
        );

        CREATE TABLE additional_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            source_type TEXT,                       -- interview / document / walkthrough / other
            source_name TEXT,
            statement TEXT NOT NULL,
            recorded_by TEXT,
            recorded_at TEXT
        );

        CREATE TABLE scenario_control_matches (
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            upload_id INTEGER NOT NULL,
            scenario_id TEXT NOT NULL,
            row_index INTEGER NOT NULL,
            rank INTEGER,
            match_score REAL,
            factor_scores TEXT,                     -- JSON (12개 요소별)
            matched_elements TEXT,                  -- JSON [(element_id, quote)]
            why_matched TEXT,
            is_vague INTEGER,
            vague_reasons TEXT,                     -- JSON
            {REVIEW_COLS},
            PRIMARY KEY (engagement_id, upload_id, scenario_id, row_index)
        );

        CREATE TABLE coverage_results (
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            upload_id INTEGER NOT NULL,
            scenario_id TEXT NOT NULL,
            scenario_title TEXT,
            external_cases TEXT,                    -- JSON case_id 배열
            matched_risks TEXT,                     -- JSON
            matched_controls TEXT,                  -- JSON
            coverage TEXT NOT NULL CHECK (coverage IN ('Covered','Partially Covered','Not Covered','Cannot Determine')),
            rationale TEXT,
            covered_elements TEXT,                  -- JSON
            uncovered_elements TEXT,                -- JSON
            possible_circumvention TEXT,            -- JSON
            information_needed TEXT,                -- JSON
            design_vs_operating_note TEXT,
            confidence REAL,
            method TEXT,                            -- rule / rule+llm
            llm_notes TEXT,                         -- LLM 검토 의견(가드레일 통과분)
            {REVIEW_COLS},
            PRIMARY KEY (engagement_id, upload_id, scenario_id)
        );

        CREATE TABLE audit_considerations (
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            upload_id INTEGER NOT NULL,
            scenario_id TEXT NOT NULL,
            coverage TEXT,
            control_coverage_gap TEXT,              -- 통제 관점: RCM에서 확인하지 못한 것 (deficiency 판정 아님)
            audit_response_consideration TEXT,      -- 감사 관점: 위험평가·추가절차 고려사항
            why_this_matters TEXT,
            followup_questions TEXT,                -- JSON
            suggested_procedures TEXT,              -- JSON
            potential_evidence TEXT,                -- JSON
            risk_assessment_effect TEXT,
            conclusion TEXT NOT NULL DEFAULT 'Auditor judgment required',
            {REVIEW_COLS},
            PRIMARY KEY (engagement_id, upload_id, scenario_id)
        );

        CREATE TABLE contradictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id INTEGER NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
            topic TEXT,
            source_1 TEXT, statement_1 TEXT,
            source_2 TEXT, statement_2 TEXT,
            status TEXT NOT NULL DEFAULT 'Potential Contradiction',
            auditor_follow_up TEXT,
            {REVIEW_COLS}
        );

        CREATE TABLE llm_cache (
            cache_key TEXT PRIMARY KEY,
            model TEXT,
            purpose TEXT,
            response TEXT,
            created_at TEXT
        );

        CREATE TABLE review_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id INTEGER,
            entity TEXT,
            entity_key TEXT,
            action TEXT,
            reviewer TEXT,
            review_status TEXT,
            comment TEXT,
            at TEXT
        );
        """,
    ),
]

# 검토 컬럼을 가진 테이블과 기본키 (review 업데이트 공용 함수용)
REVIEWABLE_TABLES = {
    "peer_candidates": ("engagement_id", "corp_code"),
    "fraud_cases": ("engagement_id", "case_id"),
    "case_scenarios": ("engagement_id", "case_id", "scenario_id"),
    "scenario_candidates": ("id",),
    "scenario_control_matches": ("engagement_id", "upload_id", "scenario_id", "row_index"),
    "coverage_results": ("engagement_id", "upload_id", "scenario_id"),
    "audit_considerations": ("engagement_id", "upload_id", "scenario_id"),
    "contradictions": ("id",),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """DB 연결 + 미적용 migration 적용. ':memory:' 지원(테스트)."""
    p = str(path or DEFAULT_DB_PATH)
    if p != ":memory:":
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return row["v"] or 0


def migrate(conn: sqlite3.Connection) -> int:
    v = current_version(conn)
    for version, sql in MIGRATIONS:
        if version <= v:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, now()))
        v = version
    return v


def j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def unj(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def upsert(conn: sqlite3.Connection, table: str, data: dict, conflict_keys: Iterable[str], preserve: Iterable[str] = ()) -> None:
    """INSERT … ON CONFLICT DO UPDATE. preserve 컬럼(검토 결과 등)은 기존 값 유지."""
    cols = list(data.keys())
    keys = list(conflict_keys)
    keep = set(preserve) | set(keys)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in keep)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) ON CONFLICT({', '.join(keys)}) DO "
    sql += f"UPDATE SET {updates}" if updates else "NOTHING"
    conn.execute(sql, [data[c] for c in cols])


REVIEW_PRESERVE = ("reviewer", "review_status", "reviewer_comment")


def set_review(conn: sqlite3.Connection, table: str, key: dict, reviewer: str, status: str, comment: str = "", engagement_id: int | None = None) -> None:
    """검토 결과 저장 + review_log 기록. 키는 REVIEWABLE_TABLES 정의를 따라야 한다."""
    if table not in REVIEWABLE_TABLES:
        raise ValueError(f"검토 대상 테이블이 아님: {table}")
    if status not in REVIEW_STATUSES:
        raise ValueError(f"review_status 값 오류: {status}")
    pk = REVIEWABLE_TABLES[table]
    if set(key) != set(pk):
        raise ValueError(f"{table} 키는 {pk} 이어야 함")
    where = " AND ".join(f"{k}=?" for k in pk)
    with conn:
        conn.execute(
            f"UPDATE {table} SET reviewer=?, review_status=?, reviewer_comment=? WHERE {where}",
            [reviewer, status, comment, *[key[k] for k in pk]],
        )
        conn.execute(
            "INSERT INTO review_log (engagement_id, entity, entity_key, action, reviewer, review_status, comment, at) VALUES (?,?,?,?,?,?,?,?)",
            (engagement_id, table, j(key), "review", reviewer, status, comment, now()),
        )


def log_action(conn: sqlite3.Connection, engagement_id: int | None, entity: str, entity_key: Any, action: str, reviewer: str = "", comment: str = "") -> None:
    with conn:
        conn.execute(
            "INSERT INTO review_log (engagement_id, entity, entity_key, action, reviewer, review_status, comment, at) VALUES (?,?,?,?,?,?,?,?)",
            (engagement_id, entity, j(entity_key), action, reviewer, None, comment, now()),
        )
