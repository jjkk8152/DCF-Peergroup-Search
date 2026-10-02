import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import connect  # noqa: E402


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def rcm(*rows):
    """(risk, control[, owner]) 튜플 → rcm_rows 형식"""
    out = []
    for i, r in enumerate(rows):
        risk, control, owner = (list(r) + [""])[:3]
        out.append({"row_index": i, "risk_id": f"R{i + 1}", "risk_description": risk, "control_id": f"C{i + 1}", "control_description": control,
                    "control_owner": owner, "evidence": "", "process": "", "sub_process": "", "preventive_detective": ""})
    return out
