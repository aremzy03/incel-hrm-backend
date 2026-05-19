from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import (
    LoanApplication,
    LoanApplicationStatus,
    LoanApprovalLog,
    LoanRepaymentPaymentStatus,
    LoanRepaymentSchedule,
    LoanType,
)
from .services import LoanEligibilityService

User = get_user_model()


# ---------------------------------------------------------------------------
# Nested helpers
# ---------------------------------------------------------------------------

class _EmployeeLoanSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email")
        read_only_fields = fields


class _ActorMinimalSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("first_name", "last_name")
        read_only_fields = fields


# ---------------------------------------------------------------------------
# LoanType
# ---------------------------------------------------------------------------

class LoanTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanType
        fields = ("id", "name", "description")
        read_only_fields = fields


# ---------------------------------------------------------------------------
# LoanRepaymentSchedule
# ---------------------------------------------------------------------------

class LoanRepaymentScheduleSerializer(serializers.ModelSerializer):
    payment_status_display = serializers.CharField(
        source="get_payment_status_display", read_only=True
    )

    class Meta:
        model = LoanRepaymentSchedule
        fields = (
            "id",
            "installment_number",
            "due_date",
            "amount_due",
            "payment_status",
            "payment_status_display",
        )
        read_only_fields = fields


class LoanRepaymentSchedulePaymentStatusSerializer(serializers.Serializer):
    payment_status = serializers.ChoiceField(choices=LoanRepaymentPaymentStatus.choices)


# ---------------------------------------------------------------------------
# LoanApplication — write
# ---------------------------------------------------------------------------

class LoanApplicationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanApplication
        fields = ("loan_type", "amount", "tenure_months", "purpose")

    def validate_tenure_months(self, value):
        if value < 1 or value > 12:
            raise serializers.ValidationError(
                "Repayment period must be between 1 and 12 months."
            )
        return value

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        if request:
            LoanEligibilityService.check_eligibility(request.user)
        return attrs

    def create(self, validated_data):
        return LoanApplication.objects.create(
            employee=self.context["request"].user,
            status=LoanApplicationStatus.DRAFT,
            **validated_data,
        )


# ---------------------------------------------------------------------------
# LoanApplication — read
# ---------------------------------------------------------------------------

class LoanApplicationReadSerializer(serializers.ModelSerializer):
    employee = _EmployeeLoanSerializer(read_only=True)
    loan_type = LoanTypeSerializer(read_only=True)
    repayment_schedule = LoanRepaymentScheduleSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = LoanApplication
        fields = (
            "id",
            "employee",
            "loan_type",
            "amount",
            "tenure_months",
            "monthly_installment",
            "purpose",
            "status",
            "status_display",
            "outstanding_balance",
            "disbursed_at",
            "closed_at",
            "resignation_deducted",
            "repayment_schedule",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


# ---------------------------------------------------------------------------
# LoanApprovalLog
# ---------------------------------------------------------------------------

class LoanApprovalLogSerializer(serializers.ModelSerializer):
    actor = _ActorMinimalSerializer(read_only=True)
    action_display = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = LoanApprovalLog
        fields = (
            "id",
            "loan",
            "actor",
            "action",
            "action_display",
            "comment",
            "previous_status",
            "new_status",
            "timestamp",
        )
        read_only_fields = fields


class LoanApprovalLogNestedSerializer(serializers.ModelSerializer):
    """Approval log nested under a loan (no redundant loan FK)."""

    actor = _ActorMinimalSerializer(read_only=True)
    action_display = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = LoanApprovalLog
        fields = (
            "id",
            "actor",
            "action",
            "action_display",
            "comment",
            "previous_status",
            "new_status",
            "timestamp",
        )
        read_only_fields = fields


class LoanApplicationLedgerSerializer(LoanApplicationReadSerializer):
    """Full loan history for an employee including approval audit trail."""

    logs = LoanApprovalLogNestedSerializer(many=True, read_only=True)

    class Meta(LoanApplicationReadSerializer.Meta):
        fields = (*LoanApplicationReadSerializer.Meta.fields, "logs")
