from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from .models import Department, Role, RoleName, Team, Unit, UserRole

User = get_user_model()


# ---------------------------------------------------------------------------
# Department
# ---------------------------------------------------------------------------

class _UserMinimalSerializer(serializers.ModelSerializer):
    """Lightweight read-only user for nesting (e.g. line_manager inside Department)."""

    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name")
        read_only_fields = fields


class DepartmentSerializer(serializers.ModelSerializer):
    line_manager = _UserMinimalSerializer(read_only=True)
    members_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Department
        fields = ("id", "name", "description", "line_manager", "members_count", "created_at", "updated_at")
        read_only_fields = ("id", "line_manager", "members_count", "created_at", "updated_at")


class _DepartmentMinimalSerializer(serializers.ModelSerializer):
    """Lightweight read-only department for nesting inside UserSerializer."""

    class Meta:
        model = Department
        fields = ("id", "name")
        read_only_fields = fields


class _UnitMinimalSerializer(serializers.ModelSerializer):
    """Lightweight read-only unit for nesting inside UserSerializer."""

    class Meta:
        model = Unit
        fields = ("id", "name")
        read_only_fields = fields


class _TeamMinimalSerializer(serializers.ModelSerializer):
    """Lightweight read-only team for nesting inside UserSerializer."""

    class Meta:
        model = Team
        fields = ("id", "name")
        read_only_fields = fields


class UnitSerializer(serializers.ModelSerializer):
    department = _DepartmentMinimalSerializer(read_only=True)
    department_id = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(),
        source="department",
        write_only=True,
        required=True,
    )
    supervisor = _UserMinimalSerializer(read_only=True)
    supervisor_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        source="supervisor",
        write_only=True,
        required=False,
        allow_null=True,
    )
    members = _UserMinimalSerializer(many=True, read_only=True)

    class Meta:
        model = Unit
        fields = (
            "id",
            "name",
            "department",
            "department_id",
            "supervisor",
            "supervisor_id",
            "members",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "department", "supervisor", "members", "created_at", "updated_at")

    def validate(self, attrs):
        # Department is set at creation time and must not be reassigned later.
        if self.instance is not None and "department" in attrs:
            raise serializers.ValidationError({"department_id": "Department cannot be changed once a unit is created."})

        # Supervisor (if provided) must be in the same department as the unit.
        unit_department = getattr(self.instance, "department", None) or attrs.get("department")
        supervisor = attrs.get("supervisor")
        if supervisor is not None and unit_department is not None:
            if supervisor.department_id != unit_department.pk:
                raise serializers.ValidationError(
                    {"supervisor_id": "Supervisor must belong to the same department as the unit."}
                )
        return attrs

    def to_internal_value(self, data):
        """
        Backwards-compatible write alias.

        Frontends may send `supervisor` as a UUID string; internally we accept `supervisor_id`.
        """
        if isinstance(data, dict) and "supervisor" in data and "supervisor_id" not in data:
            data = {**data, "supervisor_id": data.get("supervisor")}
        return super().to_internal_value(data)


class TeamSerializer(serializers.ModelSerializer):
    unit = _UnitMinimalSerializer(read_only=True)
    unit_id = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.select_related("department").all(),
        source="unit",
        write_only=True,
        required=True,
    )
    team_lead = _UserMinimalSerializer(read_only=True)
    team_lead_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.select_related("department", "unit").all(),
        source="team_lead",
        write_only=True,
        required=False,
        allow_null=True,
    )
    members = _UserMinimalSerializer(many=True, read_only=True)

    class Meta:
        model = Team
        fields = (
            "id",
            "name",
            "unit",
            "unit_id",
            "team_lead",
            "team_lead_id",
            "members",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "unit", "team_lead", "members", "created_at", "updated_at")

    def validate(self, attrs):
        # Unit is set at creation time and must not be reassigned later.
        if self.instance is not None and "unit" in attrs:
            raise serializers.ValidationError({"unit_id": "Unit cannot be changed once a team is created."})

        team_unit = getattr(self.instance, "unit", None) or attrs.get("unit")
        team_lead = attrs.get("team_lead")

        if team_lead is not None and team_unit is not None:
            if team_lead.unit_id != team_unit.pk:
                raise serializers.ValidationError({"team_lead_id": "Team lead must belong to the same unit as the team."})

        return attrs


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class UserSerializer(serializers.ModelSerializer):
    """Read-only serializer exposing all safe public fields."""

    full_name = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    department = _DepartmentMinimalSerializer(read_only=True)
    unit = _UnitMinimalSerializer(read_only=True)
    team = _TeamMinimalSerializer(read_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "phone",
            "gender",
            "date_of_birth",
            "department",
            "unit",
            "team",
            "is_active",
            "roles",
            "date_joined",
            "updated_at",
        )
        read_only_fields = fields

    def get_full_name(self, obj):
        return obj.get_full_name()

    def get_roles(self, obj):
        return obj.get_roles()


