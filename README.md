# Incel HRM Backend

Django 4.2 REST API for human-resource management.

## Quick Start

```bash
# 1. Create & activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies (development)
pip install -r requirements/dev.txt

# 3. Copy the example env file and edit it
cp .env.example .env

# 4. Run migrations
python manage.py migrate

# 5. Create a superuser
python manage.py createsuperuser

# 6. Start the development server
python manage.py runserver
```

## Settings

The project uses a split-settings layout under `hrm_backend/settings/`:

| File      | Purpose                                    |
|-----------|--------------------------------------------|
| `base.py` | Shared configuration (apps, DRF, timezone) |
| `dev.py`  | Development overrides (DEBUG, SQLite)      |
| `prod.py` | Production overrides (security, HTTPS)     |

Switch the active settings module via the `DJANGO_SETTINGS_MODULE` environment variable:

```bash
export DJANGO_SETTINGS_MODULE=hrm_backend.settings.prod
```

## Project Layout

```
hrm_backend/          - Django project configuration
hrm_backend/settings/ - Split settings (base / dev / prod)
apps/
  accounts/           - Custom User model, JWT auth, RBAC, Departments, user signals
  leave/              - Leave management, department calendar
requirements/         - Pip requirement files split by environment
```

## Authentication

All auth endpoints are mounted at `/api/v1/auth/`.

| Method | Endpoint                    | Auth required | Description                        |
|--------|-----------------------------|---------------|------------------------------------|
| POST   | `/api/v1/auth/register/`    | No            | Create a new user account          |
| POST   | `/api/v1/auth/login/`       | No            | Obtain JWT access + refresh tokens |
| POST   | `/api/v1/auth/token/refresh/` | No          | Refresh an access token            |
| GET    | `/api/v1/auth/me/`          | Yes (JWT)     | Return the current user's profile  |

### Example — register then login

```bash
# Register
curl -s -X POST http://localhost:8000/api/v1/auth/register/ \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@example.com","password":"str0ngPass!","password_confirm":"str0ngPass!","first_name":"Alice","last_name":"Smith","gender":"FEMALE","date_of_birth":"1990-05-15","department":"<dept_uuid>"}'

# Login — returns access + refresh tokens
curl -s -X POST http://localhost:8000/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@example.com","password":"str0ngPass!"}'

# Fetch own profile
curl -s http://localhost:8000/api/v1/auth/me/ \
  -H "Authorization: Bearer <access_token>"
```

## Custom User Model

`apps/accounts/models.User` replaces Django's built-in user:

| Field         | Type          | Notes                          |
|---------------|---------------|--------------------------------|
| `id`          | UUID          | Primary key, auto-generated    |
| `email`       | EmailField    | Unique, used as login field    |
| `first_name`   | CharField     |                                |
| `last_name`    | CharField     |                                |
| `phone`        | CharField     | Optional                       |
| `gender`       | CharField     | MALE/FEMALE. Required on registration |
| `date_of_birth`| DateField     | Required on registration       |
| `department`   | FK → Department | Required on registration, nullable in DB |
| `is_active`   | BooleanField  | Default `True`                 |
| `is_staff`    | BooleanField  | Default `False`                |
| `date_joined` | DateTimeField | Set on creation                |
| `updated_at`  | DateTimeField | Auto-updated on every save     |

### Helpers

```python
user.has_role("HR")                     # → bool
user.get_roles()                        # → ["HR", "LINE_MANAGER"]
user.get_department_line_manager()      # → User or None
```

## Role-Based Access Control (RBAC)

### Built-in roles

The following roles are seeded automatically on first migrate:

| Role                  | Description                                         |
|-----------------------|-----------------------------------------------------|
| `EMPLOYEE`            | Default role for all staff members; auto-assigned on registration |
| `LINE_MANAGER`        | Head of one department; first approver in leave chain |
| `HR`                  | Human Resources — manages employee records          |
| `EXECUTIVE_DIRECTOR`  | Elevated approval rights                            |
| `MANAGING_DIRECTOR`   | Highest-level access                                |

### Default EMPLOYEE role

