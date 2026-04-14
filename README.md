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

## Docker

You can also run the backend in a Docker container using the provided `Dockerfile`.

### Build the image

```bash
docker build -t incel-hrm-backend .
```

### Run the web API

The container expects configuration via environment variables (database, Redis, secret key, etc.). At minimum you should provide:

- `SECRET_KEY`
- Database settings (for example, `DATABASE_URL` or the individual `DB_*` envs you use in `base.py`)
- `REDIS_URL` / `NOTIFICATIONS_REDIS_URL`
- `ALLOWED_HOSTS` (for example, `ALLOWED_HOSTS=localhost,127.0.0.1`)

Example with PostgreSQL and Redis running elsewhere:

```bash
docker run --rm -p 8000:8000 \
  -e SECRET_KEY="change-me" \
  -e DATABASE_URL="postgres://user:password@db-host:5432/incel_hrm" \
  -e REDIS_URL="redis://redis-host:6379/0" \
  -e NOTIFICATIONS_REDIS_URL="redis://redis-host:6379/0" \
  -e ALLOWED_HOSTS="localhost,127.0.0.1" \
  incel-hrm-backend
```

The image uses:

- `DJANGO_SETTINGS_MODULE=hrm_backend.settings.prod` by default.
- Gunicorn as the WSGI server, binding to `0.0.0.0:8000`.

### Run a Celery worker with the same image

You can reuse the same image to run a Celery worker by overriding the container command:

```bash
docker run --rm \
  -e SECRET_KEY="change-me" \
  -e DATABASE_URL="postgres://user:password@db-host:5432/incel_hrm" \
  -e REDIS_URL="redis://redis-host:6379/0" \
  -e NOTIFICATIONS_REDIS_URL="redis://redis-host:6379/0" \
  -e ALLOWED_HOSTS="localhost,127.0.0.1" \
  incel-hrm-backend \
  celery -A hrm_backend worker -l info
```

You can similarly start a Celery beat process:

```bash
docker run --rm \
  -e SECRET_KEY="change-me" \
  -e DATABASE_URL="postgres://user:password@db-host:5432/incel_hrm" \
  -e REDIS_URL="redis://redis-host:6379/0" \
  -e NOTIFICATIONS_REDIS_URL="redis://redis-host:6379/0" \
  -e ALLOWED_HOSTS="localhost,127.0.0.1" \
  incel-hrm-backend \
  celery -A hrm_backend beat -l info
```

## CI & GitHub Container Registry

This repository is configured with a GitHub Actions workflow that automatically builds the Docker image from the root `Dockerfile` and publishes it to **GitHub Container Registry (GHCR)** on every push.

- Image name: `ghcr.io/<OWNER>/incel-hrm-backend`
- Tags:
  - Branch name (for example, `main`, `feature-xyz`)
  - Commit SHA
  - `latest` on the default branch

Example pull:

```bash
docker pull ghcr.io/<OWNER>/incel-hrm-backend:latest
```

Replace `<OWNER>` with your GitHub account or organization name.

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

## Background Jobs (Celery + Redis)

This backend uses Celery for asynchronous work (email notifications, long-running tasks, etc.).

### Redis configuration

Celery reads both the broker and result backend from `REDIS_URL` (configured in `hrm_backend/settings/base.py`):

```bash
export REDIS_URL="redis://localhost:6379/0"
```

### Running a worker locally

In one terminal (with your virtualenv activated), run:

```bash
celery -A hrm_backend worker -l info
```

### Email in development

In development settings (`hrm_backend/settings/dev.py`), email is printed to the console via:

- `EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"`

### Email (Google Workspace SMTP relay)

In production (and optionally in development), this backend can send email through **Google Workspace SMTP relay** using Django’s SMTP backend.

Configure via environment variables (see `.env.example`):