class RegisterSerializer(serializers.ModelSerializer):
    """Write serializer for new user registration."""

    password = serializers.CharField(write_only=True, validators=[validate_password])
    password_confirm = serializers.CharField(write_only=True)
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = User
        fields = ("email", "password", "password_confirm", "first_name", "last_name", "gender", "date_of_birth", "department")
        extra_kwargs = {"gender": {"required": True}, "date_of_birth": {"required": True}}

    def validate(self, attrs):
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        return attrs

    def create(self, validated_data):
        with transaction.atomic():
            user = User.objects.create_user(**validated_data)
            role, _ = Role.objects.get_or_create(
                name=RoleName.EMPLOYEE,
                defaults={"description": "Employee"},
            )
            UserRole.objects.get_or_create(user=user, role=role)
        return user


class UserSelfUpdateSerializer(serializers.ModelSerializer):
    """
    Serializer for employees updating their own profile.

    Only allows editing non-privileged personal fields.
    """

    class Meta:
        model = User
        fields = ("first_name", "last_name", "phone", "gender", "date_of_birth")


class UserCreateSerializer(serializers.ModelSerializer):
    """HR-only serializer for creating users."""

    password = serializers.CharField(write_only=True, validators=[validate_password])
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = User
        fields = ("email", "password", "first_name", "last_name", "phone", "gender", "date_of_birth", "department")
        extra_kwargs = {"gender": {"required": True}, "date_of_birth": {"required": True}}

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class UserUpdateSerializer(serializers.ModelSerializer):
    """HR-only serializer for updating users."""

    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(),
        required=False,
    )
    unit = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.select_related("department").all(),
        required=False,
        allow_null=True,
    )
    team = serializers.PrimaryKeyRelatedField(
        queryset=Team.objects.select_related("unit", "unit__department").all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = User
        fields = ("first_name", "last_name", "phone", "gender", "date_of_birth", "department", "unit", "team", "is_active")

    def validate(self, attrs):
        instance = self.instance
        if instance and attrs.get("department") is not None:
            if instance.has_role(RoleName.EXECUTIVE_DIRECTOR) or instance.has_role(RoleName.MANAGING_DIRECTOR):
                raise serializers.ValidationError(
                    {"department": "Executive Director and Managing Director cannot belong to any department."}
                )

        # Unit membership must be consistent with department.
        if instance is not None:
            new_department = attrs.get("department", instance.department)
            new_unit = attrs.get("unit", instance.unit)
            new_team = attrs.get("team", instance.team)

            if new_unit is not None and new_department is not None:
                if new_unit.department_id != new_department.pk:
                    raise serializers.ValidationError(
                        {"unit": "User unit must belong to the same department as the user."}
                    )

            if new_team is not None:
                if new_unit is None:
                    raise serializers.ValidationError({"team": "User cannot be assigned to a team without being assigned to a unit."})
                if new_team.unit_id != new_unit.pk:
                    raise serializers.ValidationError({"team": "User team must belong to the same unit as the user."})

            # If department changes while keeping an existing unit, enforce consistency.
            if "department" in attrs and "unit" not in attrs and instance.unit_id is not None:
                if new_department is not None and instance.unit.department_id != new_department.pk:
                    raise serializers.ValidationError(
                        {"department": "User cannot move departments without clearing or updating their unit."}
                    )

            # If unit changes while keeping an existing team, enforce consistency.
            if "unit" in attrs and "team" not in attrs and instance.team_id is not None:
                if new_unit is None:
                    raise serializers.ValidationError({"unit": "User cannot clear unit without also clearing team."})
                if instance.team.unit_id != new_unit.pk:
                    raise serializers.ValidationError({"unit": "User cannot change unit without clearing or updating their team."})

        return attrs

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


class UserDepartmentUpdateSerializer(serializers.Serializer):
    """HR-only serializer for changing a user's department."""

    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(),
        required=False,
        allow_null=True,
    )

    def validate(self, attrs):
        user = self.context.get("user")
        if user and (
            user.has_role(RoleName.EXECUTIVE_DIRECTOR)
            or user.has_role(RoleName.MANAGING_DIRECTOR)
        ):
            raise serializers.ValidationError(
                "Executive Director and Managing Director cannot belong to any department."
            )

        # Support {"department": null} to clear department.
        # If department is cleared, unit/team must also be cleared (to avoid dangling org membership).
        if "department" in attrs and attrs.get("department") is None:
            if getattr(user, "unit_id", None) is not None or getattr(user, "team_id", None) is not None:
                raise serializers.ValidationError(
                    {"department": "Cannot clear department while user still has a unit or team. Clear unit/team first."}
                )
        return attrs


