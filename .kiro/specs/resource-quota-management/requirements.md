# Requirements Document

## Introduction

This document formalises the requirements for the **Resource Quota Management** feature added to the Charmed Kubeflow Admin Portal. The feature enables administrators to define reusable ResourceQuota Groups (named templates specifying CPU, memory, storage, and GPU limits), assign groups to users, and have the portal automatically create, update, or delete Kubernetes `ResourceQuota` objects on each user's namespace. The implementation also refactors the monolithic `app.py` into a modular `app/` package and extends the existing SQLite schema additively.

All existing functionality (user provisioning, GitLab integration, health checks, MinIO secret replication) must remain unchanged.

---

## Glossary

- **Admin_Portal**: The Charmed Kubeflow Admin Portal FastAPI application.
- **Quota_Group**: A named, reusable set of resource limits (CPU, memory, storage, GPU) stored in the `resource_quota_groups` table.
- **Quota_Service**: The `app/quota_service.py` module that implements Quota_Group CRUD and synchronisation logic.
- **Kubernetes_Service**: The `app/kubernetes_service.py` module that manages `ResourceQuota` objects via the official Kubernetes Python client.
- **Database**: The SQLite database stored at `/var/lib/portal-data/portal.db`, managed by `app/database.py`.
- **ResourceQuota**: A Kubernetes `v1/ResourceQuota` object named `compute-quota` created in each user's namespace.
- **Sync_Operation**: The full cluster-wide reconciliation performed by `POST /api/sync/kubernetes` that aligns all `ResourceQuota` objects with the current database state.
- **Quota_Sync_Status**: A per-user field reflecting the current synchronisation state; one of `NO_QUOTA`, `SYNCED`, `DRIFTED`, or `ERROR`.
- **Validator**: The Pydantic model layer in `app/models.py` that enforces resource limit formats.
- **Dashboard**: The `GET /api/dashboard/quota-stats` endpoint and its frontend page that display aggregate quota statistics.
- **NNGi_Format**: A string matching the regular expression `^\d+Gi$` (e.g., `"64Gi"`, `"500Gi"`).
- **CPU_Format**: A string matching the regular expression `^\d+(\.\d+)?$` (e.g., `"16"`, `"1.5"`).
- **GPU_Format**: A string matching the regular expression `^\d+$` (e.g., `"2"`, `"0"`).

---

## Requirements

### Requirement 1: Modular Application Refactoring

**User Story:** As a developer, I want the monolithic `app.py` to be refactored into a modular `app/` package, so that the codebase is maintainable, testable, and extensible without breaking any existing functionality.

#### Acceptance Criteria

1. THE Admin_Portal SHALL be structured as an `app/` package containing `main.py`, `database.py`, `models.py`, `quota_service.py`, `kubernetes_service.py`, `gitlab_service.py`, and a `routes/` sub-package with `users.py`, `quotas.py`, `health.py`, and `dashboard.py`.
2. WHEN the Admin_Portal starts up, THE Admin_Portal SHALL register all routers and mount static files from a single `main.py` entry point, and SHALL attempt to initialise the Database; IF Database initialisation fails, THEN THE Admin_Portal SHALL log the error and continue running in a degraded state so that other routers remain available.
3. THE Admin_Portal SHALL preserve all existing API endpoints (`GET /api/users`, `POST /api/users`, `DELETE /api/users/{gitlab_id}/{username}`, `GET /api/health`) with their original request and response shapes; new fields in responses are additive only.
4. WHEN existing endpoints are called after refactoring, THE Admin_Portal SHALL return responses that are structurally identical to responses from the original `app.py` implementation.

---

### Requirement 2: Additive SQLite Schema Migration

**User Story:** As a system operator, I want the database schema to be extended additively without data loss, so that existing user records remain intact after the upgrade.

#### Acceptance Criteria

