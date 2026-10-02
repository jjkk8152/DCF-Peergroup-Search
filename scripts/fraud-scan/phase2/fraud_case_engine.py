"""Fraud Case Extraction (요구사항 5·6).

Phase 1 후보(키워드 조합 hit)를 다음 셋으로 구분하고 실제 사례는 구조화한다.
  fraud_incident               : 횡령·배임·유용 등 실제 부정(혐의 포함) 서술 → is_actual_case = 1
  control_deficiency_disclosure: 사건 서술 없이 자금 관련 통제 미비·취약점 공시
  keyword_hit                  : 단순 키워드 조합 (정기 자금부정통제 문구, 일반 통제 설명 등)

원칙: 모든 필드는 원문 문장(field_evidence)에 근거. 근거 문장이 없으면 "Not disclosed"/null.
source_evidence 가 비어 있으면 사례를 저장하지 않는다. LLM 은 선택 기능이며 인용문이 문맥에 없으면 버린다.
"""
from __future__ import annotations

import re

from db import REVIEW_PRESERVE, j, rows, unj, upsert
from text_utils import find_roles, find_terms, has_any, parse_amounts, quote_in_source, sentences_with, split_sentences, squash

ND = "Not disclosed"

FRAUD_ACT_TERMS = ["횡령", "배임", "자금유용", "자금 유용", "유용하였", "유용했", "유용한 사실", "유용 사실", "편취", "착복", "무단 출금", "무단 인출", "무단 송금", "무단 이체",
                   "무단으로", "허위 거래처", "가공 거래", "가공거래", "허위 매입", "가공 매입", "허위 계약", "위조", "변조", "사적 사용", "사적으로 사용", "개인 용도"]
INCIDENT_MARKERS = ["발생", "혐의", "확인", "적발", "고소", "고발", "기소", "판결", "수사", "사실", "발견", "인지", "피소", "선고", "공소"]
DEFICIENCY_TERMS = ["중요한 취약점", "유의한 미비점", "미비점", "통제 미비", "통제미비", "부적정", "비적정", "미흡"]
CASH_TERMS = ["자금", "지급", "계좌", "OTP", "인감", "현금", "예금", "송금", "이체", "출금", "법인카드", "인증서"]

FRAUD_TYPES = [("횡령", ["횡령"]), ("배임", ["배임"]), ("자금유용", ["자금유용", "자금 유용", "유용하였", "유용했", "유용한 사실", "유용 사실"]),
               ("허위·가공거래", ["허위 거래처", "가공 거래", "가공거래", "허위 매입", "가공 매입", "허위 계약", "허위 용역"]),
               ("문서 위·변조", ["위조", "변조"]), ("법인카드 사적 사용", ["사적 사용", "사적으로 사용", "개인 용도"]),
               ("무단 자금 인출·이체", ["무단 출금", "무단 인출", "무단 송금", "무단 이체", "무단으로"])]

ACCOUNT_LEXICON = [("현금및현금성자산", ["현금및현금성자산", "현금 및 현금성자산", "예금", "현금"]), ("단기대여금", ["단기대여금"]), ("장기대여금", ["장기대여금"]),
                   ("대여금", ["대여금"]), ("선급금", ["선급금"]), ("가지급금", ["가지급금"]), ("미수금", ["미수금"]), ("매입채무", ["매입채무"]),
                   ("미지급금", ["미지급금"]), ("재고자산", ["재고자산"]), ("투자자산", ["투자금", "투자자산", "관계기업투자", "출자금"]),
                   ("우발부채(지급보증)", ["지급보증", "채무보증"]), ("담보제공자산", ["담보 제공", "담보제공"])]
CASH_MECHANISMS = [("계좌이체", ["계좌이체", "이체", "송금"]), ("인터넷뱅킹", ["인터넷뱅킹", "펌뱅킹"]), ("현금 인출", ["현금 인출", "현금으로 인출", "인출"]),
                   ("수표·어음", ["수표", "어음"]), ("법인카드", ["법인카드"]), ("OTP·인증서 이용", ["OTP", "인증서", "보안카드"]), ("법인인감 이용", ["인감"])]
TRANSACTION_TYPES = [("자금 대여", ["대여"]), ("선급", ["선급"]), ("투자·출자", ["투자", "출자"]), ("매입·구매", ["매입", "구매"]), ("용역·외주", ["용역", "외주"]),
                     ("지급보증", ["지급보증", "채무보증"]), ("담보제공", ["담보"]), ("급여·경비", ["급여", "경비"]), ("자금 지원", ["자금 지원", "자금지원"])]