- `EMAIL_BACKEND` (default): `django.core.mail.backends.smtp.EmailBackend`
- `EMAIL_HOST` (default): `smtp-relay.gmail.com`
- `EMAIL_PORT` (default): `587`
- `EMAIL_USE_TLS` (default): `True`
- `EMAIL_USE_SSL` (default): `False`
- `DEFAULT_FROM_EMAIL` (recommended): `no-reply@yourdomain.com`

Relay authentication modes:

- **IP allowlisting (no username/password)**: leave `EMAIL_HOST_USER` and `EMAIL_HOST_PASSWORD` empty.
- **SMTP auth required**: set `EMAIL_HOST_USER` and `EMAIL_HOST_PASSWORD`.

Email deep links:

- Set `FRONTEND_BASE_URL` (e.g. `https://hrm.yourdomain.com`) so emails can link to request details like `/leave/requests/<id>`.

### Leave workflow notifications

Leave requests trigger Celery tasks in `apps/leave/tasks.py` on:

- submit: notifies the next approver
- approve: notifies the next approver, and notifies the requester when finally approved
- reject: notifies the requester with the rejection comment

## In-app Notifications (DB + SSE)

The backend also supports **in-app notifications** for the web UI:

- Notifications are **stored in the database** (`apps/notifications/models.py`).
- Real-time updates are delivered over **Server-Sent Events (SSE)**, backed by **Redis Pub/Sub**.

### REST endpoints

All endpoints are under `/api/v1/notifications/`:

- `GET /api/v1/notifications/` — list current user's notifications
- `GET /api/v1/notifications/unread-count/` — returns `{ "count": <int> }`
- `POST /api/v1/notifications/<id>/mark-read/` — mark one notification read
- `POST /api/v1/notifications/mark-all-read/` — mark all read

### SSE stream

- `GET /api/v1/notifications/stream/`
- The server emits events:
  - `ready` (initial)
  - `keepalive` (periodic)
  - `notification` (payload JSON)

### Redis configuration

The SSE system uses `NOTIFICATIONS_REDIS_URL`, which defaults to `REDIS_URL` in `hrm_backend/settings/base.py`.

### Leave notifications

When leave requests are submitted/approved/rejected, the Celery tasks in `apps/leave/tasks.py` now:

- send email (as before)
- create an in-app `Notification` record
- publish an SSE event to any active sessions for the recipient

## Running tests

By default, running with `hrm_backend.settings.dev` will use Postgres and may require database permissions to create a test database.

To run tests using the shared `base` settings (which switches to SQLite automatically for `manage.py test`), run:

