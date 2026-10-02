"""Fraud Scenario Library 로더 (scenario_library.json → DB scenario_library 테이블 동기화)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from db import j, now, rows, upsert

LIBRARY_PATH = Path(__file__).with_name("scenario_library.json")


@lru_cache(maxsize=1)
def load_library() -> dict:
    return json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))


def scenarios() -> list[dict]:
    return load_library()["scenarios"]


def scenario(scenario_id: str) -> dict | None:
    return next((s for s in scenarios() if s["scenario_id"] == scenario_id), None)


def sync_to_db(conn) -> None:
    """고정 taxonomy 를 DB에 반영 (status = Fixed Taxonomy). 신규 시나리오 후보는 scenario_candidates 테이블에 별도 보관."""
    with conn:
        for s in scenarios():
            upsert(
                conn,
                "scenario_library",
                {
                    "scenario_id": s["scenario_id"],
                    "title": s["title"],
                    "title_ko": s.get("title_ko"),
                    "description": s["description"],
                    "mechanism": s["mechanism"],
                    "definition": j(s),
                    "status": "Fixed Taxonomy",
                    "created_at": now(),
                },
                ["scenario_id"],
                preserve=["created_at"],
            )


def db_scenarios(conn) -> list[dict]:
    return rows(conn, "SELECT * FROM scenario_library ORDER BY scenario_id")