CIRCUMVENTION = [
    ("shared ID", [["공용 ID", "공유 ID", "아이디 공유", "ID 공유"]]),
    ("OTP / 인증서 공유", [["OTP", "인증서", "보안카드"], ["공유", "도용", "보관", "대신", "타인"]]),
    ("관리자 권한", [["관리자 권한", "관리자권한"]]),
    ("수동 journal", [["수기", "수동"], ["전표", "분개"]]),
    ("emergency payment", [["긴급 지급", "긴급 송금", "긴급"]]),
    ("master data override", [["마스터", "거래처 정보", "거래처정보"], ["변경", "수정"]]),
    ("segregation of duties override", [["직무분리", "직무 분리", "겸직", "겸임", "1인이 단독", "단독으로"]]),
    ("collusion", [["공모"]]),
    ("management override", [["대표이사", "경영진", "최대주주", "회장"], ["지시", "임의로", "승인 없이", "결의 없이", "이사회 승인 없이"]]),
    ("related party 사용", [["특수관계자", "관계회사", "계열회사", "관계사"]]),
    ("fictitious vendor", [["허위 거래처", "가공 거래처", "유령회사", "페이퍼컴퍼니"]]),
    ("bank account substitution", [["계좌 변경", "계좌번호 변경", "타인 계좌", "차명계좌", "차명 계좌", "개인 계좌"]]),
]
RELATED_PARTY_TERMS = ["특수관계자", "관계회사", "계열회사", "최대주주", "관계사", "대주주"]
DETECTION_SOURCES = ["내부감사", "외부감사", "감사인", "감사위원회", "자체 점검", "자체점검", "제보", "수사기관", "검찰", "경찰", "금융감독원", "거래소", "내부 조사", "내부조사"]
DETECTION_VERBS = ["발견", "확인", "인지", "적발", "통보"]
REMEDIATION_TERMS = ["재발방지", "재발 방지", "개선", "도입", "강화", "신설", "고소", "회수", "법적 조치", "법적조치", "징계", "해임"]
WEAKNESS_TERMS = ["미비", "취약점", "부재", "소홀", "미흡", "우회", "공유", "단독"]
MANAGEMENT_ROLES = {"대표이사", "회장", "최대주주", "CFO", "임원"}
PERIOD_RE = re.compile(r"(20\d{2})\s*(?:년|\.)\s*(?:(\d{1,2})\s*(?:월|\.))?(?:\s*(?:부터|~|-|–|에서)\s*(20\d{2})\s*(?:년|\.)\s*(?:(\d{1,2})\s*(?:월|\.)?)?)?")


def incident_sentences(text: str) -> list[str]:
    """부정 행위어 + 사건 표지어가 같은 문장(또는 표 행)에 있는 문장"""
    # 서식 제목(예: "횡령ㆍ배임혐의발생")은 사건 서술이 아니므로 짧은 문장은 제외
    return [s for s in split_sentences(text) if len(squash(s)) >= 20 and has_any(s, FRAUD_ACT_TERMS) and has_any(s, INCIDENT_MARKERS)]


def classify_kind(cand: dict) -> tuple[str, str, list[str]]:
    """(case_kind, basis, 근거 문장들)"""
    ctx = cand.get("context") or cand.get("evidence_text") or ""
    inc = incident_sentences(ctx)
    if inc:
        return "fraud_incident", "부정 행위 서술과 사건 표지어(발생·혐의·확인·고소 등)가 같은 문장에 있음", inc
    defi = [s for s in sentences_with(ctx, DEFICIENCY_TERMS) if has_any(s, CASH_TERMS)]
    if defi:
        return "control_deficiency_disclosure", "자금 관련 통제 미비·취약점 서술(실제 부정 사건 서술 없음)", defi
    return "keyword_hit", "키워드 조합만 확인 — 사건·미비 서술 없음(정기 공시 문구·일반 통제 설명 가능성)", []


def _first(sentences: list[str], terms: list[str]) -> str | None:
    return next((s for s in sentences if has_any(s, terms)), None)


def _label_hits(text: str, lexicon: list[tuple[str, list[str]]]) -> list[str]:
    return [label for label, terms in lexicon if has_any(text, terms)]


