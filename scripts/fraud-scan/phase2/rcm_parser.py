"""RCM 업로드·파싱 (요구사항 9).

- Excel(xlsx) / CSV(UTF-8·CP949) 지원. 상단 제목 행이 있는 양식을 위해 헤더 행을 자동 탐지.
- 회사마다 다른 컬럼명을 표준 필드로 자동 매핑(auto_map) → UI 에서 감사인이 수정 가능.
- 필수 최소 필드: risk_description, control_description. 나머지는 optional.
- 원본 파일 bytes 와 원본 행 전체(JSON)를 그대로 보관한다 (rcm_uploads.raw_bytes, rcm_rows.raw).
"""
from __future__ import annotations

import hashlib
import io
import re

import pandas as pd

from db import j, now, rows, unj

STANDARD_FIELDS: dict[str, list[str]] = {
    "risk_id": ["Risk ID", "RiskID", "Risk No", "리스크ID", "리스크 ID", "위험ID", "위험 ID", "위험번호", "리스크 번호", "Risk #"],
    "risk_description": ["Risk Description", "Risk", "리스크 설명", "리스크", "위험", "위험 내용", "위험설명", "위험 기술", "Risk Statement"],
    "control_id": ["Control ID", "ControlID", "Control No", "통제ID", "통제 ID", "통제번호", "통제 번호", "Control #"],
    "control_description": ["Control Description", "Control Activity", "Control", "통제 설명", "통제설명", "통제활동", "통제 활동", "통제 내용", "통제내용"],
    "process": ["Process", "프로세스", "업무 프로세스", "업무프로세스", "대분류", "Cycle", "사이클"],
    "sub_process": ["Sub-process", "Sub Process", "Subprocess", "하위 프로세스", "하위프로세스", "소분류", "중분류", "세부 프로세스"],
    "control_owner": ["Control Owner", "Owner", "통제 수행자", "통제수행자", "수행자", "담당자", "책임자", "통제책임자", "Performer"],
    "frequency": ["Frequency", "빈도", "수행주기", "수행 주기", "주기"],
    "preventive_detective": ["Preventive / Detective", "Preventive/Detective", "P/D", "예방/적발", "예방 / 적발", "통제유형", "통제 유형", "Control Type"],
    "manual_automated": ["Manual / Automated", "Manual/Automated", "M/A", "수동/자동", "수동 / 자동", "자동화 여부", "자동화"],
    "evidence": ["Evidence", "증빙", "통제 증빙", "테스트 증빙", "증거", "Documentation"],
    "ipe": ["IPE", "정보생성", "IPE 여부", "Information Produced by Entity"],
    "key_control": ["Key Control", "Key Control 여부", "Key 여부", "핵심통제", "핵심 통제", "Key"],
}
REQUIRED = ("risk_description", "control_description")
FIELD_LABELS = {
    "risk_id": "Risk ID", "risk_description": "Risk Description (필수)", "control_id": "Control ID",
    "control_description": "Control Description (필수)", "process": "Process", "sub_process": "Sub-process",
    "control_owner": "Control Owner", "frequency": "Frequency", "preventive_detective": "Preventive / Detective",
    "manual_automated": "Manual / Automated", "evidence": "Evidence", "ipe": "IPE", "key_control": "Key Control 여부",
}


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-/().#]+", "", str(s)).lower()


_ALIAS = {f: {_norm(a) for a in aliases} for f, aliases in STANDARD_FIELDS.items()}


def _header_hits(values) -> int:
    return sum(1 for v in values if isinstance(v, str) and any(_norm(v) in al for al in _ALIAS.values()))


def sheet_names(data: bytes, filename: str) -> list[str]:
    if filename.lower().endswith((".xlsx", ".xlsm", ".xls")):
        return pd.ExcelFile(io.BytesIO(data)).sheet_names
    return []


