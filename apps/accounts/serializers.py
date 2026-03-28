from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from .models import Department, Role, RoleName, Unit, UserRole

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

    class Meta:
        model = Department
        fields = ("id", "name", "description", "line_manager", "created_at", "updated_at")
        read_only_fields = ("id", "line_manager", "created_at", "updated_at")


class _DepartmentMinimalSerializer(serializers.ModelSerializer):
    """Lightweight read-only department for nesting inside UserSerializer."""

    class Meta:
        model = Department
        fields = ("id", "name")
        read_only_fields = fields


class UnitSerializer(serializers.ModelSerializer):
    department = _DepartmentMinimalSerializer(read_only=True)
    supervisor = _UserMinimalSerializer(read_only=True)
    members = _UserMinimalSerializer(many=True, read_only=True)

    class Meta:
        model = Unit
        fields = ("id", "name", "department", "supervisor", "members", "created_at", "updated_at")
        read_only_fields = ("id", "department", "supervisor", "members", "created_at", "updated_at")


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class UserSerializer(serializers.ModelSerializer):
    """Read-only serializer exposing all safe public fields."""

    full_name = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    department = _DepartmentMinimalSerializer(read_only=True)

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

    class Meta:
        model = User
        fields = ("first_name", "last_name", "phone", "gender", "date_of_birth", "department", "is_active")

    def validate(self, attrs):
        instance = self.instance
        if instance and attrs.get("department") is not None:
            if instance.has_role(RoleName.EXECUTIVE_DIRECTOR) or instance.has_role(RoleName.MANAGING_DIRECTOR):
                raise serializers.ValidationError(
                    {"department": "Executive Director and Managing Director cannot belong to any department."}
                )
        return attrs

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


class UserDepartmentUpdateSerializer(serializers.Serializer):
    """HR-only serializer for changing a user's department."""

    department = serializers.PrimaryKeyRelatedField(queryset=Department.objects.all())

    def validate(self, attrs):
        user = self.context.get("user")
        if user and (
            user.has_role(RoleName.EXECUTIVE_DIRECTOR)
            or user.has_role(RoleName.MANAGING_DIRECTOR)
        ):
            raise serializers.ValidationError(
                "Executive Director and Managing Director cannot belong to any department."
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

        if user.department_id != department.pk:
            raise serializers.ValidationError(
                "The user must belong to this department to be its line manager."
            )

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
