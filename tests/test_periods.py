"""ISO 周的换算，重点是跨年和第 53 周。"""

from datetime import date

import pytest

from da_agent.periods import base_week, previous_week, same_week_last_year, week_monday, week_of


def test_week_monday_and_back() -> None:
    assert week_monday("2011-W48") == date(2011, 11, 28)
    assert week_of(date(2011, 12, 4)) == "2011-W48"  # 周日仍属于同一周


def test_previous_week_crosses_year() -> None:
    assert previous_week("2011-W01") == "2010-W52"
    assert previous_week("2010-W01") == "2009-W53"  # 2009 年有第 53 周


def test_same_week_last_year() -> None:
    assert same_week_last_year("2011-W48") == "2010-W48"
    with pytest.raises(ValueError, match="2008 年没有第 53 周"):
        same_week_last_year("2009-W53")


@pytest.mark.parametrize("bad", ["2011-48", "W48-2011", "2011-W8", "2010-W53"])
def test_invalid_weeks_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        week_monday(bad)


def test_base_week_by_compare() -> None:
    assert base_week("2011-W48", "wow") == "2011-W47"
    assert base_week("2011-W48", "yoy") == "2010-W48"
    with pytest.raises(ValueError, match="wow"):
        base_week("2011-W48", "mom")
