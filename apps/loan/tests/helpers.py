"""Shared helpers for loan API tests."""

from django.contrib.auth import get_user_model

from apps.accounts.models import ConfirmationStatus, Department, Role, RoleName, UserRole
from apps.loan.models import LoanSettings, LoanType

User = get_user_model()


def ensure_role(name: str) -> Role:
    role, _ = Role.objects.get_or_create(name=name, defaults={"description": name})
    return role


def make_user(email: str, *, password="testpass123", roles=None, confirmed=True, **extra):
    user = User.objects.create_user(
        email=email,
        password=password,
        confirmation_status=(
            ConfirmationStatus.CONFIRMED if confirmed else ConfirmationStatus.PENDING
        ),
        **extra,
    )
    for role_name in roles or []:
        UserRole.objects.get_or_create(user=user, role=ensure_role(role_name))
    return user


def make_loan_type(name="Loan Test Type"):
    return LoanType.objects.get_or_create(
        name=name,
        defaults={"description": "For loan tests"},
    )[0]


def setup_department_with_line_manager(*, dept_name="Loan Test Department"):
    ensure_role(RoleName.LINE_MANAGER)
    department = Department.objects.create(name=dept_name)
    line_manager = make_user(
        f"lm-{dept_name.replace(' ', '-').lower()}@test.com",
        roles=[RoleName.LINE_MANAGER],
        department=department,
    )
    department.line_manager = line_manager
    department.save(update_fields=["line_manager", "updated_at"])
    return department, line_manager


def ensure_loan_settings(**kwargs):
    settings_obj = LoanSettings.get_solo()
    for key, value in kwargs.items():
        setattr(settings_obj, key, value)
    settings_obj.save()
    return settings_obj