def extract_rule(cand: dict, industry: str = "") -> dict:
    """규칙 기반 구조화. 반환 dict 은 fraud_cases 컬럼 + field_evidence."""
    ctx = cand.get("context") or cand.get("evidence_text") or ""
    kind, basis, key_sents = classify_kind(cand)
    sents = split_sentences(ctx)
    fe: dict[str, str] = {}  # field → 근거 문장
    focus = key_sents or sents

    fraud_types = [label for label, terms in FRAUD_TYPES if any(has_any(s, terms) for s in focus)] if kind == "fraud_incident" else []
    if fraud_types:
        fe["fraud_type"] = _first(focus, [t for label, ts in FRAUD_TYPES if label in fraud_types for t in ts]) or ""

    scheme = key_sents[0] if key_sents else None
    if scheme:
        fe["fraud_scheme"] = scheme

    actor_position, actor = ND, ND
    for s in key_sents:
        roles = find_roles(s)
        if roles:
            role, st, en = roles[0]
            prefix = "전 " if re.search(r"(전|前)\s*$", s[max(0, st - 3):st]) else ""
            actor_position = prefix + s[st:en]
            actor = "경영진" if role in MANAGEMENT_ROLES else "임직원"
            fe["actor_position"] = fe["actor"] = s
            break

    period = ND
    period_sents = key_sents + [x for x in sents if has_any(x, ["발생기간", "발생 기간", "기간", "부터", "에 걸쳐"])] if kind == "fraud_incident" else []
    for s in period_sents:
        m = PERIOD_RE.search(s)
        if m:
            period = m.group(0).strip()
            fe["period_of_fraud"] = s
            break

    amount = None
    amount_sents = [s for s in (key_sents + [x for x in sents if has_any(x, ["금액", "횡령", "배임", "유용"])]) if parse_amounts(s)]
    if amount_sents and kind == "fraud_incident":
        best = max(((v, s) for s in amount_sents for v, _ in parse_amounts(s)), key=lambda t: t[0])
        amount, fe["amount"] = best[0], best[1]

    acct_src = " ".join(focus)
    accounts = _label_hits(acct_src, ACCOUNT_LEXICON)
    if accounts:
        fe["affected_accounts"] = _first(focus, [t for _, ts in ACCOUNT_LEXICON for t in ts]) or ""
    mech = _label_hits(acct_src, CASH_MECHANISMS)
    if mech:
        fe["cash_mechanism"] = _first(focus, [t for _, ts in CASH_MECHANISMS for t in ts]) or ""
    tx = _label_hits(acct_src, TRANSACTION_TYPES)
    if tx:
        fe["transaction_type"] = _first(focus, [t for _, ts in TRANSACTION_TYPES for t in ts]) or ""

    rp_sent = _first(focus, RELATED_PARTY_TERMS)
    related = True if rp_sent else None  # 언급이 없으면 '미공시'(None) — False 로 단정하지 않음
    if rp_sent:
        fe["related_party_involved"] = rp_sent

    weakness = [s for s in sents if has_any(s, WEAKNESS_TERMS) and has_any(s, ["통제", "승인", "관리", "분리", "OTP", "인감", "권한", "보관"])][:3]
    if weakness:
        fe["control_weakness"] = weakness[0]
    circ = [label for label, groups in CIRCUMVENTION if any(all(has_any(s, g) for g in groups) for s in sents)]
    if circ:
        fe["control_circumvention_method"] = next(s for s in sents if any(all(has_any(s, g) for g in groups) for label, groups in CIRCUMVENTION if label in circ))
    detection = next((s for s in sents if has_any(s, DETECTION_SOURCES) and has_any(s, DETECTION_VERBS)), None)
    if detection:
        fe["detection_method"] = detection
    remediation = [s for s in sents if has_any(s, REMEDIATION_TERMS) and s not in key_sents[:1]][:3]
    if remediation:
        fe["remediation"] = remediation[0]

    evidence_sents = list(dict.fromkeys([*key_sents[:3], *[v for v in fe.values() if v]]))
    loc = f"{cand.get('document_name') or ''} ({cand.get('document_file') or ''})".strip()
    source_evidence = [{"text": s, "section": cand.get("section") or "", "page_or_location": loc} for s in evidence_sents if s]

    n_fields = sum(1 for k in ("amount", "actor_position", "period_of_fraud", "cash_mechanism", "affected_accounts") if k in fe)
    confidence = {"fraud_incident": min(0.7, 0.45 + 0.05 * n_fields), "control_deficiency_disclosure": 0.4, "keyword_hit": 0.2}[kind]

    return {
        "case_kind": kind,
        "is_actual_case": 1 if kind == "fraud_incident" else 0,
        "case_basis": basis,
        "industry": industry,
        "fraud_type": ", ".join(fraud_types) if fraud_types else ND,
        "fraud_scheme": scheme or ND,
        "actor": actor,
        "actor_position": actor_position,
        "period_of_fraud": period,
        "amount": amount,
        "affected_accounts": accounts,
        "cash_mechanism": ", ".join(mech) if mech else ND,
        "transaction_type": ", ".join(tx) if tx else ND,
        "related_party_involved": related,
        "control_weakness": weakness,
        "control_circumvention_method": circ,
        "detection_method": detection or ND,
        "remediation": remediation,
        "audit_relevance": (
            f"외부 사례(감사증거 아님) — 감사대상회사의 {', '.join(accounts) or '자금'} 관련 fraud risk identification 시 "
            f"유사 메커니즘 존재 여부를 고려하는 input"
        ),
        "source_evidence": source_evidence,
        "field_evidence": fe,
        "confidence": round(confidence, 2),
        "extraction_method": "rule",
    }


