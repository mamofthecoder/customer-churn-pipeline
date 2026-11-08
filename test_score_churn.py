from datetime import date

import pandas as pd
import pytest

from score_churn import validate_batch


def sample_batch():
    return pd.DataFrame({
        "customer_id": [42],
        "scoring_month": [date(2026, 12, 1)],
        "Tenure_Months": [4],
        "Monthly_Fee": [None],
        "Support_Tickets": [1.0],
        "Usage_Hours": [None],
        "Contract_Type": [None],
    })

def test_duplicate_customer_month_fails():
    batch = sample_batch()
    duplicated = pd.concat([batch, batch], ignore_index=True)

    with pytest.raises(ValueError, match="one row per customer"):
        validate_batch(duplicated)

def test_allowed_missing_values_pass():
    validated = validate_batch(sample_batch())
    assert len(validated) == 1
    assert validated["Monthly_Fee"].isna().all()


@pytest.mark.parametrize(
    "column,value,message",
    [
        ("Usage_Hours", -5.0, "Invalid Usage_Hours"),
        ("Support_Tickets", 2.5, "must contain whole numbers"),
        ("Tenure_Months", None, "Missing required Tenure_Months"),
        ("Contract_Type", "Montly", "Invalid Contract_Type"),
    ],
)
def test_invalid_inputs_fail(column, value, message):
    batch = sample_batch()
    batch[column] = value

    with pytest.raises(ValueError, match=message):
        validate_batch(batch)
