"""Engagement Profile (요구사항 3.1) 저장·조회."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from db import j, now, rows, unj


@dataclass
class EngagementProfile:
    company_name: str
    period_from: str  # YYYY-MM-DD
    period_to: str
    industry: str = ""
    subindustry: str = ""
    ksic_codes: list[str] = field(default_factory=list)
    market: str = ""  # KOSPI / KOSDAQ / KONEX / 비상장
    size_basis: str = ""  # revenue / assets / market_cap
    size_value: float | None = None
    has_overseas_subsidiary: bool | None = None
    business_description: str = ""
    treasury_features: str = ""


def save_engagement(conn, p: EngagementProfile, engagement_id: int | None = None) -> int:
    data = asdict(p)
    data["ksic_codes"] = j(p.ksic_codes)
    data["has_overseas_subsidiary"] = None if p.has_overseas_subsidiary is None else int(p.has_overseas_subsidiary)
    data["updated_at"] = now()
    with conn:
        if engagement_id:
            sets = ", ".join(f"{k}=?" for k in data)
            conn.execute(f"UPDATE engagements SET {sets} WHERE id=?", [*data.values(), engagement_id])
            return engagement_id
        data["created_at"] = data["updated_at"]
        cur = conn.execute(f"INSERT INTO engagements ({', '.join(data)}) VALUES ({', '.join('?' for _ in data)})", list(data.values()))
        return int(cur.lastrowid)


def load_engagement(conn, engagement_id: int) -> EngagementProfile | None:
    r = rows(conn, "SELECT * FROM engagements WHERE id=?", [engagement_id])
    if not r:
        return None
    d = r[0]
    return EngagementProfile(
        company_name=d["company_name"],
        period_from=d["period_from"],
        period_to=d["period_to"],
        industry=d["industry"] or "",
        subindustry=d["subindustry"] or "",
        ksic_codes=unj(d["ksic_codes"], []),
        market=d["market"] or "",
        size_basis=d["size_basis"] or "",
        size_value=d["size_value"],
        has_overseas_subsidiary=None if d["has_overseas_subsidiary"] is None else bool(d["has_overseas_subsidiary"]),
        business_description=d["business_description"] or "",
        treasury_features=d["treasury_features"] or "",
    )


def list_engagements(conn) -> list[dict]:
    return rows(conn, "SELECT id, company_name, period_from, period_to, updated_at FROM engagements ORDER BY id DESC")