1. WHEN `init_db()` runs on a database that already contains the `users` table, THE Database SHALL detect columns already present via `PRAGMA table_info` and skip the corresponding `ALTER TABLE` migration step.
2. WHEN `init_db()` runs on a fresh database, THE Database SHALL create the `users` table and the `resource_quota_groups` table; THE Database SHALL then add the `quota_group_id` and `quota_sync_status` columns to `users` using the same `ALTER TABLE` migration logic applied to existing databases.
3. THE Database SHALL create the `resource_quota_groups` table with columns: `id` (INTEGER PRIMARY KEY AUTOINCREMENT), `name` (TEXT UNIQUE NOT NULL), `description` (TEXT), `cpu_limit` (TEXT), `memory_limit` (TEXT), `storage_limit` (TEXT), `gpu_limit` (TEXT), `created_at` (TIMESTAMP DEFAULT CURRENT_TIMESTAMP), `updated_at` (TIMESTAMP DEFAULT CURRENT_TIMESTAMP).
4. THE Database SHALL add a `quota_group_id` column (INTEGER, nullable, foreign key referencing `resource_quota_groups(id)`) to the `users` table via an idempotent `ALTER TABLE` migration.
5. THE Database SHALL add a `quota_sync_status` column (TEXT DEFAULT `'NO_QUOTA'`) to the `users` table via an idempotent `ALTER TABLE` migration.
6. IF a migration step for a column that already exists is triggered, THEN THE Database SHALL perform a no-op for that step and leave existing data unchanged.

---

### Requirement 3: Resource Quota Group CRUD

**User Story:** As an administrator, I want to create, list, edit, duplicate, and delete ResourceQuota Groups, so that I can manage reusable quota templates for my users.

#### Acceptance Criteria

1. WHEN an admin sends `POST /api/quota-groups` with a valid `QuotaGroupCreate` payload, THE Quota_Service SHALL insert a new row into `resource_quota_groups` and return a `QuotaGroupResponse` with HTTP 200.
2. WHEN an admin sends `GET /api/quota-groups`, THE Admin_Portal SHALL return the list of all `QuotaGroupResponse` objects, each including the count of users currently assigned to that group in an `assigned_users` field.
3. WHEN an admin sends `PUT /api/quota-groups/{id}` with a valid `QuotaGroupUpdate` payload, THE Quota_Service SHALL update the corresponding `resource_quota_groups` row, refresh `updated_at`, trigger a re-sync for all users assigned to that group, and return the updated `QuotaGroupResponse`.
4. WHEN an admin sends `DELETE /api/quota-groups/{id}` and no users are currently assigned to that group, THE Quota_Service SHALL delete the row and return `{"deleted": id}`.
5. WHEN an admin sends `DELETE /api/quota-groups/{id}` and one or more users are currently assigned to that group, THE Quota_Service SHALL block the deletion and return HTTP 400 with a detail message stating the count of currently assigned users.
6. WHEN an admin sends `POST /api/quota-groups/{id}/duplicate`, THE Quota_Service SHALL create a new `resource_quota_groups` row with the same field values and a name suffixed with `" (copy)"`, then return the new `QuotaGroupResponse`.
7. WHEN an admin sends any quota group endpoint for a non-existent `{id}`, THE Quota_Service SHALL return HTTP 404.

---

### Requirement 4: Resource Limit Validation

**User Story:** As an administrator, I want the portal to validate all resource limit values before saving them, so that only well-formed values are sent to the Kubernetes API.

#### Acceptance Criteria

