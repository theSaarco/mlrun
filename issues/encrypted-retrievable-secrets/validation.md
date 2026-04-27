# Validation: Encrypted Retrievable Project Secrets

## VALID

The problem is well-defined, technically accurate, and actionable. All key claims match the
actual codebase. Corrections and additions are noted below.

---

## 1. Accuracy of the Problem Description

### Storage mechanism — confirmed

The actual K8s secret name template is `mlrun-project-secrets-{project}` (not `mlrun-project-{project}`
as written in the problem). Confirmed in `mlrun/config.py` line 747:

```
"project_secret_name": "mlrun-project-secrets-{project}",
```

This is a minor naming discrepancy in the problem statement; the architecture (one K8s Opaque
secret per project, flat key/value data) is accurate.

### Blocking of K8s value retrieval — confirmed

In `server/py/services/api/crud/secrets.py`, `Secrets.list_project_secrets` (line 337):

```python
if not allow_secrets_from_k8s:
    raise mlrun.errors.MLRunAccessDeniedError(
        "Not allowed to list secrets data from kubernetes provider"
    )
```

The REST endpoint `GET /projects/{project}/secrets` (in `project_secrets.py` lines 167–173)
calls `Secrets().list_project_secrets` without passing `allow_secrets_from_k8s=True`, so K8s
values are always blocked externally. Internal callers (e.g., `get_project_secret_provider`)
pass `allow_secrets_from_k8s=True` explicitly.

### SDK method table — confirmed with one note

The SDK method names in `mlrun/db/httpdb.py` match the table in the problem:
- `create_project_secrets` (line 3505) — write-only POST
- `list_project_secret_keys` (line 3596) — returns keys only
- `list_project_secrets` (line 3552) — calls the blocked GET endpoint

Note: `list_project_secrets` in the SDK is not technically "blocked" on the client side; it
sends the GET request but the server always raises `MLRunAccessDeniedError` for the kubernetes
provider because the endpoint never passes `allow_secrets_from_k8s=True`. The SDK docstring
incorrectly describes this method as being for Vault only and says "kubernetes provider only
supports an empty list" — this should be corrected in the implementation.

### Internal key prefix convention — confirmed

`Secrets.internal_secrets_key_prefix = "mlrun."` (line 56 of `secrets.py`) and
`Secrets.validate_internal_project_secret_key_allowed` (line 99) enforce that users cannot
write keys starting with `mlrun.`. This is the correct building block for `mlrun.retrievable-keys`.

### `store_secrets_with_retry` exists — confirmed

`K8sHelper.store_secrets_with_retry` (line 683 of `k8s.py`) exists with conflict-retry logic
(`retry_on_conflict_count=5`). The atomic-update concern raised by approach 1a is real and
already addressed by this mechanism.

---

## 2. Constraints — All Realistic

- **K8s as sole backend**: confirmed — no DB writes happen in the secrets path.
- **`mlrun.` prefix for internal keys**: enforced in code; approach 1a fits naturally.
- **Auth/authz via `AuthVerifier`**: the existing `store_project_secrets` and
  `list_project_secret_keys` endpoints both call
  `AuthVerifier().query_project_resource_permissions` with `AuthorizationResourceTypes.secret`
  — the new endpoint must do the same.
- **Existing unencrypted path unchanged**: Option 1 adds a new flag path alongside the current
  flow; no existing logic is altered.

---

## 3. Success Criteria — Clear and Testable

Each criterion maps to a specific code change:

| Criterion | Testable via |
|---|---|
| Mark a secret as retrievable | New `retrievable_keys` param on POST endpoint / CRUD |
| API returns ciphertext for marked keys | New GET endpoint or `?retrievable_only=true` param |
| Non-marked secrets remain unretrievable | Existing `allow_secrets_from_k8s=False` guard |
| Internal `mlrun.*` keys not exposed | `is_internal_project_secret_key` filter (already exists) |
| Existing tests pass unchanged | No changes to existing POST / GET key-list paths |
| New unit tests cover edge cases | Mockable via `InMemorySecretProvider` in test mode |

