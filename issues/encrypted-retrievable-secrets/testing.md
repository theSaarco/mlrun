# Testing: Encrypted Retrievable Project Secrets

## Code Review

### `mlrun/common/schemas/secret.py`

**Status: PASS**

- `retrievable_keys: list[str] = Field(default_factory=list)` correctly added to `SecretsData`.
  `default_factory=list` avoids the shared-mutable-default pitfall. Correct pydantic v1 convention.
- `RetrievableSecretsData` is clean: `provider` defaults to kubernetes, `secrets` uses `default_factory=dict`.
- No issues.

### `mlrun/common/schemas/__init__.py`

**Status: PASS**

- `RetrievableSecretsData` correctly exported alongside the existing secret schema exports.

### `server/py/services/api/crud/secrets.py`

**Status: PASS — all 6 review revisions applied correctly**

**Constant:**
- `retrievable_keys_secret_key = f"{internal_secrets_key_prefix}retrievable-keys"` correct.

**`store_project_secrets`:**
- Retrievable-keys validation and injection happen AFTER `_validate_and_enrich_project_secrets_to_store` returns `secrets_to_store`. This correctly avoids the `None.copy()` crash (review revision 3).
- Internal key guard (`is_internal_project_secret_key`) raises `MLRunAccessDeniedError` before the presence check — correct ordering.
- Presence check uses `rk not in (secrets_to_store or {})` — handles the `secrets_to_store=None` case correctly.
- Merges via `set` union and `sorted()` — deterministic output.
- Injects `mlrun.retrievable-keys` only into `secrets_to_store`, not into the `secrets` argument — avoids triggering regex validation on the internal key.

**`_get_retrievable_keys_list`:**
- Calls `self.secrets_provider.get_project_secret_data()` directly, bypassing the `allow_secrets_from_k8s` guard (review revision 2).
- Returns `[]` on missing key or JSON decode error.
- JSON decode error caught via `(json.JSONDecodeError, TypeError)` — correct.
- Structured log on error: `logger.warning("...", project=project)` — MLRun convention.

**`list_retrievable_project_secrets`:**
- Single K8s read via `get_project_secret_data(project, None)` (review revision 5).
- Extracts metadata list and data values from the same response dict.
- `secrets is not None` check correctly distinguishes between "filter by name" vs "return all".
- Raises `MLRunNotFoundError` for any requested key not in the retrievable set — information not leaked (review revision 6).
- Stale metadata entries silently skipped via `if key in all_data`.
- `is_internal_project_secret_key(key)` guard ensures internal keys never leak even if somehow in metadata.
- Docstring is clear and complete.

**`delete_project_secrets`:**
- `is_delete_all = not secrets` captured BEFORE the branch that replaces `secrets` with the listed user-visible keys — intent preserved correctly (review revision 4).
- Delete-all path: after deleting user-visible keys, deletes `mlrun.retrievable-keys` via a direct provider call (not `store_project_secrets`). Other internal keys (e.g., `mlrun.internal-key`) are NOT affected.
- Per-key path: reads current metadata, computes `updated_retrievable`, writes back or deletes as appropriate. Two sequential provider calls (not atomic) — acceptable because stale entries are benign at read time.

**`mlrun.retrievable-keys` excluded from `list_project_secret_keys`:**
- `list_project_secret_keys` filters internal keys via `is_internal_project_secret_key` when `allow_internal_secrets=False` (the default). Since `mlrun.retrievable-keys` starts with `mlrun.`, it is excluded from the public key listing. Verified by the existing `test_secrets_crud_internal_project_secrets` test.

### `server/py/services/api/api/endpoints/project_secrets.py`

**Status: PASS**

- `GET /projects/{project}/secrets/retrievable` endpoint follows all auth patterns:
  - `ensure_project` called.
  - `query_project_resource_permissions` with `AuthorizationResourceTypes.secret` and `AuthorizationAction.read`.
  - Provider validation (`provider != kubernetes`) raises `MLRunInvalidArgumentError`.
  - `db_session` dependency present (review revision 7).
  - Delegates to `Secrets().list_retrievable_project_secrets` via `run_in_threadpool`.
- Route path is distinct from `GET /projects/{project}/secrets` — no router conflict.

### `mlrun/db/base.py`

**Status: PASS**

- Both `create_project_secrets` (with new `retrievable_keys` parameter) and `get_project_retrievable_secrets` decorated with `@abstractmethod` (review revision 1).
- Signatures consistent with the rest of the abstract interface.