```bash
DJANGO_SETTINGS_MODULE=hrm_backend.settings.base python manage.py test
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

| Method | Endpoint                      | Auth required | Description                        |
|--------|-------------------------------|---------------|------------------------------------|
| POST   | `/api/v1/auth/register/`      | No            | Create a new user account          |
| POST   | `/api/v1/auth/login/`         | No            | Obtain JWT access + refresh tokens |
| POST   | `/api/v1/auth/token/refresh/` | No            | Refresh an access token            |
| GET    | `/api/v1/auth/me/`            | Yes (JWT)     | Return the current user's profile (read-only) |
| GET    | `/api/v1/auth/profile/`       | Yes (JWT)     | Get current user's profile (detailed) |
| PATCH  | `/api/v1/auth/profile/`       | Yes (JWT)     | Update own basic profile fields    |

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

# Fetch own profile (read-only summary)
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

| Role                  | Description                                                      |
|-----------------------|------------------------------------------------------------------|
| `EMPLOYEE`            | Default role for all staff members; auto-assigned on registration |
| `LINE_MANAGER`        | Head of one department; first approver in the leave chain       |
| `SUPERVISOR`          | Supervisor of a Unit within a department; first approver for unit members |
| `HR`                  | Human Resources — manages employee records                      |
| `EXECUTIVE_DIRECTOR`  | Elevated approval rights                                        |
| `MANAGING_DIRECTOR`   | Highest-level access                                            |

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
    IsSupervisor,
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

### User profile (self-service)

For employees to manage their own profile:

| Method | Endpoint                 | Permission   | Description                                  |
|--------|--------------------------|--------------|----------------------------------------------|
| GET    | `/api/v1/auth/profile/` | Authenticated| Get current user's full profile              |
| PATCH  | `/api/v1/auth/profile/` | Authenticated| Update own `first_name`, `last_name`, `phone`, `gender`, `date_of_birth` |

Role, department, and unit assignment remain restricted to HR and Line Managers via:

- `PATCH /api/v1/users/:id/` — change department/unit (HR/Line Manager UI).
- `POST /api/v1/users/:id/roles/` — assign a new role (overwrites any existing role).

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

### Unit model

Units sit under departments and optionally have a dedicated supervisor.

| Field        | Type                 | Notes                                          |
|--------------|----------------------|-----------------------------------------------|
| `id`         | UUID                 | Primary key, auto-generated                    |
| `name`       | CharField            | Max 150 chars; unique per department           |
| `department` | FK → Department      | The parent department                          |
| `supervisor` | OneToOneField → User | Optional. Each Unit has at most one supervisor |
| `created_at` | DateTimeField        | Auto-set on creation                           |
| `updated_at` | DateTimeField        | Auto-updated on save                           |

Each `User` can also optionally belong to a `Unit` (via `User.unit`), which must be in the same `department` as the user.

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
| GET | `/api/v1/departments/:id/detail/` | Own dept or HR/ED/MD | Department + members + units + line manager |
| POST | `/api/v1/departments/:id/bulk-add-members/` | HR or Admin | Bulk-add users to a department (partial success) |
| POST | `/api/v1/departments/:id/bulk-remove-members/` | HR or Admin | Bulk-remove users from a department (partial success) |

### Unit endpoints

| Method | Endpoint                         | Permission                      | Description                              |
|--------|----------------------------------|----------------------------------|------------------------------------------|
| GET    | `/api/v1/units/?department=:id` | Dept member or HR/ED/MD         | List units in a department               |
| POST   | `/api/v1/units/`                | LINE_MANAGER of that department | Create a unit                            |
| GET    | `/api/v1/units/:id/`            | Dept member or HR/ED/MD         | Unit detail: name, supervisor, members   |
| PATCH  | `/api/v1/units/:id/`            | LINE_MANAGER of unit's dept     | Update unit (e.g. name)                  |
| DELETE | `/api/v1/units/:id/`            | LINE_MANAGER of unit's dept     | Delete unit (or restrict if it has members) |
| POST   | `/api/v1/units/:id/bulk-add-members/` | HR/ED/MD/Admin or LINE_MANAGER | Bulk-add users to a unit (partial success) |
| POST   | `/api/v1/units/:id/bulk-remove-members/` | HR/ED/MD/Admin or LINE_MANAGER | Bulk-remove users from a unit (partial success) |

### Team endpoints

| Method | Endpoint | Permission | Description |
|--------|----------|------------|-------------|
| GET | `/api/v1/teams/?unit=:id` | Unit/Dept member or HR/ED/MD | List teams in a unit |
| POST | `/api/v1/teams/` | HR/ED/MD/Admin or LINE_MANAGER | Create a team in a unit |
| GET | `/api/v1/teams/:id/` | Unit/Dept member or HR/ED/MD | Team detail |
| PATCH | `/api/v1/teams/:id/` | HR/ED/MD/Admin or LINE_MANAGER | Update a team |
| DELETE | `/api/v1/teams/:id/` | HR/ED/MD/Admin or LINE_MANAGER | Delete a team |
| POST | `/api/v1/teams/:id/bulk-add-members/` | HR/ED/MD/Admin or LINE_MANAGER | Bulk-add users to a team (partial success) |
| POST | `/api/v1/teams/:id/bulk-remove-members/` | HR/ED/MD/Admin or LINE_MANAGER | Bulk-remove users from a team (partial success) |

### Bulk add payload (Department / Unit / Team)

All bulk-add endpoints accept the same request shape.

```json
{
  "user_ids": ["<uuid>", "<uuid>"],
  "dry_run": false,
  "clear_conflicts": false
}
```

Notes:
- `dry_run=true` validates and returns which user IDs would succeed/fail without writing.
- `clear_conflicts=true` is supported for Department and Unit bulk-add:
  - Department bulk-add can clear conflicting `unit`/`team` when moving a user to the new department.
  - Unit bulk-add can move a user into the unit’s department and clear a conflicting team.
- Team bulk-add enforces that each user already belongs to the same unit as the team (no auto-move).

### Bulk add response (partial success)

```json
{
  "target": { "department_id": "<uuid>" },
  "succeeded_user_ids": ["<uuid>"],
  "failed": [
    { "user_id": "<uuid>", "code": "not_found", "error": "User not found." }
  ]
}
```


**Unit rules:**

- Units belong to a single department.
- Only the department’s Line Manager can create/update/delete units in that department.
- A supervisor may be assigned to a unit (via `Unit.supervisor` and the `SUPERVISOR` role), and employees in that unit will route leave requests through the unit’s supervisor first.

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

### Frontend integration summary

- **User Profile UI**
  - Use `GET /api/v1/auth/me/` for a quick summary (e.g. header/avatar).
  - Use `GET /api/v1/auth/profile/` to populate a full profile form.
  - On save, send `PATCH /api/v1/auth/profile/` with any subset of: `first_name`, `last_name`, `phone`, `gender`, `date_of_birth`.
  - For HR/Line Manager admin panels:
    - Use `PATCH /api/v1/users/:id/` to change `department` and `unit`.
    - Use `POST /api/v1/users/:id/roles/` to change a user's role.

- **Department Detail UI**
  - Use `GET /api/v1/departments/:id/detail/` to build a department page that shows:
    - Department summary (`department` object — name, description, line manager).
    - `members`: list of users in that department.
    - `units`: list of units with their supervisors and members.
  - EMPLOYEES can only view their own department; HR/ED/MD/staff can navigate to any.

- **Unit Detail UI**
  - Use `GET /api/v1/units/:id/` to render a unit detail page:
    - Core info: `id`, `name`, `department`, `supervisor`.
    - `members`: list of users in that unit.
  - LINE_MANAGER/HR workflows can reuse:
    - `GET /api/v1/units/?department=<dept_id>` to show all units in a department.
    - `POST /api/v1/units/`, `PATCH/DELETE /api/v1/units/:id/` to manage units.

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

##### API (for UI highlighting)

- `GET /api/v1/public-holidays/` — list public holidays (authenticated)
  - Optional: `?year=2026` (includes recurring holidays + non-recurring holidays in that year)

##### HR CSV upload

- `POST /api/v1/public-holidays/upload/` — bulk upsert holidays by date (HR/admin only)\n\nCSV format:\n\n```csv\nname,date\nNew Year’s Day,2026-01-01\nWorkers’ Day,2026-05-01\n```\n\nExample:\n\n```bash\ncurl -X POST \"http://localhost:8000/api/v1/public-holidays/upload/\" \\\n  -H \"Authorization: Bearer <access_token>\" \\\n  -F \"file=@public-holidays-2026.csv\"\n```\n+
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
| `cover_person`      | FK → User. Optional. If provided, must be in the same department and cannot be the applicant |
| `total_working_days`| Computed automatically on `save()` (excludes weekends + public holidays) |
| `is_emergency`      | Flag for urgent requests                                    |
| `status`            | See workflow below                                          |

