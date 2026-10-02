"""Peer / Benchmark 후보 생성 (요구사항 4) — 결정론, LLM 없음.

데이터: 저장소의 기존 캐시를 그대로 읽는다 (Phase 1·MCP 서버와 같은 원천).
  - data/peer-snapshot/{분기말}.json.gz : KSIC 업종코드, 시장, 사업의 개요(+요약), 부문매출, 제외 플래그
  - data/valuation-cache/{분기말}.json   : 시가총액, KSIC 업종명 (종목코드 키)
AI(코드)는 후보와 점수·이유만 제시하고, 최종 peer 는 감사인이 체크박스로 확정한다(selected 컬럼).
"""
from __future__ import annotations

import gzip
import json
import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from db import REPO_ROOT, REVIEW_PRESERVE, j, log_action, now, rows, upsert
from engagement import EngagementProfile

SNAPSHOT_DIR = REPO_ROOT / "data" / "peer-snapshot"
VALUATION_DIR = REPO_ROOT / "data" / "valuation-cache"

WEIGHTS = {"industry": 0.40, "business": 0.30, "size": 0.15, "overseas": 0.10, "market": 0.05}

OVERSEAS_RE = re.compile(
    r"(해외|현지)\s*(법인|자회사|종속회사|생산법인|판매법인)|"
    r"(중국|베트남|미국|인도|멕시코|폴란드|헝가리|체코|인도네시아|태국|말레이시아|일본|독일|브라질|튀르키예|터키|슬로바키아|싱가포르|필리핀|캐나다|영국|홍콩|대만)\s*(현지\s*)?(법인|공장|자회사|종속회사)"
)
STOPWORDS = set("당사 회사 연결회사 사업 부문 제품 서비스 주요 등을 있습니다 하고 하는 있는 있으며 및 의 를 을 이 가 에 으로 로 한 등 기타 통해 관련 영위 위해 대한 바탕 기준 당기 전기 매출 영업 보유 생산".split())


def available_snapshots() -> list[str]:
    return sorted(p.name.split(".")[0] for p in SNAPSHOT_DIR.glob("*.json.gz"))


def resolve_snapshot(as_of: str) -> str:
    """as_of(YYYY-MM-DD 또는 YYYYMMDD) 이하 최근 분기말 스냅샷, 없으면 가장 이른 것"""
    d = as_of.replace("-", "")
    snaps = available_snapshots()
    if not snaps:
        raise FileNotFoundError(f"peer 스냅샷이 없습니다: {SNAPSHOT_DIR}")
    le = [s for s in snaps if s <= d]
    return le[-1] if le else snaps[0]