### `mlrun/db/httpdb.py`

**Status: PASS**

- `create_project_secrets` passes `retrievable_keys or []` in the `SecretsData` payload — correct null-to-empty conversion.
- `get_project_retrievable_secrets` constructs the GET request with correct params and deserializes `RetrievableSecretsData`.
- Docstrings present and accurate.
- Structured logging (`mlrun.utils.logger`) not required here since there is no log-worthy operation; the method delegates entirely to `api_call`.

### `mlrun/db/nopdb.py`

**Status: PASS**

- `create_project_secrets` stub accepts `retrievable_keys` parameter and does nothing (consistent with NopDB pattern).
- `get_project_retrievable_secrets` stub returns empty `RetrievableSecretsData` — correct default for disconnected state.

### `server/py/framework/rundb/sqldb.py`

**Status: PASS**

- Both stubs raise `NotImplementedError` — consistent with SQLRunDB pattern for Kubernetes-only features.

---

## Review Revisions Applied

All 6 required review revisions are correctly implemented:

1. `@abstractmethod` on both new `base.py` methods — DONE.
2. `_get_retrievable_keys_list` calls provider directly — DONE.
3. Retrievable-keys validation after `_validate_and_enrich` returns — DONE.
4. Delete-all and per-key delete paths call provider directly for metadata cleanup — DONE.
5. Single K8s read in `list_retrievable_project_secrets` — DONE.
6. Requested key not in retrievable set raises `MLRunNotFoundError` regardless of data key existence — DONE.

---

## Potential Issues Found

### Minor: `import json` inside test functions

The test file already imports `json` at the top of the module (line 18). The new tests have local `import json` statements inside individual test functions — these are redundant but harmless. They should be removed for cleanliness.

**Severity: Minor / style.** Not a bug.

### No blocking issues found.

---

## Test Results

### Test File
`server/py/services/api/tests/unit/crud/test_secrets.py`

### New Tests Added (13)

| Test | Status | What it covers |
|---|---|---|
| `test_store_retrievable_project_secrets_marks_keys` | PASS | Store a retrievable key, verify metadata written |
| `test_store_retrievable_project_secrets_merges_existing_list` | PASS | Two store calls merge into single metadata list |
| `test_store_retrievable_project_secrets_key_not_in_secrets_raises` | PASS | Key in `retrievable_keys` but not in `secrets` → `MLRunInvalidArgumentError` |
| `test_store_retrievable_project_secrets_internal_key_in_retrievable_raises` | PASS | `mlrun.*` key in `retrievable_keys` → `MLRunAccessDeniedError` |
| `test_list_retrievable_project_secrets_returns_marked_keys` | PASS | Only retrievable keys returned; non-retrievable excluded |
| `test_list_retrievable_project_secrets_filter_by_name` | PASS | `secrets=["KEY_A"]` filters to only requested key |
| `test_list_retrievable_project_secrets_nonexistent_key_raises` | PASS | Never-stored key → `MLRunNotFoundError` |
| `test_list_retrievable_project_secrets_non_retrievable_key_raises` | PASS | Existing-but-non-retrievable key → `MLRunNotFoundError` (no leakage) |
| `test_list_retrievable_project_secrets_stale_metadata_ignored` | PASS | Stale metadata entry (key removed externally) → silently skipped |
| `test_list_retrievable_project_secrets_empty_when_none_marked` | PASS | No retrievable keys marked → empty result |
| `test_delete_retrievable_key_updates_metadata` | PASS | Delete `KEY_A` removes it from metadata, `KEY_B` remains |
| `test_delete_all_removes_retrievable_metadata` | PASS | Delete-all removes `mlrun.retrievable-keys` |
| `test_internal_keys_not_exposed_in_retrievable_listing` | PASS | `mlrun.*` key in metadata never returned |

### Full Test Suite

All 46 tests in `server/py/services/api/tests/unit/crud/test_secrets.py` pass (33 pre-existing + 13 new).

### Linting

`ruff check` on all changed implementation files: **clean**.

```
mlrun/common/schemas/secret.py            → All checks passed
server/py/services/api/crud/secrets.py   → All checks passed
server/py/services/api/api/endpoints/project_secrets.py → All checks passed
mlrun/db/base.py                          → All checks passed
mlrun/db/httpdb.py                        → All checks passed
```

---

## BLOCKING ISSUES

None.