Every newly created user (via registration, HR create, or admin) is automatically assigned the `EMPLOYEE` role by a `post_save` signal in `apps/accounts/signals.py`.

### RBAC endpoints

| Method | Endpoint                               | Permission          | Description                      |
|--------|----------------------------------------|---------------------|----------------------------------|
| GET    | `/api/v1/roles/`                       | HR or Admin         | List all roles                   |
| POST   | `/api/v1/roles/`                       | HR or Admin         | Create a new role                |
| GET    | `/api/v1/roles/:id/`                   | HR or Admin         | Retrieve a role                  |
| PUT    | `/api/v1/roles/:id/`                   | HR or Admin         | Full update                      |
| PATCH  | `/api/v1/roles/:id/`                  | HR or Admin         | Partial update                   |
| DELETE | `/api/v1/roles/:id/`                   | HR or Admin         | Delete a role                    |
| POST   | `/api/v1/users/<id>/roles/`            | HR or Admin         | Set user's role (overwrites any existing role) |
| DELETE | `/api/v1/users/<id>/roles/<role_id>/`  | HR or Admin         | Remove a role from a user        |

**One role per user**: Each user holds at most one role at a time. Assigning a new role replaces any existing role.

### DRF permission classes

Import from `apps.accounts.permissions`:

```python
from apps.accounts.permissions import (
    IsEmployee,
    IsLineManager,
    IsHR,
    IsExecutiveDirector,
    IsManagingDirector,
)

class MyView(APIView):
    permission_classes = [IsAuthenticated, IsHR | IsManagingDirector]
```

### Example — assign the HR role to a user

```bash
# Assign role
curl -s -X POST http://localhost:8000/api/v1/users/<user_uuid>/roles/ \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"role_id": "<role_uuid>"}'

# Remove role
curl -s -X DELETE \
  http://localhost:8000/api/v1/users/<user_uuid>/roles/<role_uuid>/ \
  -H "Authorization: Bearer <access_token>"
```

---

## Users (HR CRUD)

HR staff and admins can perform full CRUD on users via `/api/v1/users/`.

| Method | Endpoint | Permission | Description |
|--------|----------|-----------|-------------|
| GET | `/api/v1/users/` | HR or Admin | List all users |
| POST | `/api/v1/users/` | HR or Admin | Create a user |
| GET | `/api/v1/users/:id/` | HR or Admin | Retrieve one |
| PUT | `/api/v1/users/:id/` | HR or Admin | Full update |
| PATCH | `/api/v1/users/:id/` | HR or Admin | Partial update |
| DELETE | `/api/v1/users/:id/` | HR or Admin | Delete a user |

**Create payload** (`POST /api/v1/users/`):

```json
{
  "email": "jane@example.com",
  "password": "str0ngPass!",
  "first_name": "Jane",
  "last_name": "Doe",
  "phone": "+234...",
  "gender": "FEMALE",
  "date_of_birth": "1990-05-15",
  "department": "<dept_uuid>"
}
```

**Update payload** (`PATCH /api/v1/users/:id/`): `first_name`, `last_name`, `phone`, `gender`, `date_of_birth`, `department`, `is_active` (all optional).

---

## Departments

### Department model

| Field          | Type               | Notes                          |
|----------------|--------------------|--------------------------------|
| `id`           | UUID               | Primary key, auto-generated    |
| `name`         | CharField          | Unique, max 150 chars          |
| `description`  | TextField          | Optional                       |
| `line_manager` | OneToOneField → User | Nullable. Each dept has at most one line manager; each user can manage at most one dept |
| `created_at`   | DateTimeField      | Auto-set on creation           |
| `updated_at`   | DateTimeField      | Auto-updated on save           |

### Department endpoints

| Method | Endpoint | Permission | Description |
|--------|----------|-----------|-------------|
| GET | `/api/v1/departments/` | Anyone | List all departments |
| POST | `/api/v1/departments/` | HR or Admin | Create a department |
| GET | `/api/v1/departments/:id/` | Anyone | Retrieve one |
| PUT | `/api/v1/departments/:id/` | HR or Admin | Full update |
| PATCH | `/api/v1/departments/:id/` | HR or Admin | Partial update |
| DELETE | `/api/v1/departments/:id/` | HR or Admin | Delete a department |
| PATCH | `/api/v1/users/:id/department/` | HR or Admin | Change a user's department |
| GET | `/api/v1/departments/:id/members/` | Own dept or HR/ED/MD | List users in a department |

