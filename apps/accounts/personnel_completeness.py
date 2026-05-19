"""Profile completeness score (0–100) for personnel records."""

from __future__ import annotations

from typing import Any


def _filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    return True


def compute_profile_completeness(user) -> int:
    """
    Return an integer 0–100 based on how many checklist fields are populated.

    Weights are equal per field; the checklist focuses on core identity and contact.
    """
    checks = [
        user.email,
        user.first_name,
        user.last_name,
        user.phone,
        user.gender,
        user.date_of_birth,
        user.staff_id,
        user.date_of_employment,
        user.job_role,
        user.state_of_origin,
        user.lga,
        user.residential_address,
        user.next_of_kin_first_name,
        user.next_of_kin_last_name,
        user.next_of_kin_phone,
        user.qualification_school,
        user.qualification_degree,
    ]
    total = len(checks)
    if total == 0:
        return 0
    done = sum(1 for v in checks if _filled(v))
    return int(round(100 * done / total))
