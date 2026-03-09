from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import Role, UserRole

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """Read-only serializer exposing all safe public fields."""

    full_name = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "phone",
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

    class Meta:
        model = User
        fields = ("email", "password", "password_confirm", "first_name", "last_name")

    def validate(self, attrs):
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


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
        user_role, created = UserRole.objects.get_or_create(user=user, role=role)
        if not created:
            raise serializers.ValidationError({"role_id": "User already has this role."})
        return user_role