### Department members

Any user can view other users in their own department. HR, ED, and MD can view members of any department.

### Line Manager assignment

Each department can have exactly one Line Manager. HR staff and the Executive Director can assign or revoke this role.

| Method | Endpoint | Permission | Description |
|--------|----------|-----------|-------------|
| POST | `/api/v1/departments/:id/line-manager/` | HR / ED / Admin | Assign a line manager |
| DELETE | `/api/v1/departments/:id/line-manager/` | HR / ED / Admin | Remove the line manager |

**Assign payload:**

```json
{ "user_id": "<uuid of user in this department>" }
```

Validations:
- The user must belong to the target department.
- The user must not already be line manager of another department.
- If the user does not already hold the `LINE_MANAGER` role, it is granted automatically.

### Example — create a department, assign a line manager, and reassign a user

```bash
# Create department
curl -s -X POST http://localhost:8000/api/v1/departments/ \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"name": "Engineering", "description": "Software engineering team"}'

# Assign line manager
curl -s -X POST http://localhost:8000/api/v1/departments/<dept_uuid>/line-manager/ \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "<user_uuid>"}'

# Remove line manager
curl -s -X DELETE http://localhost:8000/api/v1/departments/<dept_uuid>/line-manager/ \
  -H "Authorization: Bearer <token>"

# Change a user's department
curl -s -X PATCH http://localhost:8000/api/v1/users/<user_uuid>/department/ \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"department": "<dept_uuid>"}'
```

---

## Department Leave Calendar

Provides a consolidated view of approved leave within a department (or across all departments for privileged roles).

### Endpoint

```
GET /api/v1/calendar/?year=<int>&month=<int>[&department=<uuid>]
```

### Scoping rules

| Role | Scope |
|------|-------|
| Employee / Line Manager | Own department only |
| HR / ED / MD / Admin | All departments (optionally filter by `?department=<uuid>`) |

### Query parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `year` | No | Current year | Filter by year |
| `month` | No | — | Filter by month (1-12). If omitted, returns the full year |
| `department` | No | — | Filter by department UUID (privileged roles only) |

### Response

Each entry contains:

```json
{
  "id": "<leave_request_uuid>",
  "employee": {
    "id": "<uuid>",
    "email": "alice@example.com",
    "first_name": "Alice",
    "last_name": "Smith",
    "department_name": "Engineering"
  },
  "leave_type": {
    "id": "<uuid>",
    "name": "Annual",
    "description": "",
    "default_days": 21,
    "created_at": "...",
    "updated_at": "..."
  },
  "start_date": "2025-03-10",
  "end_date": "2025-03-14",
  "total_working_days": 5
}
```

---

## Leave Management

### Data model overview

```
LeaveType ──< LeavePolicy
LeaveType ──< LeaveBalance >── User
LeaveType ──< LeaveRequest >── User (employee)
               LeaveRequest ──> User (cover_person)
               LeaveRequest ──< LeaveApprovalLog >── User (actor)
PublicHoliday  (standalone)
Department ──> User (line_manager, OneToOne)
```

### Models

#### `LeaveType`

Seeded automatically via data migration:

| Name       | Default days | Eligibility        |
|------------|--------------|--------------------|
| Annual     | 21           | All                |
| Sick       | 14           | All                |
| Casual     | 5            | All                |
| Maternity  | 90           | Female staff only  |
| Paternity  | 14           | Male staff only    |

#### `LeavePolicy`

Per-leave-type configuration controlling entitlement rules:

| Field                      | Default | Description                               |
|----------------------------|---------|-------------------------------------------|
| `annual_entitlement`       | —       | Days allocated per year                   |
| `carry_forward`            | `False` | Unused days do not roll over              |
| `half_day_allowed`         | `False` | Half-day requests not permitted           |
| `weekend_excluded`         | `True`  | Weekends not counted as leave days        |
| `public_holiday_excluded`  | `True`  | Public holidays not counted               |
| `forfeited_on_resignation` | `True`  | Unused balance forfeited on resignation   |

