"""Streamlit 화면 스모크 테스트 — 샘플 DB로 모든 화면이 예외 없이 렌더되는지."""
import os
from pathlib import Path

import pytest

import run_sample

APP = str(Path(__file__).resolve().parents[1] / "app.py")
PAGES = ["Dashboard", "1. Engagement Profile", "2. Peer Selection", "3. Fraud Cases", "4. Fraud Scenarios",
         "5. RCM Upload", "6. Additional Evidence", "7. Coverage & Audit Response", "8. Review & Export"]


@pytest.fixture(scope="module")
def sample_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("app") / "app.db"
    run_sample.run(str(db), None, quiet=True)["conn"].close()
    return db


@pytest.mark.parametrize("page", PAGES)
def test_pages_render(sample_db, page, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("PHASE2_DB", str(sample_db))
    import importlib

    import db

    importlib.reload(db)  # DEFAULT_DB_PATH 재평가
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.sidebar.selectbox[0].select_index(1)  # 샘플 engagement
    at.sidebar.radio[0].set_value(page)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    if page == "Dashboard":
        md = " ".join(m.value for m in at.markdown)
        assert "8 Fraud Scenarios identified" in md
        assert "Not Covered" in " ".join(e.label for e in at.expander)