1. WHEN a `QuotaGroupCreate` or `QuotaGroupUpdate` request contains a `memory_limit` value that does not match NNGi_Format, THE Validator SHALL reject the request with HTTP 422.
2. WHEN a `QuotaGroupCreate` or `QuotaGroupUpdate` request contains a `storage_limit` value that does not match NNGi_Format, THE Validator SHALL reject the request with HTTP 422.
3. WHEN a `QuotaGroupCreate` or `QuotaGroupUpdate` request contains a `cpu_limit` value that does not match CPU_Format, THE Validator SHALL reject the request with HTTP 422.
4. WHEN a `QuotaGroupCreate` or `QuotaGroupUpdate` request contains a `gpu_limit` value that does not match GPU_Format, THE Validator SHALL reject the request with HTTP 422.
5. WHEN a `QuotaGroupCreate` or `QuotaGroupUpdate` request contains a `memory_limit`, `storage_limit`, `cpu_limit`, or `gpu_limit` that is `null` or an empty string, THE Validator SHALL treat the field as absent (no limit applied for that resource dimension).
6. WHEN all provided limit fields pass format validation, THE Validator SHALL accept the request and pass the model to the Quota_Service.

---

### Requirement 5: User Quota Group Assignment

**User Story:** As an administrator, I want to assign or remove a ResourceQuota Group from a user, so that I can control each user's resource entitlements at any time.

#### Acceptance Criteria

1. WHEN an admin sends `POST /api/users` with a `quota_group_id` field, THE Admin_Portal SHALL store the `quota_group_id` on the new user record and call `apply_quota_for_user` as a background task after the Kubeflow Profile is created.
2. WHEN an admin sends `POST /api/users` without a `quota_group_id` (or with `quota_group_id: null`), THE Admin_Portal SHALL create the user with `quota_group_id = NULL` and `quota_sync_status = 'NO_QUOTA'`, and SHALL NOT create any ResourceQuota for that user.
3. WHEN an admin sends `PUT /api/users/{username}/quota` with a non-null `quota_group_id`, THE Quota_Service SHALL update the user's `quota_group_id`, call `upsert_resource_quota` for that user's namespace, and set `quota_sync_status` to `'SYNCED'` on success or `'ERROR'` on Kubernetes failure; THE Quota_Service SHALL trigger this operation only when an admin explicitly makes the PUT request.
4. WHEN an admin sends `PUT /api/users/{username}/quota` with `quota_group_id: null`, THE Quota_Service SHALL set the user's `quota_group_id` to NULL, call `delete_resource_quota_if_exists` for that user's namespace, and set `quota_sync_status` to `'NO_QUOTA'`.
5. WHEN `GET /api/users` is called, THE Admin_Portal SHALL include `quota_group_id`, `quota_group_name`, and `quota_sync_status` fields in each user object in the response.

---

### Requirement 6: Kubernetes ResourceQuota Lifecycle

**User Story:** As an administrator, I want the portal to automatically create, update, and delete Kubernetes ResourceQuota objects, so that namespace resource limits always reflect the assigned quota group.

#### Acceptance Criteria

1. WHEN `upsert_resource_quota(username, quota_spec)` is called and no `ResourceQuota` named `compute-quota` exists in namespace `username`, THE Kubernetes_Service SHALL create the ResourceQuota using `create_namespaced_resource_quota()` with `spec.hard` set to the values in `quota_spec`; IF a ResourceQuota already exists, THEN THE Kubernetes_Service SHALL skip creation and proceed to the patch step in AC2 instead.
2. WHEN `upsert_resource_quota(username, quota_spec)` is called and a `ResourceQuota` named `compute-quota` already exists in namespace `username`, THE Kubernetes_Service SHALL patch it using `patch_namespaced_resource_quota()` so that `spec.hard` matches `quota_spec`.
3. WHEN `delete_resource_quota_if_exists(username)` is called and a `ResourceQuota` named `compute-quota` exists in namespace `username`, THE Kubernetes_Service SHALL delete it using `delete_namespaced_resource_quota()`.
4. WHEN `delete_resource_quota_if_exists(username)` is called and no `ResourceQuota` named `compute-quota` exists in namespace `username`, THE Kubernetes_Service SHALL perform a no-op.
5. WHEN building a `quota_spec` from a `QuotaGroupResponse`, THE Quota_Service SHALL strictly exclude `"limits.cpu"` when `cpu_limit` is null or empty, strictly exclude `"limits.memory"` when `memory_limit` is null or empty, strictly exclude `"requests.storage"` when `storage_limit` is null or empty, and strictly exclude `"nvidia.com/gpu"` when `gpu_limit` is null or empty.
6. WHEN `apply_quota_for_user` is invoked, THE Quota_Service SHALL wait for the user's namespace to exist (polling up to 30 seconds) before applying the ResourceQuota; IF the namespace does not appear within the timeout, THEN THE Quota_Service SHALL log a warning and set `quota_sync_status` to `'ERROR'`; WHEN the namespace appears within the timeout and quota application succeeds, THE Quota_Service SHALL set `quota_sync_status` to `'SYNCED'`.
7. WHEN the Kubernetes API is unreachable during an active quota create, update, or delete operation, THE Kubernetes_Service SHALL raise an exception that causes the caller to return HTTP 503 and roll back any in-progress database changes.