#### `PublicHoliday`

Stores named public holidays. `is_recurring=True` means the holiday repeats on the same calendar date every year.

#### `LeaveBalance`

Tracks per-employee, per-leave-type entitlement for a given year.

**Auto-creation**: When a user is created (registration or HR create), a `post_save` signal creates `LeaveBalance` rows for the current year for each eligible leave type. `allocated_days` is set to `leave_type.default_days`. Maternity is omitted for male users; Paternity is omitted for female users.

```python
balance.remaining_days  # property: allocated_days - used_days
```

Unique constraint: `(employee, leave_type, year)`.

#### `LeaveRequest`

| Field               | Notes                                                       |
|---------------------|-------------------------------------------------------------|
| `cover_person`      | FK → User. Required. Must be in the same department, cannot be the applicant |
| `total_working_days`| Computed automatically on `save()` (Mon–Fri only)          |
| `is_emergency`      | Flag for urgent requests                                    |
| `status`            | See workflow below                                          |

#### Leave request workflow

```
DRAFT → PENDING_MANAGER → PENDING_HR → PENDING_ED → APPROVED
                                                   ↘ REJECTED
          (any stage) ──────────────────────────→ CANCELLED
```

| Status             | Meaning                                     |
|--------------------|---------------------------------------------|
| `DRAFT`            | Saved but not yet submitted                 |
| `PENDING_MANAGER`  | Awaiting line manager approval              |
| `PENDING_HR`       | Awaiting HR approval                        |
| `PENDING_ED`       | Awaiting Executive Director approval        |
| `APPROVED`         | Fully approved                              |
| `REJECTED`         | Declined at any stage                       |
| `CANCELLED`        | Withdrawn by the employee                   |

#### `LeaveApprovalLog`

Immutable audit trail. One entry is appended per status transition. Fields include `actor`, `action` (`APPROVE / REJECT / CANCEL / MODIFY`), `previous_status`, `new_status`, `comment`, and `timestamp`.

### Business rules

1. **One Line Manager per department** -- Each department has at most one line manager (`Department.line_manager`). A user can manage at most one department (OneToOneField). HR and ED can assign/revoke via `POST/DELETE /api/v1/departments/:id/line-manager/`.

2. **Cover person required** -- When creating a leave request, the employee must designate a `cover_person` (another active user in the same department) who will handle their responsibilities during the leave period. The cover person cannot be the requesting employee.

3. **Maternity and Paternity by gender** -- Maternity leave is only available for female staff; Paternity leave is only available for male staff. Leave balances for these types are created only for eligible users.

4. **Default leave balances** -- On user creation, `LeaveBalance` rows are auto-created for the current year for each eligible leave type, with `allocated_days` = `leave_type.default_days`.

5. **Department leave exclusivity (Annual & Casual only)** -- For Annual and Casual leave only, at most one employee in a department may have an active leave request overlapping any given date range. Sick, Maternity, Paternity, and other leave types are excluded; multiple employees may be on those types simultaneously. If another colleague's Annual or Casual leave already covers the requested dates, the request is rejected at validation time.

6. **Submit requires a line manager** -- An employee cannot submit a DRAFT request (`POST .../submit/`) unless their department has a line manager assigned. This ensures the approval chain is complete before a request enters the pipeline.

7. **Approval chain** -- When a request is submitted, it flows through: department Line Manager → HR → Executive Director. Each approver can only act at their designated stage.

8. **Line Manager scoped visibility** -- A Line Manager's `GET /api/v1/leave-requests/` queryset is now scoped to their own department (not all requests).

---

## Services

Business logic lives in `apps/leave/services.py` rather than in views or serializers, keeping it testable and reusable.

### `WorkingDaysService`

#### `calculate_working_days(start_date, end_date) -> int`

Counts working days between two dates (inclusive).

Rules applied in order:
1. Skip Saturdays and Sundays.
2. Skip any `PublicHoliday` whose `is_recurring=False` and `date` falls in the range.
3. Skip any `PublicHoliday` whose `is_recurring=True` if its `(month, day)` matches the current date (year-agnostic).