@lru_cache(maxsize=4)
def load_snapshot(date: str) -> dict:
    with gzip.open(SNAPSHOT_DIR / f"{date}.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=4)
def load_valuation(date: str) -> dict:
    p = VALUATION_DIR / f"{date}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


@lru_cache(maxsize=1)
def ksic_names() -> dict[str, str]:
    """valuation-cache 에 들어있는 KSIC 코드→업종명"""
    names: dict[str, str] = {}
    for p in sorted(VALUATION_DIR.glob("*.json")):
        try:
            for v in json.loads(p.read_text(encoding="utf-8")).values():
                ind = v.get("industry") or {}
                if ind.get("code") and ind.get("name"):
                    names[ind["code"]] = ind["name"]
        except (ValueError, AttributeError):
            continue
    return names


def search_ksic(query: str, limit: int = 30) -> list[tuple[str, str]]:
    q = re.sub(r"\s+", "", query)
    out = [(c, n) for c, n in ksic_names().items() if q and (q in re.sub(r"\s+", "", n) or c.startswith(q))]
    return sorted(out)[:limit]


def _bigrams(text: str) -> Counter:
    t = re.sub(r"[^가-힣A-Za-z0-9]", "", text or "")
    return Counter(t[i : i + 2] for i in range(len(t) - 1))


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


_JOSA_RE = re.compile(r"(으로|에서|에게|하여|하고|하는|하며|되는|에|을|를|이|가|은|는|의|로|와|과|도)$")


def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[가-힣A-Za-z]{2,}", text or ""):
        w = _JOSA_RE.sub("", w) if len(w) > 2 else w
        if len(w) >= 2 and w not in STOPWORDS:
            out.add(w)
    return out


def _lcp(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _business_text(c: dict) -> str:
    return " ".join(filter(None, [c.get("overviewSummary"), c.get("segmentsSummary"), (c.get("overview") or "")[:1500]]))


def generate_candidates(profile: EngagementProfile, limit: int = 30, exclude_flags: bool = True) -> tuple[str, list[dict]]:
    """(snapshot_date, 후보 리스트[점수 내림차순])"""
    snap_date = resolve_snapshot(profile.period_to)
    snap = load_snapshot(snap_date)["companies"]
    val = load_valuation(snap_date)
    names = ksic_names()
    query_text = " ".join([profile.industry, profile.subindustry, profile.business_description])
    q_vec = _bigrams(query_text)
    q_words = _words(query_text)
    targets = [c for c in profile.ksic_codes if c]
    target_name = re.sub(r"\s|\(주\)|㈜|주식회사", "", profile.company_name)

    pool = []
    for code, c in snap.items():
        if re.sub(r"\s|\(주\)|㈜|주식회사", "", c.get("name", "")) == target_name:
            continue  # 감사대상회사 자신 제외
        flags = c.get("flags") or {}
        if exclude_flags and (flags.get("isSpac") or flags.get("isReit") or flags.get("isHolding")):
            continue
        ind_code = c.get("industryCode") or ""
        depth = max((_lcp(ind_code, t) for t in targets), default=0)
        ind_score = max(((_lcp(ind_code, t) / len(t)) if _lcp(ind_code, t) >= 2 else 0.0) for t in targets) if targets else 0.0
        btext = _business_text(c)
        sim = _cosine(q_vec, _bigrams(btext)) if q_vec else 0.0
        pool.append((code, c, ind_code, depth, ind_score, sim, btext))

    if not pool:
        return snap_date, []
    # 후보군: 업종 대분류(2자리) 이상 일치 ∪ 사업설명 유사도 상위 20
    by_sim = sorted(pool, key=lambda p: -p[5])[:20]
    keep = {p[0] for p in pool if p[3] >= 2} | {p[0] for p in by_sim if p[5] > 0}
    pool = [p for p in pool if p[0] in keep]
    max_sim = max((p[5] for p in pool), default=0) or 1.0

    out = []
    for code, c, ind_code, depth, ind_score, sim, btext in pool:
        comps: dict[str, float | None] = {"industry": ind_score if targets else None, "business": (sim / max_sim) if q_vec else None}
        reasons: list[str] = []
        diffs: list[str] = []
        ind_name = names.get(ind_code, "")
        if targets:
            if ind_score >= 1:
                reasons.append(f"KSIC {ind_code}({ind_name}) — 입력 업종코드와 일치")
            elif depth >= 2:
                reasons.append(f"KSIC 상위 {depth}자리 일치: {ind_code}({ind_name})")
            else:
                diffs.append(f"업종코드 상이: {ind_code}({ind_name})")
        if q_vec:
            if sim / max_sim >= 0.6:
                reasons.append(f"사업 설명 텍스트 유사도 상위 ({sim:.2f})")
            shared = sorted(q_words & _words(btext))[:6]
            if shared:
                reasons.append("공통 키워드: " + ", ".join(shared))
        # 시장
        mk = c.get("market") or ""
        if profile.market and profile.market in ("KOSPI", "KOSDAQ", "KONEX"):
            comps["market"] = 1.0 if mk == profile.market else 0.0
            (reasons if mk == profile.market else diffs).append(f"상장시장 {mk or '미상'}")
        else:
            comps["market"] = None
        # 규모: 피어 측 데이터는 시가총액만 보유 → 같은 기준일 때만 비교
        mcap = ((val.get(code) or {}).get("marketCap") or {}).get("total")
        if profile.size_basis == "market_cap" and profile.size_value and mcap:
            ratio = mcap / profile.size_value
            comps["size"] = max(0.0, 1 - abs(math.log10(ratio)))
            (reasons if comps["size"] >= 0.5 else diffs).append(f"시가총액 {mcap / 1e8:,.0f}억원 (대상 대비 {ratio:.1f}배)")
        else:
            comps["size"] = None
        # 해외법인 (사업의 개요 문구 기준)
        overview = c.get("overview") or ""
        peer_overseas = bool(OVERSEAS_RE.search(overview)) if overview else None
        if profile.has_overseas_subsidiary is not None and peer_overseas is not None:
            comps["overseas"] = 1.0 if peer_overseas == profile.has_overseas_subsidiary else 0.0
        else:
            comps["overseas"] = None
        if peer_overseas:
            reasons.append("해외 법인·공장 언급(사업의 개요)")

        avail = {k: v for k, v in comps.items() if v is not None}
        wsum = sum(WEIGHTS[k] for k in avail) or 1.0
        score = sum(WEIGHTS[k] * v for k, v in avail.items()) / wsum
        if (c.get("flags") or {}).get("isAdministrative"):
            diffs.append("관리종목 지정 이력(스냅샷 플래그)")
        if not overview:
            diffs.append("사업의 개요 미수집 — 텍스트 유사도 판단 제한")
        out.append(
            {
                "corp_code": c.get("corpCode"),
                "stock_code": code,
                "company_name": c.get("name"),
                "industry_code": ind_code,
                "industry_name": ind_name,
                "market": mk,
                "similarity_score": round(score, 3),
                "score_breakdown": {k: (None if v is None else round(v, 3)) for k, v in comps.items()},
                "reasons": reasons,
                "differences": diffs,
                "market_cap": mcap,
                "has_overseas": peer_overseas,
            }
        )
    out.sort(key=lambda r: -r["similarity_score"])
    notes = []
    if profile.size_basis in ("revenue", "assets") and profile.size_value:
        notes.append("규모 비교 제외: peer 측 매출·자산 데이터가 캐시에 없어 시가총액 기준 입력 시에만 비교")
    for r in out:
        r["notes"] = notes
    return snap_date, out[:limit]


def save_candidates(conn, engagement_id: int, snap_date: str, candidates: list[dict]) -> None:
    """후보 저장. 기존 선택(selected)·검토 결과는 유지한다."""
    with conn:
        for r in candidates:
            upsert(
                conn,
                "peer_candidates",
                {
                    "engagement_id": engagement_id,
                    "corp_code": r["corp_code"],
                    "stock_code": r["stock_code"],
                    "company_name": r["company_name"],
                    "industry_code": r["industry_code"],
                    "industry_name": r["industry_name"],
                    "market": r["market"],
                    "similarity_score": r["similarity_score"],
                    "score_breakdown": j(r["score_breakdown"]),
                    "reasons": j({"similar": r["reasons"], "different": r["differences"], "notes": r.get("notes", [])}),
                    "snapshot_date": snap_date,
                    "ai_generated": 1,
                },
                ["engagement_id", "corp_code"],
                preserve=("selected", "selected_by", "selected_at", *REVIEW_PRESERVE),
            )


def set_selection(conn, engagement_id: int, selected_corp_codes: set[str], reviewer: str) -> None:
    """감사인의 최종 peer 확정 (체크박스). 선택 이력은 review_log 에 남긴다."""
    with conn:
        for r in rows(conn, "SELECT corp_code, selected FROM peer_candidates WHERE engagement_id=?", [engagement_id]):
            want = 1 if r["corp_code"] in selected_corp_codes else 0
            if want != r["selected"]:
                conn.execute(
                    "UPDATE peer_candidates SET selected=?, selected_by=?, selected_at=? WHERE engagement_id=? AND corp_code=?",
                    (want, reviewer, now(), engagement_id, r["corp_code"]),
                )
    log_action(conn, engagement_id, "peer_candidates", sorted(selected_corp_codes), "peer_selection", reviewer)


def selected_peers(conn, engagement_id: int) -> list[dict]:
    return rows(conn, "SELECT * FROM peer_candidates WHERE engagement_id=? AND selected=1 ORDER BY similarity_score DESC", [engagement_id])


def add_manual_peer(conn, engagement_id: int, corp_code: str, stock_code: str, company_name: str, reviewer: str) -> None:
    """감사인이 후보 목록 밖의 회사를 직접 추가 (AI 생성 아님)"""
    with conn:
        upsert(
            conn,
            "peer_candidates",
            {
                "engagement_id": engagement_id, "corp_code": corp_code, "stock_code": stock_code, "company_name": company_name,
                "reasons": j({"similar": ["감사인 직접 추가"], "different": [], "notes": []}),
                "selected": 1, "selected_by": reviewer, "selected_at": now(), "ai_generated": 0,
            },
            ["engagement_id", "corp_code"],
        )
    log_action(conn, engagement_id, "peer_candidates", corp_code, "manual_add", reviewer)