---

### Requirement 7: Full Idempotent Kubernetes Sync

**User Story:** As an administrator, I want a full sync operation that reconciles all Kubernetes ResourceQuota objects with the database, so that I can correct any drift at any time.

#### Acceptance Criteria

1. WHEN an admin sends `POST /api/sync/kubernetes`, THE Quota_Service SHALL iterate over all users and apply the correct quota state for each.
2. WHILE iterating users during sync, THE Quota_Service SHALL apply `upsert_resource_quota` for users with a non-null `quota_group_id` and `delete_resource_quota_if_exists` for users with `quota_group_id = NULL`.
3. WHEN a user's namespace does not exist during sync, THE Quota_Service SHALL log a warning, increment the `skipped` counter, and continue processing remaining users without aborting.
4. WHEN the sync completes, THE Admin_Portal SHALL return a `SyncResult` JSON object containing `synced` (count of successful quota applications), `skipped` (count of missing namespaces), and `errors` (list of per-user error messages).
5. WHEN `POST /api/sync/kubernetes` is called N times (N ≥ 1) on an already-correct cluster state, THE Quota_Service SHALL produce the same Kubernetes state as calling it once, with no errors for successfully synced users.
6. WHEN a per-user sync step succeeds, THE Quota_Service SHALL set that user's `quota_sync_status` to `'SYNCED'`.
7. WHEN a per-user sync step fails due to a Kubernetes error, THE Quota_Service SHALL set that user's `quota_sync_status` to `'ERROR'`, append an error message to the `errors` list, and continue with the next user.

---

### Requirement 8: Quota Sync Status Tracking

**User Story:** As an administrator, I want to see the synchronisation status of each user's quota, so that I can identify and resolve any discrepancies between the database and the cluster.

#### Acceptance Criteria

1. THE Admin_Portal SHALL maintain a `quota_sync_status` value for every user in the Database, which MUST be one of: `'NO_QUOTA'`, `'SYNCED'`, `'DRIFTED'`, or `'ERROR'`.
2. WHEN a user has `quota_group_id = NULL`, THE Database SHALL store `quota_sync_status = 'NO_QUOTA'` for that user.
3. WHEN a quota is successfully applied to a user's namespace, THE Quota_Service SHALL update that user's `quota_sync_status` to `'SYNCED'` in the Database.
4. WHEN a quota operation fails for a user, THE Quota_Service SHALL update that user's `quota_sync_status` to `'ERROR'` in the Database.
5. WHEN `GET /api/users` is called, THE Admin_Portal SHALL return the current `quota_sync_status` for each user in the response.

---

### Requirement 9: Quota Dashboard

**User Story:** As an administrator, I want a dashboard showing aggregate quota statistics and charts, so that I can understand the overall resource allocation across the cluster.

#### Acceptance Criteria