```python
from apps.leave.services import WorkingDaysService
import datetime

days = WorkingDaysService.calculate_working_days(
    datetime.date(2025, 1, 6),   # Monday
    datetime.date(2025, 1, 10),  # Friday
)
# → 5  (or fewer if public holidays fall within the range)
```

#### `validate_leave_balance(employee, leave_type, year, requested_days) -> None`

Raises `rest_framework.exceptions.ValidationError` when:
- No `LeaveBalance` row exists for `(employee, leave_type, year)`.
- `balance.remaining_days < requested_days`.

Error message format: `"Insufficient leave balance. Available: {n}, Requested: {m}"`

#### `check_overlapping_leave(employee, start_date, end_date, exclude_id=None) -> None`

Raises `ValidationError` when the employee has an active leave request (status not in `REJECTED`, `CANCELLED`) whose date range overlaps with `[start_date, end_date]`.

Pass `exclude_id=request.pk` when editing an existing request to prevent it from conflicting with itself.

Overlap condition used in the DB query:
```
existing.start_date <= new.end_date  AND  existing.end_date >= new.start_date
```

#### `check_department_leave_overlap(employee, start_date, end_date, leave_type=None, exclude_id=None) -> None`

Raises `ValidationError` if any **other** employee in the same department already has an active **Annual or Casual** leave request (status not in `REJECTED`, `CANCELLED`) whose date range overlaps with the requested period.

This check runs only when the requested leave type is Annual or Casual. For Sick, Maternity, Paternity, and other types, the rule is skipped (multiple employees may be on leave simultaneously).

---

## Testing

Run the full test suite:

```bash
python manage.py test
```

Run only the service tests:

```bash
python manage.py test apps.leave.tests.test_services --verbosity=2
```

---

## Serializers

All leave serializers live in `apps/leave/serializers.py`.

### `LeaveTypeSerializer`

Read/write for `name`, `description`, `default_days`. `id`, `created_at`, `updated_at` are read-only. HR and admins can create, update, and delete leave types via the API.

### `LeaveBalanceSerializer`

Read-only. Includes a nested `LeaveTypeSerializer` and the computed `remaining_days` property.

### `LeaveRequestCreateSerializer` (write)

Used for `POST` (create) and `PATCH` (update) requests.

**Fields:** `leave_type`, `start_date`, `end_date`, `reason`, `is_emergency`, `cover_person`

**Validation pipeline (in `validate()`):**

1. `end_date` must be strictly after `start_date`.
2. `cover_person` must not be the requesting employee.
3. `cover_person` must be in the same department as the employee.
4. Maternity leave: only available for female staff. Paternity leave: only available for male staff.
5. `WorkingDaysService.check_overlapping_leave()` — rejects if the employee already has an active request in the same window. Passes `exclude_id` automatically on updates.
6. `WorkingDaysService.check_department_leave_overlap()` — for Annual and Casual only: rejects if any other employee in the same department has an active Annual or Casual request overlapping the same dates. Skipped for Sick, Maternity, Paternity, and other types.
7. `WorkingDaysService.validate_leave_balance()` — rejects if remaining balance for `(employee, leave_type, start_date.year)` is less than the computed working days.

**`create()` behaviour:**

- `total_working_days` is computed via `WorkingDaysService.calculate_working_days()`.
- `status` is always set to `DRAFT`.
- `employee` is taken from `request.user` via serializer context.

### `LeaveRequestReadSerializer` (read)

Full read representation. Includes:
- Nested `_EmployeeMinimalSerializer` for `employee` (`id`, `email`, `first_name`, `last_name`)
- Nested `_EmployeeMinimalSerializer` for `cover_person`
- Nested `LeaveTypeSerializer` for `leave_type`
- `status` (raw value) + `status_display` (human-readable label)
- `total_working_days`, `is_emergency`, `reason`, `created_at`, `updated_at`

### `LeaveApprovalLogSerializer`

Read-only. Includes nested `actor` (minimal employee fields), `action` + `action_display`, `previous_status`, `new_status`, `comment`, `timestamp`.

---

---

## Leave API Endpoints

All leave endpoints require a valid JWT `Authorization: Bearer <token>` header.