class DepartmentLineManagerSerializer(serializers.Serializer):
    """Assign a line manager to a department (HR / ED only)."""

    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        source="user",
    )

    def validate_user_id(self, user):
        department = self.context["department"]

        if hasattr(user, "managed_department") and user.managed_department.pk != department.pk:
            raise serializers.ValidationError(
                "This user is already the line manager of another department."
            )

        return user


# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------

class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ("id", "name", "description", "created_at")
        read_only_fields = ("id", "created_at")


class UserRoleSerializer(serializers.ModelSerializer):
    role_id = serializers.UUIDField(write_only=True)
    role = RoleSerializer(read_only=True)

    class Meta:
        model = UserRole
        fields = ("id", "role_id", "role")
        read_only_fields = ("id", "role")

    def validate_role_id(self, value):
        try:
            return Role.objects.get(pk=value)
        except Role.DoesNotExist:
            raise serializers.ValidationError("Role not found.")

    def create(self, validated_data):
        role = validated_data.pop("role_id")
        user = validated_data["user"]
        user_role, _ = UserRole.objects.get_or_create(user=user, role=role)
        return user_role


# ---------------------------------------------------------------------------
# Password change / reset
# ---------------------------------------------------------------------------

_password_field_kwargs = {"write_only": True, "style": {"input_type": "password"}}


class PasswordChangeSerializer(serializers.Serializer):
    """Self-service password change: current + new + confirm."""

    current_password = serializers.CharField(**_password_field_kwargs)
    new_password = serializers.CharField(**_password_field_kwargs)
    new_password_confirm = serializers.CharField(**_password_field_kwargs)

    def validate(self, attrs):
        request = self.context.get("request")
        if request is None or not getattr(request, "user", None) or not request.user.is_authenticated:
            raise serializers.ValidationError("Authentication required.")

        user = request.user
        if not user.check_password(attrs["current_password"]):
            raise serializers.ValidationError({"current_password": "Current password is incorrect."})

        if attrs["new_password"] != attrs.pop("new_password_confirm"):
            raise serializers.ValidationError({"new_password_confirm": "Passwords do not match."})

        if user.check_password(attrs["new_password"]):
            raise serializers.ValidationError(
                {"new_password": "New password must be different from the current password."}
            )

        validate_password(attrs["new_password"], user=user)
        return attrs


class PasswordResetSerializer(serializers.Serializer):
    """HR/admin reset: new + confirm only (target user in context as `user`)."""

    new_password = serializers.CharField(**_password_field_kwargs)
    new_password_confirm = serializers.CharField(**_password_field_kwargs)

    def validate(self, attrs):
        user = self.context.get("user")
        if user is None:
            raise serializers.ValidationError("Target user is required in serializer context.")

        if attrs["new_password"] != attrs.pop("new_password_confirm"):
            raise serializers.ValidationError({"new_password_confirm": "Passwords do not match."})

        if user.check_password(attrs["new_password"]):
            raise serializers.ValidationError(
                {"new_password": "New password must be different from the current password."}
            )

        validate_password(attrs["new_password"], user=user)
        return attrs


# ---------------------------------------------------------------------------
# Bulk membership operations
# ---------------------------------------------------------------------------

class BulkUserIdsSerializer(serializers.Serializer):
    """
    Shared request serializer for bulk user assignment endpoints.

    Endpoints implement partial success; this serializer only validates shape.
    """

    user_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
    )
    dry_run = serializers.BooleanField(required=False, default=False)
    clear_conflicts = serializers.BooleanField(required=False, default=False)

    def validate_user_ids(self, value):
        # Preserve order while removing duplicates.
        seen = set()
        unique_ids = []
        for v in value:
            if v not in seen:
                seen.add(v)
                unique_ids.append(v)
        return unique_ids