1. WHEN an admin sends `GET /api/dashboard/quota-stats`, THE Admin_Portal SHALL return a `QuotaStats` response containing: `total_quota_groups`, `users_with_quotas`, `unlimited_users`, `total_cpu_cores`, `total_memory_gi`, `total_gpu_count`, `users_per_group`, `cpu_by_group`, `memory_by_group`, and `gpu_by_group`.
2. WHEN computing dashboard statistics, THE Admin_Portal SHALL derive all values from a single aggregating SQL query against the Database, rather than multiple round-trips.
3. WHEN a user has `quota_group_id = NULL`, THE Admin_Portal SHALL count that user in `unlimited_users` and SHALL NOT include that user's resources in aggregate CPU, memory, or GPU totals.
4. WHEN a user has a non-null `quota_group_id`, THE Admin_Portal SHALL count that user in `users_with_quotas` and include their group's limits in the per-group breakdown lists.

---

### Requirement 10: Error Handling

**User Story:** As an administrator, I want the portal to handle infrastructure failures gracefully, so that partial failures do not leave the system in an inconsistent state.

#### Acceptance Criteria

1. WHEN the Kubernetes API is unreachable during an active quota create, update, or delete operation, THE Admin_Portal SHALL return HTTP 503 to the caller.
2. WHEN a Database write fails, THE Admin_Portal SHALL return HTTP 500 to the caller; THE Admin_Portal SHALL attempt to log the error, but SHALL return HTTP 500 regardless of whether the logging succeeds.
3. WHEN a namespace does not exist at the time a quota operation is attempted during a full sync, THE Quota_Service SHALL log a warning and skip that user without failing the entire sync operation.
4. IF a `ResourceQuota` named `compute-quota` already exists in a namespace, THEN THE Kubernetes_Service SHALL patch it rather than raising an error.
5. WHEN an admin attempts to operate on a Quota_Group `{id}` that does not exist, THE Admin_Portal SHALL return HTTP 404.
6. WHEN an admin attempts to delete a Quota_Group that has one or more assigned users, THE Admin_Portal SHALL return HTTP 400 with a message indicating the number of assigned users.

---

### Requirement 11: Backward Compatibility

**User Story:** As an administrator, I want all existing portal functionality to work exactly as before, so that the upgrade does not disrupt current operations.

#### Acceptance Criteria

1. WHEN `GET /api/users` is called after the upgrade, THE Admin_Portal SHALL include all fields present in the original response plus the additive new fields (`quota_group_id`, `quota_group_name`, `quota_sync_status`).
2. WHEN `POST /api/users` is called without a `quota_group_id` field, THE Admin_Portal SHALL behave identically to the original implementation and SHALL NOT create any ResourceQuota.
3. WHEN `DELETE /api/users/{gitlab_id}/{username}` is called, THE Admin_Portal SHALL delete the GitLab user, Kubeflow Profile, namespace, and local database record, exactly as the original implementation does.
4. WHEN `GET /api/health` is called, THE Admin_Portal SHALL return the original health response shape with `status`, `kubernetes_reachable`, `gitlab_reachable`, and `longhorn_reachable` fields.
5. WHEN existing users with `quota_group_id = NULL` are present in the Database, THE Admin_Portal SHALL NOT assign them a ResourceQuota unless an admin explicitly assigns a Quota_Group to them.

---

### Requirement 12: Input Sanitisation and Security

**User Story:** As a system operator, I want all resource limit inputs to be validated server-side before reaching the Kubernetes API, so that malformed or malicious strings cannot cause cluster-side errors.

#### Acceptance Criteria

1. THE Validator SHALL validate all resource limit field values (CPU, memory, storage, GPU) server-side using Pydantic field validators before the values are passed to the Kubernetes_Service; IF validation fails, THEN THE Validator SHALL block the values and prevent them from being passed to the Kubernetes_Service.
2. WHEN quota limit strings pass Pydantic validation, THE Admin_Portal SHALL use them verbatim as Kubernetes `spec.hard` values without further string construction.
3. THE Admin_Portal SHALL protect all `/api/quota-groups/*`, `/api/users/*/quota`, and `/api/sync/*` endpoints with the same admin authentication mechanism used by existing user endpoints.