#### Leave request workflow

```
DRAFT
  → PENDING_TEAM_LEAD (if employee is in a team with a team lead)
  → PENDING_SUPERVISOR (if employee is in a unit with a supervisor)
  → PENDING_MANAGER
  → PENDING_HR
  → PENDING_ED
  → APPROVED
                         ↘ REJECTED
         (any stage) ─────────────────────→ CANCELLED
```

| Status               | Meaning                                                  |
|----------------------|----------------------------------------------------------|
| `DRAFT`              | Saved but not yet submitted                              |
| `PENDING_TEAM_LEAD`  | Awaiting approval from the employee’s Team Lead          |
| `PENDING_SUPERVISOR` | Awaiting approval from the employee’s Unit supervisor    |
| `PENDING_MANAGER`    | Awaiting Line Manager approval                           |
| `PENDING_HR`         | Awaiting HR approval                                     |
| `PENDING_ED`         | Awaiting Executive Director approval                     |
| `APPROVED`           | Fully approved                                           |
| `REJECTED`           | Declined at any stage                                    |
| `CANCELLED`          | Withdrawn by the employee or cancelled by HR             |

#### `LeaveApprovalLog`

Immutable audit trail. One entry is appended per status transition. Fields include `actor`, `action` (`APPROVE / REJECT / CANCEL / MODIFY`), `previous_status`, `new_status`, `comment`, and `timestamp`.