### Leave Types

| Method | Endpoint | Permission | Description |
|--------|----------|-----------|-------------|
| GET | `/api/v1/leave-types/` | Any authenticated | List all leave types |
| POST | `/api/v1/leave-types/` | HR or Admin | Create a leave type |
| GET | `/api/v1/leave-types/:id/` | Any authenticated | Retrieve one |
| PUT | `/api/v1/leave-types/:id/` | HR or Admin | Full update |
| PATCH | `/api/v1/leave-types/:id/` | HR or Admin | Partial update |
| DELETE | `/api/v1/leave-types/:id/` | HR or Admin | Delete a leave type |

### Leave Balances

| Method | Endpoint | Permission | Description |
|--------|----------|-----------|-------------|
| GET | `/api/v1/leave-balances/` | Authenticated | HR/Manager/ED see all; employees see own |
| GET | `/api/v1/leave-balances/:id/` | Authenticated | Retrieve one |
| GET | `/api/v1/leave-balances/?employee=<uuid>&year=<int>` | Privileged | Filter by employee and/or year |

### Leave Requests

| Method | Endpoint | Permission | Description |
|--------|----------|-----------|-------------|
| GET | `/api/v1/leave-requests/` | Authenticated | Role-filtered list |
| POST | `/api/v1/leave-requests/` | Authenticated | Create DRAFT request |
| GET | `/api/v1/leave-requests/:id/` | Owner / Manager / HR / ED | Retrieve one |
| PATCH | `/api/v1/leave-requests/:id/` | HR only | Modify a request |
| PUT | `/api/v1/leave-requests/:id/` | — | 405 — use PATCH |
| DELETE | `/api/v1/leave-requests/:id/` | — | 405 — use cancel action |

#### Custom actions

| Method | Endpoint | Permission | Description |
|--------|----------|-----------|-------------|
| POST | `/api/v1/leave-requests/:id/submit/` | Request owner | DRAFT → PENDING_MANAGER (requires dept line manager) |
| POST | `/api/v1/leave-requests/:id/approve/` | Role-matched approver | Stage transition (see table below) |
| POST | `/api/v1/leave-requests/:id/reject/` | Role-matched approver | Any pending stage → REJECTED (comment required) |
| POST | `/api/v1/leave-requests/:id/cancel/` | Owner (DRAFT/PENDING_MANAGER) or HR | → CANCELLED |
| GET | `/api/v1/leave-requests/:id/logs/` | HR / Manager / ED / owner | Full approval audit trail |

#### Approval stage transitions

| Current status | Required role | Next status |
|---|---|---|
| `PENDING_MANAGER` | `LINE_MANAGER` | `PENDING_HR` |
| `PENDING_HR` | `HR` | `PENDING_ED` |
| `PENDING_ED` | `EXECUTIVE_DIRECTOR` | `APPROVED` |

On final `APPROVED`:
- `LeaveBalance.used_days` is incremented by `total_working_days` using an atomic `F()` expression.
- The approved leave automatically appears on the department calendar (`GET /api/v1/calendar/`).
- A `LeaveApprovalLog` entry is created at every stage transition.

#### `reject` payload

```json
{ "comment": "Reason for rejection is required." }
```

#### `cancel` payload (optional)

```json
{ "comment": "Optional cancellation note." }
```

---

### Test coverage — `WorkingDaysService` (28 tests)

| Class | Tests |
|---|---|
| `CalculateWorkingDaysTests` | single weekday; single Saturday; single Sunday; full Mon–Fri week; Friday–Monday span; non-recurring holiday mid-range; recurring holiday mid-range; `start > end` → 0; two consecutive holidays; holiday on weekend not double-counted |
| `ValidateLeaveBalanceTests` | exact boundary passes; sufficient balance passes; insufficient balance raises with correct message; no balance record raises; wrong year raises |
| `CheckOverlappingLeaveTests` | no existing requests; adjacent before/after passes; REJECTED does not block; CANCELLED does not block; `exclude_id` skips own request; different employee does not block; exact same range raises; partial overlap at start/end raises; new range inside existing raises; new range contains existing raises; single-day overlap raises |
