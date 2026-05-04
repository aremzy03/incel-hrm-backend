import csv
import datetime
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from apps.accounts.models import Department, Role, RoleName, UserRole
from apps.leave.models import LeaveBalance, LeaveRequest, LeaveRequestStatus, LeaveType
from apps.leave.serializers import LeaveRequestCreateSerializer

User = get_user_model()


class LeaveRequestSameDaySerializerTests(TestCase):
    """start_date == end_date is allowed (single working day)."""

    def setUp(self):
        self.department = Department.objects.create(name="Seed Test Dept")
        self.user = User.objects.create_user(
            email="seedserializer@example.com",
            password="x",
            department=self.department,
        )
        role, _ = Role.objects.get_or_create(name=RoleName.EMPLOYEE)
        UserRole.objects.get_or_create(user=self.user, role=role)

        self.leave_type, _ = LeaveType.objects.get_or_create(
            name="Annual",
            defaults={"description": "", "default_days": 21},
        )

        self.monday = datetime.date(2030, 6, 3)
        LeaveBalance.objects.update_or_create(
            employee=self.user,
            leave_type=self.leave_type,
            year=self.monday.year,
            defaults={"allocated_days": 21, "used_days": 0},
        )

    def test_same_start_and_end_passes_validation(self):
        factory = APIRequestFactory()
        request = factory.post("/api/v1/leave-requests/")
        request.user = self.user

        serializer = LeaveRequestCreateSerializer(
            data={
                "leave_type": self.leave_type.pk,
                "start_date": self.monday,
                "end_date": self.monday,
                "reason": "",
            },
            context={"request": request},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)


class SeedLeaveReportFromCsvCommandTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Seed Cmd Dept")
        self.user = User.objects.create_user(
            email="seedcmd@example.com",
            password="x",
            department=self.department,
        )
        role, _ = Role.objects.get_or_create(name=RoleName.EMPLOYEE)
        UserRole.objects.get_or_create(user=self.user, role=role)

        self.leave_type, _ = LeaveType.objects.get_or_create(
            name="Annual",
            defaults={"description": "", "default_days": 21},
        )
        self.sick_type, _ = LeaveType.objects.get_or_create(
            name="Sick",
            defaults={"description": "", "default_days": 14},
        )

    def _write_csv(self, rows: List[Dict[str, Any]]) -> Path:
        header = [
            "email Address",
            "Employee Leave Request Start Date",
            "Employee Leave Request End Date",
            "Employee Leave Request Days Taken",
            "Leave Type Name",
            "Employee Leave Request Status",
            "Employee Leave Request Resumption Date",
            "Employee Leave Application Date",
            "Leave Type Description",
            "Employee Name",
        ]
        f = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".csv",
            delete=False,
            encoding="utf-8",
            newline="",
        )
        try:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for r in rows:
                w.writerow(r)
            f.flush()
            return Path(f.name)
        finally:
            f.close()

    def test_approved_increments_used_days_rejected_does_not(self):
        path = self._write_csv(
            [
                {
                    "email Address": "seedcmd@example.com",
                    "Employee Leave Request Start Date": "03/06/2030",
                    "Employee Leave Request End Date": "03/06/2030",
                    "Employee Leave Request Days Taken": "1",
                    "Leave Type Name": "Annual",
                    "Employee Leave Request Status": "APPROVED",
                    "Employee Leave Request Resumption Date": "04/06/2030",
                    "Employee Leave Application Date": "01/06/2030",
                    "Leave Type Description": "x",
                    "Employee Name": "Test",
                },
                {
                    "email Address": "seedcmd@example.com",
                    "Employee Leave Request Start Date": "10/06/2030",
                    "Employee Leave Request End Date": "10/06/2030",
                    "Employee Leave Request Days Taken": "1",
                    "Leave Type Name": "Sick",
                    "Employee Leave Request Status": "REJECTED",
                    "Employee Leave Request Resumption Date": "11/06/2030",
                    "Employee Leave Application Date": "09/06/2030",
                    "Leave Type Description": "y",
                    "Employee Name": "Test",
                },
            ]
        )
        try:
            call_command("seed_leave_report_from_csv", str(path))
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(LeaveRequest.objects.count(), 2)
        bal_annual = LeaveBalance.objects.get(
            employee=self.user,
            leave_type=self.leave_type,
            year=2030,
        )
        self.assertEqual(bal_annual.used_days, 1)
        bal_sick = LeaveBalance.objects.get(
            employee=self.user,
            leave_type=self.sick_type,
            year=2030,
        )
        self.assertEqual(bal_sick.used_days, 0)

    def test_duplicate_row_skipped_second_run(self):
        row = {
            "email Address": "seedcmd@example.com",
            "Employee Leave Request Start Date": "03/06/2030",
            "Employee Leave Request End Date": "03/06/2030",
            "Employee Leave Request Days Taken": "1",
            "Leave Type Name": "Annual",
            "Employee Leave Request Status": "APPROVED",
            "Employee Leave Request Resumption Date": "04/06/2030",
            "Employee Leave Application Date": "01/06/2030",
            "Leave Type Description": "x",
            "Employee Name": "Test",
        }
        path = self._write_csv([row, row])
        try:
            call_command("seed_leave_report_from_csv", str(path))
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(LeaveRequest.objects.count(), 1)
        bal = LeaveBalance.objects.get(
            employee=self.user,
            leave_type=self.leave_type,
            year=2030,
        )
        self.assertEqual(bal.used_days, 1)

    def test_second_full_import_skips_db_duplicate(self):
        row = {
            "email Address": "seedcmd@example.com",
            "Employee Leave Request Start Date": "03/06/2030",
            "Employee Leave Request End Date": "03/06/2030",
            "Employee Leave Request Days Taken": "1",
            "Leave Type Name": "Annual",
            "Employee Leave Request Status": "APPROVED",
            "Employee Leave Request Resumption Date": "04/06/2030",
            "Employee Leave Application Date": "01/06/2030",
            "Leave Type Description": "x",
            "Employee Name": "Test",
        }
        path = self._write_csv([row])
        try:
            call_command("seed_leave_report_from_csv", str(path))
            call_command("seed_leave_report_from_csv", str(path))
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(LeaveRequest.objects.count(), 1)
        bal = LeaveBalance.objects.get(
            employee=self.user,
            leave_type=self.leave_type,
            year=2030,
        )
        self.assertEqual(bal.used_days, 1)

    def test_dry_run_does_not_create(self):
        path = self._write_csv(
            [
                {
                    "email Address": "seedcmd@example.com",
                    "Employee Leave Request Start Date": "03/06/2030",
                    "Employee Leave Request End Date": "03/06/2030",
                    "Employee Leave Request Days Taken": "1",
                    "Leave Type Name": "Annual",
                    "Employee Leave Request Status": "APPROVED",
                    "Employee Leave Request Resumption Date": "04/06/2030",
                    "Employee Leave Application Date": "01/06/2030",
                    "Leave Type Description": "x",
                    "Employee Name": "Test",
                },
            ]
        )
        try:
            call_command("seed_leave_report_from_csv", str(path), "--dry-run")
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(LeaveRequest.objects.count(), 0)
        self.assertFalse(
            LeaveBalance.objects.filter(employee=self.user, year=2030).exists()
        )