### Business rules

1. **One Line Manager per department** -- Each department has at most one line manager (`Department.line_manager`). A user can manage at most one department (OneToOneField). HR and ED can assign/revoke via `POST/DELETE /api/v1/departments/:id/line-manager/`.

2. **Cover person optional** -- When creating a leave request, `cover_person` can be omitted (for example, when no suitable cover exists). If provided, the cover person must be another active user in the same department and cannot be the requesting employee.

3. **Maternity and Paternity by gender** -- Maternity leave is only available for female staff; Paternity leave is only available for male staff. Leave balances for these types are created only for eligible users.

4. **Default leave balances** -- On user creation, `LeaveBalance` rows are auto-created for the current year for each eligible leave type, with `allocated_days` = `leave_type.default_days`.

5. **Org-scoped leave exclusivity (Annual & Casual only)** -- For Annual and Casual leave only, at most one employee in the *lowest available org scope* may have an active leave request overlapping any given date range:

   - If the department has **teams**, exclusivity is enforced at the **team** level.
   - Else if the department has **units**, exclusivity is enforced at the **unit** level.
   - Else exclusivity is enforced at the **department** level.

   Sick, Maternity, Paternity, and other leave types are excluded; multiple employees may be on those types simultaneously. If another colleague's Annual or Casual leave already covers the requested dates within the applicable scope, the request is rejected at validation time.

6. **Submit requires a line manager** -- An employee cannot submit a DRAFT request (`POST .../submit/`) unless their department has a line manager assigned. This ensures the approval chain is complete before a request enters the pipeline.

7. **Approval chain** -- When a request is submitted, it flows through: Team Lead (if applicable) → Unit Supervisor (if applicable) → department Line Manager → HR → Executive Director. Certain requester roles may route through special chains.\n+\n+   - **LINE_MANAGER applicant**: Management Department Line Manager (ED) → HR → ED (final)\n+   - **HR applicant**: Department Line Manager → ED (skips HR stage)\n*** End Patch}"}]}Commentary to=functions.ApplyPatch  天天中彩票未form code with 279 more bytes to show code>

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

---

## Performance Testing with k6

This project includes k6 scripts for load and stress testing the API.

### Folder layout

- `k6/config.js` – shared configuration (base URL, thresholds, stage presets, common helpers).
- `k6/utils/auth.js` – helpers for obtaining JWTs for seeded users (employee, HR, manager, ED).
- `k6/utils/data.js` – small data factories (emails, phones, DOBs, department/unit IDs from env).
- `k6/scenarios/` – domain-specific flows:
  - `auth_scenarios.js` – login, register-then-login, profile update.
  - `accounts_scenarios.js` – HR user list/CRUD and role assignment flows.
  - `departments_units_scenarios.js` – department browse/detail, unit listing, basic admin flows.
  - `leave_scenarios.js` – leave browse, draft+submit, cancel, and full approval chain.
  - `calendar_scenarios.js` – employee and HR calendar views.
- `k6/tests/` – test entrypoints:
  - `smoke.test.js` – fast sanity check across core flows.
  - `load.test.js` – typical business-hours load.
  - `stress.test.js` – push system towards upper capacity bounds.
  - `spike.test.js` – sudden burst traffic behaviour.
  - `soak.test.js` – optional long-running endurance test.
