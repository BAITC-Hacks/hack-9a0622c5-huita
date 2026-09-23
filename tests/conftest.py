"""Generated test inputs; organizer CSVs are never needed by the test suite."""

import pandas as pd
import pytest


@pytest.fixture
def synthetic_frames():
    """A small complete dataset with known positive tariff transitions.

    Keep all three ARPU classes and every optional category represented so the
    real worker exercises segmentation, pilots, planning, and scoring. These
    values are fabricated here and are unrelated to the organizer dataset.
    """
    profile_rows = []
    history_rows = []
    for arpu_segment, arpu in (("LOW", 700.0), ("MID", 3000.0), ("HIGH", 7000.0)):
        for index in range(90):
            profile_rows.append({
                "ID_NUMBER": len(profile_rows) + 1,
                "current_tariff": "tariff_a",
                "arpu_segment": arpu_segment,
                "data_segment": ("NON_USER", "LITE", "HEAVY")[index % 3],
                "call_segment": ("LOW", "MEDIUM", "HIGH")[(index // 3) % 3],
                "predicted_arpu": arpu + index % 10,
            })
        for target, multiplier in (("tariff_b", 2.0), ("tariff_c", 1.25)):
            for index in range(12):
                previous = arpu + index
                history_rows.append({
                    "ID_NUMBER": len(history_rows) + 1,
                    "tariff_plan_code_from": "tariff_a",
                    "tariff_plan_code_to": target,
                    "AVG_ARPU_PREV_3M": previous,
                    "AVG_ARPU_NEXT_3M": previous * multiplier,
                })
    return {
        "profile": pd.DataFrame(profile_rows),
        "history": pd.DataFrame(history_rows),
        "tariffs": pd.DataFrame({
            "tariff_plan_code": ["tariff_a", "tariff_b", "tariff_c"],
            "price_tariff": [800.0, 2500.0, 5000.0],
        }),
    }
