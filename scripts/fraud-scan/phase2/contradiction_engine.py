"""Contradictory Evidence Module (요구사항 17).

같은 사실(예: OTP 보관자)에 대해 출처가 다른 진술이 서로 다르면 'Potential Contradiction' 으로 기록한다.
어느 진술이 사실인지 판단하지 않는다 — Auditor Follow-up Required.

현재 지원 주제: 인증수단·인감·카드·통장 등의 보관/관리 주체(custody). 진술 출처: RCM 행, 추가 증거(인터뷰·문서 등).
확장: TOPICS 에 주제를 추가하거나, extract_statements 에 다른 사실 유형 추출기를 추가.
"""
from __future__ import annotations

import re

from db import REVIEW_PRESERVE, now, rows
from rcm_parser import control_label
from text_utils import find_roles, find_terms, split_sentences

TOPICS: dict[str, list[str]] = {
    "인터넷뱅킹 OTP": ["OTP", "보안카드", "OTP카드"],
    "공동인증서(인증서)": ["공동인증서", "공인인증서", "인증서"],
    "법인인감": ["법인인감", "법인 인감", "인감"],
    "법인카드": ["법인카드"],
    "통장": ["통장"],
    "인터넷뱅킹 승인 권한": ["승인 권한", "승인권한", "Approver 권한"],
}
CUSTODY_VERBS = ["보관", "관리", "소지", "보유", "가지고", "사용"]
SUBJECT_PARTICLE = re.compile(r"^\s*(이|가|는|은|께서|에서)")


def _topic_of(sentence: str) -> list[str]:
    return [t for t, terms in TOPICS.items() if find_terms(sentence, terms)]


def _holder(sentence: str, topic_terms: list[str]) -> str | None:
    """보관·관리 주체(역할) 추출. 'CFO OTP' 처럼 인증수단 바로 앞 역할은 '소유자'로 보고, 주격 조사가 붙은 역할을 우선."""
    roles = find_roles(sentence)
    if not roles or not find_terms(sentence, CUSTODY_VERBS):
        return None
    topic_pos = [m.start() for t in topic_terms for m in re.finditer(re.escape(t), sentence, re.IGNORECASE)]
    candidates = []
    for role, st, en in roles:
        after = sentence[en : en + 4]
        owner_of_topic = any(0 <= p - en <= 2 for p in topic_pos) or bool(re.match(r"^\s*의\s*", after))
        subject = bool(SUBJECT_PARTICLE.match(after))
        candidates.append((subject and not owner_of_topic, not owner_of_topic, -st, role))
    candidates.sort(reverse=True)
    return candidates[0][3] if candidates else None


def extract_statements(sources: list[tuple[str, str]]) -> list[dict]:
    """sources: [(출처 라벨, 텍스트)] → [{topic, holder, source, statement}]"""
    out = []
    for label, text in sources:
        for s in split_sentences(text):
            for topic in _topic_of(s):
                h = _holder(s, TOPICS[topic])
                if h:
                    out.append({"topic": topic, "holder": h, "source": label, "statement": s.strip()})
    return out


def detect(statements: list[dict]) -> list[dict]:
    """같은 주제에 대해 서로 다른 출처가 다른 보관자를 진술하면 상충 후보"""
    found, seen = [], set()
    for i, a in enumerate(statements):
        for b in statements[i + 1 :]:
            if a["topic"] != b["topic"] or a["source"] == b["source"] or a["holder"] == b["holder"]:
                continue
            key = (a["topic"], frozenset([(a["source"], a["holder"]), (b["source"], b["holder"])]))
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "topic": f"{a['topic']} 보관·관리 주체",
                "source_1": a["source"], "statement_1": a["statement"],
                "source_2": b["source"], "statement_2": b["statement"],
                "status": "Potential Contradiction",
                "auditor_follow_up": (
                    f"Contradictory Information – Auditor Follow-up Required: '{a['topic']}'의 보관·관리 주체가 "
                    f"{a['source']}에는 '{a['holder']}', {b['source']}에는 '{b['holder']}'로 기술됨. "
                    "어느 진술이 사실인지 판단하지 않음 — 실물 보관 실태 walkthrough, 인터넷뱅킹 사용자·권한 목록, 사용 로그로 확인 고려."
                ),
            })
    return found


def run(conn, engagement_id: int, upload_id: int | None) -> list[dict]:
    sources: list[tuple[str, str]] = []
    if upload_id:
        for r in rows(conn, "SELECT * FROM rcm_rows WHERE upload_id=? ORDER BY row_index", [upload_id]):
            text = " ".join(filter(None, [r["control_description"], f"{r['control_owner']}가 수행" if r["control_owner"] else ""]))
            sources.append((f"RCM {control_label(r)}", text))
    for e in rows(conn, "SELECT * FROM additional_evidence WHERE engagement_id=? ORDER BY id", [engagement_id]):
        sources.append((f"{e['source_type'] or '추가자료'}: {e['source_name'] or e['id']}", e["statement"]))
    found = detect(extract_statements(sources))
    with conn:
        conn.execute("DELETE FROM contradictions WHERE engagement_id=? AND review_status='Not Reviewed'", [engagement_id])
        for c in found:
            exists = rows(conn, "SELECT id FROM contradictions WHERE engagement_id=? AND statement_1=? AND statement_2=?", [engagement_id, c["statement_1"], c["statement_2"]])
            if exists:
                continue  # 이미 검토된 동일 건 유지
            conn.execute(
                "INSERT INTO contradictions (engagement_id, topic, source_1, statement_1, source_2, statement_2, status, auditor_follow_up, ai_generated) VALUES (?,?,?,?,?,?,?,?,1)",
                (engagement_id, c["topic"], c["source_1"], c["statement_1"], c["source_2"], c["statement_2"], c["status"], c["auditor_follow_up"]),
            )
    return found


def add_evidence(conn, engagement_id: int, source_type: str, source_name: str, statement: str, recorded_by: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO additional_evidence (engagement_id, source_type, source_name, statement, recorded_by, recorded_at) VALUES (?,?,?,?,?,?)",
            (engagement_id, source_type, source_name, statement, recorded_by, now()),
        )


_ = REVIEW_PRESERVE  # (검토 결과는 DELETE 조건으로 보존)