# ─── LLM 보강 (선택) ───
_LLM_FIELDS = ["fraud_type", "fraud_scheme", "actor", "actor_position", "period_of_fraud", "amount", "affected_accounts",
               "cash_mechanism", "transaction_type", "related_party_involved", "control_weakness", "control_circumvention_method",
               "detection_method", "remediation"]


def _llm_schema() -> dict:
    from llm_client import obj, str_list

    fld = lambda value_schema: obj({"value": value_schema, "quote": {"type": "string", "description": "근거 문장을 문맥에서 글자 그대로 복사. 근거가 없으면 빈 문자열"}})  # noqa: E731
    nullable_num = {"anyOf": [{"type": "number"}, {"type": "null"}]}
    nullable_bool = {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
    return obj({
        "case_kind": {"type": "string", "enum": ["fraud_incident", "control_deficiency_disclosure", "keyword_hit"]},
        "case_kind_quote": {"type": "string"},
        "fraud_type": fld({"type": "string"}),
        "fraud_scheme": fld({"type": "string"}),
        "actor": fld({"type": "string"}),
        "actor_position": fld({"type": "string"}),
        "period_of_fraud": fld({"type": "string"}),
        "amount": fld(nullable_num),
        "affected_accounts": fld(str_list()),
        "cash_mechanism": fld({"type": "string"}),
        "transaction_type": fld({"type": "string"}),
        "related_party_involved": fld(nullable_bool),
        "control_weakness": fld(str_list()),
        "control_circumvention_method": fld(str_list()),
        "detection_method": fld({"type": "string"}),
        "remediation": fld(str_list()),
        "confidence": {"type": "number"},
    })


_LLM_ROLE = """[작업] 공시 문맥에서 외부 부정사례를 구조화합니다.
- case_kind: fraud_incident(실제 부정·혐의 서술) / control_deficiency_disclosure(사건 없이 통제 미비 공시) / keyword_hit(일반 통제 설명·정기 공시 문구).
- 모든 필드는 value 와 quote(문맥 문장을 글자 그대로 복사)를 함께 냅니다. quote 가 없으면 value 는 "Not disclosed"(숫자·불리언은 null)로 둡니다.
- 금액은 원 단위 숫자. 실명은 쓰지 않고 직위만 씁니다. actor 는 경영진/임직원/외부인/Not disclosed 중 하나.
- 문맥에 없는 금액·행위자·통제미비·메커니즘을 만들어내지 않습니다."""


def extract_llm(cand: dict, base: dict, llm) -> tuple[dict, list[str]]:
    """LLM 구조화 결과를 규칙 결과 위에 병합. 인용문이 문맥에 없으면 해당 필드는 버린다. (merged, notes)"""
    ctx = cand.get("context") or ""
    user = (
        f"회사: {cand.get('corp_name')} / 공시: {cand.get('report_nm')} / 접수일 {cand.get('rcept_dt')}\n"
        f"문서: {cand.get('document_name')} / 위치: {cand.get('section')}\n\n<context>\n{ctx}\n</context>"
    )
    data = llm.structured("fraud_case_extraction", _LLM_ROLE, user, _llm_schema())
    merged = dict(base)
    fe = dict(base["field_evidence"])
    notes = []
    for f in _LLM_FIELDS:
        item = data.get(f) or {}
        quote, value = item.get("quote") or "", item.get("value")
        if not quote:
            continue
        if not quote_in_source(quote, ctx):
            notes.append(f"{f}: 인용문이 원문에 없어 폐기")
            continue
        if value in (None, "", [], ND):
            continue
        merged[f] = ", ".join(value) if f in ("fraud_type",) and isinstance(value, list) else value
        fe[f] = quote
    kind_q = data.get("case_kind_quote") or ""
    if data.get("case_kind") and (data["case_kind"] == "keyword_hit" or quote_in_source(kind_q, ctx)):
        merged["case_kind"] = data["case_kind"]
        merged["is_actual_case"] = 1 if data["case_kind"] == "fraud_incident" else 0
        merged["case_basis"] = f"LLM 판정(근거: {kind_q[:120]})" if kind_q else "LLM 판정"
    merged["field_evidence"] = fe
    loc = base["source_evidence"][0]["page_or_location"] if base["source_evidence"] else ""
    ev = list(dict.fromkeys([e["text"] for e in base["source_evidence"]] + [v for v in fe.values() if v]))
    merged["source_evidence"] = [{"text": t, "section": cand.get("section") or "", "page_or_location": loc} for t in ev]
    merged["confidence"] = round(min(float(data.get("confidence") or 0.5), 0.9), 2)
    merged["extraction_method"] = "rule+llm"
    return merged, notes


def build_cases(conn, engagement_id: int, llm=None, only_latest: bool = True) -> dict:
    """source_candidates → fraud_cases. 검토 결과(reviewer 등)는 재실행해도 유지."""
    industries = {r["corp_code"]: r["industry_name"] for r in rows(conn, "SELECT corp_code, industry_name FROM peer_candidates WHERE engagement_id=?", [engagement_id])}
    cands = rows(conn, "SELECT * FROM source_candidates WHERE engagement_id=?" + (" AND is_latest=1" if only_latest else ""), [engagement_id])
    stats = {"candidates": len(cands), "fraud_incident": 0, "control_deficiency_disclosure": 0, "keyword_hit": 0, "llm_used": 0, "llm_errors": 0, "skipped_no_evidence": 0}
    notes_all: list[str] = []
    for c in cands:
        rec = extract_rule(c, industries.get(c.get("corp_code"), "") or "")
        if llm is not None and rec["case_kind"] != "keyword_hit":
            try:
                rec, notes = extract_llm(c, rec, llm)
                notes_all += notes
                stats["llm_used"] += 1
            except Exception as e:  # LLM 실패는 규칙 결과로 계속
                stats["llm_errors"] += 1
                notes_all.append(f"{c['candidate_id']}: LLM 실패 — {e}")
        if rec["case_kind"] != "keyword_hit" and not rec["source_evidence"]:
            stats["skipped_no_evidence"] += 1  # 근거 없는 사례는 저장하지 않음
            continue
        stats[rec["case_kind"]] += 1
        row = {
            "case_id": f"C-{c['candidate_id']}",
            "engagement_id": engagement_id,
            "candidate_id": c["candidate_id"],
            "company_name": c.get("corp_name"),
            "corp_code": c.get("corp_code"),
            "rcept_no": c.get("rcept_no"),
            "filing_date": c.get("rcept_dt"),
            "report_name": c.get("report_nm"),
            "source_url": c.get("source_url"),
            "ai_generated": 1,
            **{k: v for k, v in rec.items() if k not in ("field_evidence",)},
        }
        for k in ("affected_accounts", "control_weakness", "control_circumvention_method", "remediation", "source_evidence"):
            row[k] = j(row[k])
        row["related_party_involved"] = None if row["related_party_involved"] is None else int(bool(row["related_party_involved"]))
        row["field_evidence"] = j(rec["field_evidence"])
        with conn:
            upsert(conn, "fraud_cases", row, ["engagement_id", "case_id"], preserve=REVIEW_PRESERVE)
    stats["notes"] = notes_all[:50]
    return stats


def load_cases(conn, engagement_id: int, kinds: tuple[str, ...] = ("fraud_incident", "control_deficiency_disclosure")) -> list[dict]:
    q = f"SELECT * FROM fraud_cases WHERE engagement_id=? AND case_kind IN ({','.join('?' for _ in kinds)}) AND review_status != 'Rejected' ORDER BY filing_date DESC"
    out = rows(conn, q, [engagement_id, *kinds])
    for r in out:
        for k in ("affected_accounts", "control_weakness", "control_circumvention_method", "remediation", "source_evidence", "field_evidence"):
            r[k] = unj(r[k], [] if k != "field_evidence" else {})
    return out
