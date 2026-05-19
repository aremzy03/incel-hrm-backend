import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# Role constants
# ---------------------------------------------------------------------------

class Gender(models.TextChoices):
    MALE = "MALE", "Male"
    FEMALE = "FEMALE", "Female"


class MaritalStatus(models.TextChoices):
    SINGLE = "SINGLE", "Single"
    MARRIED = "MARRIED", "Married"
    DIVORCED = "DIVORCED", "Divorced"
    WIDOWED = "WIDOWED", "Widowed"
    SEPARATED = "SEPARATED", "Separated"
    OTHER = "OTHER", "Other"


class ConfirmationStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    CONFIRMED = "CONFIRMED", "Confirmed"
    NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable"


class ContractType(models.TextChoices):
    PERMANENT = "PERMANENT", "Permanent"
    FIXED_TERM = "FIXED_TERM", "Fixed term"
    CONTRACT = "CONTRACT", "Contract"
    INTERN = "INTERN", "Intern"
    OTHER = "OTHER", "Other"


class RoleName(models.TextChoices):
    EMPLOYEE = "EMPLOYEE", "Employee"
    LINE_MANAGER = "LINE_MANAGER", "Line Manager"
    HR = "HR", "HR"
    EXECUTIVE_DIRECTOR = "EXECUTIVE_DIRECTOR", "Executive Director"
    MANAGING_DIRECTOR = "MANAGING_DIRECTOR", "Managing Director"
    SUPERVISOR = "SUPERVISOR", "Supervisor"
    TEAM_LEAD = "TEAM_LEAD", "Team Lead"


# ---------------------------------------------------------------------------
# Department
# ---------------------------------------------------------------------------

HR_DEPARTMENT_NAME = "Human Resources (HR)"
MANAGEMENT_DEPARTMENT_NAME = "Management"


