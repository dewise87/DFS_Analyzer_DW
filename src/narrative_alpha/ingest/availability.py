"""Explicit salary-feed inactivity, shared by decisions and outcome accounting."""

INACTIVE_SALARY_STATUSES = frozenset({"O", "OUT", "INACTIVE", "IR", "PUP", "SUSPENDED"})


def inactive_salary_status(value: str | None) -> bool:
    """Unknown, questionable, and doubtful statuses are not confirmed inactivity."""

    return value is not None and value.strip().upper().replace(" ", "_") in INACTIVE_SALARY_STATUSES