The `InMemorySecretProvider` (used when `test_mode_mock_secrets=True`) means new logic can be
unit-tested without a K8s cluster — a significant advantage for testability.

---

## 4. Assessment of the Recommended Approach (Option 1 / 1a)

### Soundness — confirmed

Option 1 with approach 1a (internal metadata key `mlrun.retrievable-keys`) is sound. The
pattern of storing a JSON-encoded list under a `mlrun.*` key already exists in the codebase
(e.g., key maps stored under `mlrun.map.*` keys, as seen in `key_map_secrets_key_prefix`). The
`mlrun.retrievable-keys` key follows the exact same convention.

The atomic-update concern is real but handled: `store_secrets_with_retry` already retries on
K8s conflict (409) with configurable retry count. Updating `mlrun.retrievable-keys` at the same
time as storing the new ciphertext value happens in a single `store_project_secrets` call (which
passes the entire dict to `store_secrets_with_retry`), so both are written in one K8s PATCH
operation — there is no separate metadata write required.

### One implementation nuance

When a user deletes a retrievable key, the `mlrun.retrievable-keys` list must also be updated
to remove that entry. The existing `delete_project_secrets` CRUD method does not do this
automatically. The implementation will need to either:
- Hook into `delete_project_secrets` for the retrievable case, or
- Treat stale entries in `mlrun.retrievable-keys` as benign (the retrieval endpoint skips keys
  not present in the K8s secret data anyway).

The simpler approach is: when listing retrievable keys, intersect `mlrun.retrievable-keys` with
the set of keys actually present in K8s — stale entries are silently ignored. Deletion cleanup
of `mlrun.retrievable-keys` is still desirable for correctness but is not a blocker.

---

## 5. Items Blocking or Complicating the Implementation

### Nothing blocking Option 1

There are no architectural blockers. The implementation touches:

1. `mlrun/common/schemas/secret.py` — add `retrievable_keys: list[str] = []` to `SecretsData`.
2. `server/py/services/api/crud/secrets.py` — in `store_project_secrets`, if `retrievable_keys`
   is non-empty, merge them into `mlrun.retrievable-keys` before calling
   `secrets_provider.store_project_secrets`. In a new method `list_retrievable_project_secrets`,
   read `mlrun.retrievable-keys` (with `allow_internal_secrets=True`, `allow_secrets_from_k8s=True`)
   and then return only those keys' values via `list_project_secrets` with
   `allow_secrets_from_k8s=True`.
3. `server/py/services/api/api/endpoints/project_secrets.py` — new GET endpoint (or query
   param) that calls the new CRUD method.
4. `mlrun/db/httpdb.py` — new SDK method `get_project_secrets` (or add `retrievable_only` param
   to `list_project_secrets`).

### Minor schema note

`SecretsData` in `mlrun/common/schemas/secret.py` uses `pydantic.v1` (line 18: `from
pydantic.v1 import BaseModel, Field`), not pydantic v2. The new `retrievable_keys` field must
follow the same v1 conventions (default factory via `Field(default_factory=list)` or
`default=[]`).

### Correction to problem statement

The problem states the K8s secret name is `mlrun-project-{project}`. The actual template
(from `mlrun/config.py`) is `mlrun-project-secrets-{project}`. This does not affect the design
but should be corrected in the problem document.

---

## Summary

The problem is valid. The description accurately captures the current restriction
(`allow_secrets_from_k8s=False` in `Secrets.list_project_secrets`), the K8s one-secret-per-project
structure, the `mlrun.` internal key prefix convention, and the atomic-write capability via
`store_secrets_with_retry`. Option 1 (approach 1a) is the correct starting point: low risk, no
new dependencies, consistent with existing patterns, and fully testable with the in-memory
secret provider. No blocking issues were found.