class Department(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    line_manager = models.OneToOneField(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_department",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Department"
        verbose_name_plural = "Departments"
        ordering = ["name"]

    def __str__(self):
        return self.name


class DepartmentMembership(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "User",
        on_delete=models.CASCADE,
        related_name="department_memberships",
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Department Membership"
        verbose_name_plural = "Department Memberships"
        unique_together = ("user", "department")

    def __str__(self):
        return f"{self.user.email} -> {self.department.name}"


class Unit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    department = models.ForeignKey(
        Department,
        on_delete=models.CASCADE,
        related_name="units",
    )
    supervisor = models.OneToOneField(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supervised_unit",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Unit"
        verbose_name_plural = "Units"
        ordering = ["name"]
        unique_together = ("department", "name")

    def __str__(self):
        return f"{self.name} ({self.department.name})"


class Team(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    unit = models.ForeignKey(
        Unit,
        on_delete=models.CASCADE,
        related_name="teams",
    )
    team_lead = models.OneToOneField(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="led_team",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Team"
        verbose_name_plural = "Teams"
        ordering = ["name"]
        unique_together = ("unit", "name")

    def __str__(self):
        return f"{self.name} ({self.unit.name} / {self.unit.department.name})"


def get_or_create_hr_department():
    """Return the HR department, creating it if it does not exist."""
    department, _ = Department.objects.get_or_create(
        name=HR_DEPARTMENT_NAME,
        defaults={"description": "Default department for Human Resources staff."},
    )
    return department


def get_or_create_management_department():
    """Return the Management department, creating it if it does not exist."""
    department, _ = Department.objects.get_or_create(
        name=MANAGEMENT_DEPARTMENT_NAME,
        defaults={"description": "Default department for management chain routing."},
    )
    return department


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class CustomUserManager(BaseUserManager):
    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("The Email field must be set.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    other_names = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=False,
        related_name="members",
    )
    unit = models.ForeignKey(
        Unit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="members",
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="members",
    )
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    staff_id = models.CharField(max_length=64, null=True, blank=True)
    date_of_employment = models.DateField(null=True, blank=True)
    confirmation_status = models.CharField(
        max_length=32,
        choices=ConfirmationStatus.choices,
        blank=True,
    )
    confirmation_date = models.DateField(null=True, blank=True)
    marital_status = models.CharField(
        max_length=20,
        choices=MaritalStatus.choices,
        blank=True,
    )
    last_promotion_date = models.DateField(null=True, blank=True)
    is_exited = models.BooleanField(default=False)
    exit_reason = models.TextField(blank=True)
    job_role = models.CharField(max_length=150, blank=True)
    cost_centre = models.CharField(max_length=100, blank=True)
    regions = models.CharField(max_length=255, blank=True)
    state_of_locations = models.CharField(max_length=100, blank=True)
    state_of_origin = models.CharField(max_length=100, blank=True)
    lga = models.CharField(max_length=100, blank=True)
    religion = models.CharField(max_length=100, blank=True)
    tenure_on_grade = models.CharField(max_length=100, blank=True)
    completeness_score = models.PositiveSmallIntegerField(default=0)

    state_of_residence = models.CharField(max_length=100, blank=True)
    city_of_residence = models.CharField(max_length=100, blank=True)
    landmark_of_residence = models.CharField(max_length=255, blank=True)
    state_of_permanent_address = models.CharField(max_length=100, blank=True)
    city_of_permanent_address = models.CharField(max_length=100, blank=True)
    landmark_of_permanent_address = models.CharField(max_length=255, blank=True)
    residential_address = models.TextField(blank=True)
    permanent_address = models.TextField(blank=True)

    qualification_school = models.CharField(max_length=255, blank=True)
    qualification_course = models.CharField(max_length=255, blank=True)
    qualification_degree = models.CharField(max_length=100, blank=True)
    qualification_grade = models.CharField(max_length=50, blank=True)
    qualification_start_date = models.DateField(null=True, blank=True)
    qualification_end_date = models.DateField(null=True, blank=True)

    certification_institution = models.CharField(max_length=255, blank=True)
    certification_course = models.CharField(max_length=255, blank=True)
    certification_issuing_body = models.CharField(max_length=255, blank=True)
    certification_license_number = models.CharField(max_length=100, blank=True)
    certification_start_date = models.DateField(null=True, blank=True)
    certification_end_date = models.DateField(null=True, blank=True)

    next_of_kin_title = models.CharField(max_length=20, blank=True)
    next_of_kin_first_name = models.CharField(max_length=150, blank=True)
    next_of_kin_last_name = models.CharField(max_length=150, blank=True)
    next_of_kin_phone = models.CharField(max_length=20, blank=True)
    next_of_kin_email = models.EmailField(blank=True)
    next_of_kin_relationship = models.CharField(max_length=100, blank=True)
    next_of_kin_address = models.TextField(blank=True)

    contract_type = models.CharField(
        max_length=20,
        choices=ContractType.choices,
        blank=True,
    )
    contract_start_date = models.DateField(null=True, blank=True)
    contract_end_date = models.DateField(null=True, blank=True)

    objects = CustomUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["-date_joined"]
        constraints = [
            models.UniqueConstraint(
                fields=("staff_id",),
                condition=models.Q(staff_id__isnull=False) & ~models.Q(staff_id=""),
                name="accounts_user_staff_id_unique_when_set",
            ),
        ]

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def has_role(self, role_name: str) -> bool:
        """Return True if this user holds the given role name."""
        return self.user_roles.filter(role__name=role_name).exists()

    def get_roles(self) -> list:
        """Return a list of role name strings assigned to this user."""
        return list(self.user_roles.values_list("role__name", flat=True))

    def get_department_line_manager(self):
        """Return the User who is line manager of this user's department, or None."""
        if self.department_id:
            return getattr(self.department, "line_manager", None)
        return None

    @property
    def is_confirmed(self) -> bool:
        return self.confirmation_status == ConfirmationStatus.CONFIRMED

    def __str__(self):
        return self.email


# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------

class Role(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=50, unique=True, choices=RoleName.choices)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Role"
        verbose_name_plural = "Roles"
        ordering = ["name"]

    def __str__(self):
        return self.get_name_display()


# ---------------------------------------------------------------------------
# UserRole (M:N through table)
# ---------------------------------------------------------------------------

class UserRole(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="user_roles")
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="user_roles")

    class Meta:
        verbose_name = "User Role"
        verbose_name_plural = "User Roles"
        unique_together = ("user", "role")

    def __str__(self):
        return f"{self.user.email} — {self.role.name}"