def read_table(data: bytes, filename: str, sheet: str | None = None) -> pd.DataFrame:
    """원본 → DataFrame(문자열). 헤더 행은 상위 15행 중 표준 컬럼명이 가장 많이 나오는 행."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        raw = pd.read_excel(io.BytesIO(data), sheet_name=sheet or 0, header=None, dtype=str)
    else:
        for enc in ("utf-8-sig", "cp949", "utf-16"):
            try:
                raw = pd.read_csv(io.BytesIO(data), header=None, dtype=str, encoding=enc)
                break
            except (UnicodeDecodeError, pd.errors.ParserError):
                continue
        else:
            raise ValueError("CSV 인코딩을 판별할 수 없습니다 (UTF-8 / CP949 / UTF-16 지원)")
    raw = raw.dropna(how="all").dropna(axis=1, how="all")
    best, best_hits = 0, -1
    for i in range(min(15, len(raw))):
        h = _header_hits(raw.iloc[i].tolist())
        if h > best_hits:
            best, best_hits = i, h
    header = [str(v).strip() if isinstance(v, str) and v.strip() else f"Column{k + 1}" for k, v in enumerate(raw.iloc[best].tolist())]
    seen: dict[str, int] = {}
    uniq = []
    for h in header:  # 중복 컬럼명 구분
        seen[h] = seen.get(h, 0) + 1
        uniq.append(h if seen[h] == 1 else f"{h}.{seen[h]}")
    df = raw.iloc[best + 1 :].copy()
    df.columns = uniq
    df = df.dropna(how="all").fillna("").astype(str).reset_index(drop=True)
    df.attrs["header_row"] = int(best)
    return df


def auto_map(columns: list[str]) -> dict[str, str | None]:
    """표준 필드 → 원본 컬럼. 별칭 정확 일치 우선, 다음은 포함 관계. 한 컬럼은 한 필드에만."""
    mapping: dict[str, str | None] = {f: None for f in STANDARD_FIELDS}
    used: set[str] = set()
    norm_cols = {c: _norm(c) for c in columns}
    for f in STANDARD_FIELDS:  # 1) 정확 일치
        for c, n in norm_cols.items():
            if c not in used and n in _ALIAS[f]:
                mapping[f] = c
                used.add(c)
                break
    for f in STANDARD_FIELDS:  # 2) 포함 관계 (긴 별칭 우선)
        if mapping[f]:
            continue
        for alias in sorted(_ALIAS[f], key=len, reverse=True):
            if len(alias) < 3:
                continue
            c = next((c for c, n in norm_cols.items() if c not in used and alias in n), None)
            if c:
                mapping[f] = c
                used.add(c)
                break
    return mapping


def validate_mapping(mapping: dict[str, str | None]) -> list[str]:
    return [f"필수 필드 미지정: {FIELD_LABELS[f]}" for f in REQUIRED if not mapping.get(f)]


def save_upload(conn, engagement_id: int, filename: str, data: bytes, df: pd.DataFrame, mapping: dict[str, str | None], sheet: str | None = None) -> int:
    errs = validate_mapping(mapping)
    if errs:
        raise ValueError("; ".join(errs))
    with conn:
        cur = conn.execute(
            "INSERT INTO rcm_uploads (engagement_id, filename, sha256, raw_bytes, sheet_name, column_mapping, uploaded_at) VALUES (?,?,?,?,?,?,?)",
            (engagement_id, filename, hashlib.sha256(data).hexdigest(), data, sheet, j(mapping), now()),
        )
        upload_id = int(cur.lastrowid)
        for i, rec in enumerate(df.to_dict(orient="records")):
            std = {f: (rec.get(col, "") if col else "") for f, col in mapping.items()}
            if not (std["risk_description"].strip() or std["control_description"].strip()):
                continue
            conn.execute(
                f"INSERT INTO rcm_rows (upload_id, row_index, raw, {', '.join(STANDARD_FIELDS)}) VALUES (?,?,?,{', '.join('?' for _ in STANDARD_FIELDS)})",
                (upload_id, i, j(rec), *[std[f] for f in STANDARD_FIELDS]),
            )
    return upload_id


def load_rcm(conn, upload_id: int) -> list[dict]:
    out = rows(conn, "SELECT * FROM rcm_rows WHERE upload_id=? ORDER BY row_index", [upload_id])
    for r in out:
        r["raw"] = unj(r["raw"], {})
    return out


def latest_upload(conn, engagement_id: int) -> dict | None:
    r = rows(conn, "SELECT id, filename, sha256, sheet_name, column_mapping, uploaded_at FROM rcm_uploads WHERE engagement_id=? ORDER BY id DESC LIMIT 1", [engagement_id])
    return r[0] if r else None


def control_label(r: dict) -> str:
    return r.get("control_id") or f"row {r['row_index'] + 1}"