- `k6/report.js` – `handleSummary` that emits `k6-summary.html` and `k6-summary.json`.

### Required environment

Before running k6 tests, export:

```bash
export K6_BASE_URL="http://localhost:8000"

# Seeded users for role-based flows
export K6_EMPLOYEE_EMAIL="employee@example.com"
export K6_EMPLOYEE_PASSWORD="..."
export K6_HR_EMAIL="hr@example.com"
export K6_HR_PASSWORD="..."
export K6_MANAGER_EMAIL="manager@example.com"
export K6_MANAGER_PASSWORD="..."
export K6_ED_EMAIL="ed@example.com"
export K6_ED_PASSWORD="..."

# Organisation and leave context used in some scenarios
export K6_DEPT_ID="<department_uuid>"
export K6_UNIT_ID="<unit_uuid_optional>"
export K6_ROLE_TARGET_USER_ID="<user_uuid_optional>"
export K6_ROLE_ID="<role_uuid_optional>"
export K6_LEAVE_TYPE_ID="<leave_type_uuid>"
export K6_COVER_PERSON_ID="<user_uuid_in_same_department>"
export K6_LEAVE_START_DATE="2025-01-06"
export K6_LEAVE_END_DATE="2025-01-10"
```

These should point to real objects in your database. For CI/staging, seed a small dataset and wire these values via secrets.

### Running k6 tests

From the project root:

```bash
# Smoke test (quick sanity check)
k6 run k6/tests/smoke.test.js

# Load test
k6 run k6/tests/load.test.js

# Stress test
k6 run k6/tests/stress.test.js

# Spike test
k6 run k6/tests/spike.test.js

# Soak test (long running; customise via env)
K6_SOAK_VUS=15 K6_SOAK_DURATION=2h k6 run k6/tests/soak.test.js
```

All runs will also produce `k6-summary.html` and `k6-summary.json` in the working directory for quick inspection.

### CI/CD integration (example)

You can wire the smoke test into GitHub Actions using the official k6 action:

```yaml
- name: Run k6 smoke test
  uses: grafana/k6-action@v0.3.1
  with:
    filename: k6/tests/smoke.test.js
  env:
    K6_BASE_URL: ${{ secrets.K6_BASE_URL }}
    K6_EMPLOYEE_EMAIL: ${{ secrets.K6_EMPLOYEE_EMAIL }}
    K6_EMPLOYEE_PASSWORD: ${{ secrets.K6_EMPLOYEE_PASSWORD }}
    # ...other K6_* env vars...
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
2. If provided, `cover_person` must not be the requesting employee.
3. If provided, `cover_person` must be in the same department as the employee.
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
| GET | `/api/v1/leave-balances/` | Authenticated | List balances for the authenticated user only |
| GET | `/api/v1/leave-balances/:id/` | Authenticated | Retrieve a single balance (must belong to the authenticated user) |

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
| POST | `/api/v1/leave-requests/:id/submit/` | Request owner | Submit a draft into the approval workflow (requires dept line manager) |
| POST | `/api/v1/leave-requests/create-and-submit/` | Request owner | Create a new request and immediately submit it |
| POST | `/api/v1/leave-requests/:id/approve/` | Role-matched approver | Stage transition (see table below) |
| POST | `/api/v1/leave-requests/:id/reject/` | Role-matched approver | Any pending stage → REJECTED (comment required) |
| POST | `/api/v1/leave-requests/:id/cancel/` | Owner (DRAFT/early pending) or HR | → CANCELLED |
| GET | `/api/v1/leave-requests/:id/logs/` | HR / Manager / ED / owner | Full approval audit trail |

#### Approval stage transitions

| Current status | Required role | Next status |
|---|---|---|
| `PENDING_TEAM_LEAD` | `TEAM_LEAD` | `PENDING_SUPERVISOR` |
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
