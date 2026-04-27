# Proposal: Encrypted Retrievable Project Secrets (Option 1 / Approach 1a)

## Overview

Implement Option 1 (caller-managed encryption) with approach 1a (internal `mlrun.retrievable-keys`
metadata key). MLRun acts as a dumb store/retrieve transport; the caller is responsible for
encryption before storing and decryption after retrieval.

The change adds a `retrievable_keys` field to the store request, a new `GET` endpoint for
retrieval, and corresponding SDK + CRUD + base-class methods. All existing code paths are
preserved without modification.

---

## 1. Schema Changes — `mlrun/common/schemas/secret.py`

### 1a. Extend `SecretsData`

Add an optional `retrievable_keys` field. This list names which keys in `secrets` should be
marked as retrievable. Keys not listed here are stored as normal (non-retrievable) secrets.

```python
class SecretsData(BaseModel):
    provider: SecretProviderName = Field(SecretProviderName.vault)
    secrets: dict | None = {}
    retrievable_keys: list[str] = Field(default_factory=list)
```

The field uses pydantic v1 conventions consistent with the rest of the file (`from pydantic.v1
import BaseModel, Field`). `default_factory=list` avoids the shared-mutable-default pitfall.

### 1b. Add `RetrievableSecretsData` response model

A dedicated response type for the new retrieval endpoint, separating it cleanly from the
blocked `SecretsData` response of the existing `GET /projects/{project}/secrets` endpoint.

```python
class RetrievableSecretsData(BaseModel):
    provider: SecretProviderName = Field(SecretProviderName.kubernetes)
    secrets: dict[str, str] = Field(default_factory=dict)
```

This model is returned only by the new `GET /projects/{project}/secrets/retrievable` endpoint.
No changes are made to `SecretKeysData`.

---

## 2. CRUD Changes — `server/py/services/api/crud/secrets.py`

### 2a. Constant for the metadata key

Add a class-level constant alongside the existing `internal_secrets_key_prefix` and
`key_map_secrets_key_prefix`:

```python
retrievable_keys_secret_key = f"{internal_secrets_key_prefix}retrievable-keys"
```

### 2b. Extend `store_project_secrets`

Modify the existing `store_project_secrets` method to accept and process `retrievable_keys` from
the `SecretsData` object. When `secrets.retrievable_keys` is non-empty:

1. Validate that every key in `retrievable_keys` is also present in `secrets.secrets`. Raise
   `MLRunInvalidArgumentError` if any listed key is not being stored in this call.
2. Validate that no key in `retrievable_keys` starts with `mlrun.` (the internal prefix). Raise
   `MLRunAccessDeniedError` if violated.
3. Read the current `mlrun.retrievable-keys` value from the K8s secret (via
   `_get_retrievable_keys_list`, described below).
4. Merge the new keys into the existing list (union, deduplication via `set`).
5. Inject the updated `mlrun.retrievable-keys` JSON string into `secrets_to_store` before
   calling `secrets_provider.store_project_secrets`.

Steps 3–5 happen inside `_validate_and_enrich_project_secrets_to_store` (or in a new private
helper called from `store_project_secrets` for the k8s branch), so that the metadata key and
the data keys are written in a single `store_secrets_with_retry` call — no separate K8s PATCH.

**Signature change (no breaking change — new field on existing schema object):**

```python
def store_project_secrets(
    self,
    project: str,
    secrets: mlrun.common.schemas.SecretsData,
    allow_internal_secrets: bool = False,
    key_map_secret_key: str | None = None,
    allow_storing_key_maps: bool = False,
):
```

No new parameters on the method itself; the `retrievable_keys` data arrives via the schema.

### 2c. New method: `list_retrievable_project_secrets`

```python
def list_retrievable_project_secrets(
    self,
    project: str,
    secrets: list[str] | None = None,
) -> mlrun.common.schemas.RetrievableSecretsData:
```

Steps:

1. Read `mlrun.retrievable-keys` from K8s using `list_project_secrets` with
   `allow_secrets_from_k8s=True` and `allow_internal_secrets=True`.
2. Decode the JSON list. If the key does not exist or is empty, return empty `RetrievableSecretsData`.
3. If the caller passed a `secrets` filter list, intersect it with the retrievable set. Return
   `MLRunNotFoundError` for any requested key not in the retrievable set.
4. Read the actual values for the intersected keys via `list_project_secrets` with
   `allow_secrets_from_k8s=True`. Filter out any stale entries in `mlrun.retrievable-keys` that
   no longer have a corresponding data key (benign — stale entries are silently skipped).
5. Return `RetrievableSecretsData(provider=..., secrets={key: ciphertext, ...})`.

The method never passes `allow_secrets_from_k8s=True` to public-facing callers; the permission
boundary is enforced by only allowing retrieval of keys that appear in `mlrun.retrievable-keys`.

### 2d. New private helper: `_get_retrievable_keys_list`

```python
def _get_retrievable_keys_list(self, project: str) -> list[str]:
```

Reads `mlrun.retrievable-keys` from K8s and returns the decoded list. Returns `[]` if the key
does not exist. Used by both `store_project_secrets` (to merge) and
`list_retrievable_project_secrets` (to filter).

### 2e. Extend `delete_project_secrets`

When `secrets` (the list of keys to delete) is provided, check each key: if it is in the
current `mlrun.retrievable-keys` list, remove it from the list and update
`mlrun.retrievable-keys` in the same K8s write batch (via `store_project_secrets` with
`allow_internal_secrets=True`).

When `secrets` is empty/None (delete-all), also delete the `mlrun.retrievable-keys` metadata
key by including it in the deletion list only when `allow_internal_secrets=False` (the normal
delete-all path already leaves internal keys intact per existing logic; the implementation must
explicitly delete `mlrun.retrievable-keys` when performing a user-triggered delete-all since
the user's regular keys are being wiped).

Implementation note: the existing `delete_project_secrets` already has a branch that builds
the list of non-internal keys for delete-all. After that list is processed, add a second write
to purge `mlrun.retrievable-keys` if the remaining retrievable set becomes empty after the
deletion.

---

## 3. K8s Helper Changes — `server/py/framework/utils/singletons/k8s.py`

**No new methods required.** The existing `store_project_secrets`, `get_project_secret_data`,
and `get_project_secret_keys` methods, combined with `store_secrets_with_retry`, are sufficient.

The single relevant clarification: the CRUD layer injects `mlrun.retrievable-keys` as an
additional entry in the `secrets` dict passed to `secrets_provider.store_project_secrets`.
Since that dict is processed in a single `store_secrets_with_retry` call, atomicity is
guaranteed by the existing retry-on-conflict mechanism (`retry_on_conflict_count=5`).

---

## 4. Endpoint Changes — `server/py/services/api/api/endpoints/project_secrets.py`

### 4a. New endpoint: `GET /projects/{project}/secrets/retrievable`

```python
@router.get(
    "/projects/{project}/secrets/retrievable",
    response_model=mlrun.common.schemas.RetrievableSecretsData,
)
async def get_project_retrievable_secrets(
    project: str,
    secrets: list[str] = fastapi.Query(None, alias="secret"),
    provider: mlrun.common.schemas.SecretProviderName = mlrun.common.schemas.SecretProviderName.kubernetes,
    auth_info: mlrun.common.schemas.AuthInfo = fastapi.Depends(
        framework.api.deps.authenticate_request
    ),
    db_session: Session = fastapi.Depends(framework.api.deps.get_db_session),
):
```

Steps:
1. Call `ensure_project` (same as all other endpoints in this file).
2. Call `AuthVerifier().query_project_resource_permissions` with
   `AuthorizationResourceTypes.secret` and `AuthorizationAction.read` (same as the existing
   `list_project_secrets` endpoint).
3. Validate `provider == SecretProviderName.kubernetes` — the new feature is k8s-only. Raise
   `MLRunInvalidArgumentError` for other providers.
4. Call `await run_in_threadpool(services.api.crud.Secrets().list_retrievable_project_secrets, project, secrets)`.
5. Return the `RetrievableSecretsData` result.

The route path `/retrievable` must be registered before `/` on the same prefix, but since FastAPI
uses path matching order, and `/secrets/retrievable` is a distinct path from `/secrets`, there is
no conflict with the existing `GET /projects/{project}/secrets` route.

The existing `POST /projects/{project}/secrets` endpoint requires no changes — it already accepts
the full `SecretsData` body, and the new `retrievable_keys` field is optional with a default of
`[]`, so existing callers are unaffected.

---

## 5. SDK Changes

### 5a. `mlrun/db/base.py`

Add a new abstract method (not `@abstractmethod` — follow the existing pattern of
`create_project_secrets` which is a non-abstract pass-through, since this feature is
kubernetes-only and may not have a Vault implementation):

```python
def get_project_retrievable_secrets(
    self,
    project: str,
    secrets: list[str] | None = None,
    provider: Union[
        str, mlrun.common.schemas.SecretProviderName
    ] = mlrun.common.schemas.SecretProviderName.kubernetes,
) -> mlrun.common.schemas.RetrievableSecretsData:
    pass
```

Also extend the existing `create_project_secrets` signature to accept `retrievable_keys`:

```python
def create_project_secrets(
    self,
    project: str,
    provider: Union[
        str, mlrun.common.schemas.SecretProviderName
    ] = mlrun.common.schemas.SecretProviderName.kubernetes,
    secrets: dict | None = None,
    retrievable_keys: list[str] | None = None,
):
    pass
```

### 5b. `mlrun/db/httpdb.py`

**Extend `create_project_secrets`:**

Add `retrievable_keys: list[str] | None = None` parameter. When provided, include it in the
`SecretsData` payload sent to `POST /projects/{project}/secrets`.

```python
def create_project_secrets(
    self,
    project: str,
    provider: Union[
        str, mlrun.common.schemas.SecretProviderName
    ] = mlrun.common.schemas.SecretProviderName.kubernetes,
    secrets: dict | None = None,
    retrievable_keys: list[str] | None = None,
):
    path = f"projects/{project}/secrets"
    secrets_input = mlrun.common.schemas.SecretsData(
        secrets=secrets,
        provider=provider,
        retrievable_keys=retrievable_keys or [],
    )
    body = secrets_input.dict()
    error_message = f"Failed creating secret provider {project}/{provider}"
    self.api_call("POST", path, error_message, body=dict_to_json(body))
```

**Add `get_project_retrievable_secrets`:**

```python
def get_project_retrievable_secrets(
    self,
    project: str,
    secrets: list[str] | None = None,
    provider: Union[
        str, mlrun.common.schemas.SecretProviderName
    ] = mlrun.common.schemas.SecretProviderName.kubernetes,
) -> mlrun.common.schemas.RetrievableSecretsData:
    """Retrieve pre-encrypted (user-encrypted) project secrets.

    Returns only secrets that were stored with retrievable_keys marking.
    The returned values are ciphertexts as stored — the caller is responsible
    for decryption using their own private/symmetric key.

    :param project: The project name.
    :param secrets: A list of specific secret keys to retrieve. If None or empty,
        retrieves all keys that are marked as retrievable.
    :param provider: The secrets provider. Only ``kubernetes`` is supported.
    :return: RetrievableSecretsData containing the ciphertext values.
    """
    path = f"projects/{project}/secrets/retrievable"
    params = {"provider": provider, "secret": secrets}
    error_message = f"Failed retrieving retrievable secrets {project}/{provider}"
    result = self.api_call("GET", path, error_message, params=params)
    return mlrun.common.schemas.RetrievableSecretsData(**result.json())
```

---

## 6. Tests

### 6a. Extend `server/py/services/api/tests/unit/crud/test_secrets.py`

Add the following test functions in the existing file. All tests use the `k8s_secrets_mock`
fixture from `conftest.py` (which uses `InMemorySecretProvider` via `test_mode_mock_secrets`).

**`test_store_retrievable_project_secrets_marks_keys`**
- Store secrets with `retrievable_keys=["CIPHER_KEY"]`.
- Assert that `mlrun.retrievable-keys` is written with `["CIPHER_KEY"]` in the K8s mock.
- Assert the ciphertext value is stored under `CIPHER_KEY`.

**`test_store_retrievable_project_secrets_merges_existing_list`**
- Store `KEY_A` as retrievable.
- Store `KEY_B` as retrievable in a second call.
- Assert `mlrun.retrievable-keys` contains both `["KEY_A", "KEY_B"]`.

**`test_store_retrievable_project_secrets_key_not_in_secrets_raises`**
- Store `secrets={"CIPHER_KEY": "val"}` with `retrievable_keys=["OTHER_KEY"]` (key not in secrets).
- Assert `MLRunInvalidArgumentError` is raised.

**`test_store_retrievable_project_secrets_internal_key_in_retrievable_raises`**
- Attempt to mark `mlrun.something` as retrievable.
- Assert `MLRunAccessDeniedError` is raised.

**`test_list_retrievable_project_secrets_returns_marked_keys`**
- Store one retrievable key (`CIPHER_KEY`) and one non-retrievable key (`PLAIN_KEY`).
- Call `list_retrievable_project_secrets(project)`.
- Assert result contains only `{"CIPHER_KEY": <ciphertext>}`.
- Assert `PLAIN_KEY` is absent.

**`test_list_retrievable_project_secrets_filter_by_name`**
- Store `KEY_A` and `KEY_B` as retrievable.
- Call `list_retrievable_project_secrets(project, secrets=["KEY_A"])`.
- Assert only `KEY_A` is returned.

**`test_list_retrievable_project_secrets_nonexistent_key_raises`**
- Store only `KEY_A` as retrievable.
- Call `list_retrievable_project_secrets(project, secrets=["DOES_NOT_EXIST"])`.
- Assert `MLRunNotFoundError` is raised.

**`test_list_retrievable_project_secrets_stale_metadata_ignored`**
- Manually inject `mlrun.retrievable-keys = ["STALE_KEY"]` without a corresponding data key.
- Call `list_retrievable_project_secrets(project)`.
- Assert empty result (no crash, stale key silently ignored).

**`test_list_retrievable_project_secrets_empty_when_none_marked`**
- Store secrets without `retrievable_keys`.
- Call `list_retrievable_project_secrets(project)`.
- Assert empty result.

**`test_delete_retrievable_key_updates_metadata`**
- Store `KEY_A` and `KEY_B` as retrievable.
- Delete `KEY_A` via `delete_project_secrets`.
- Assert `mlrun.retrievable-keys` now contains only `["KEY_B"]`.
- Assert `KEY_A` data is also gone.

**`test_delete_all_removes_retrievable_metadata`**
- Store one retrievable key.
- Call `delete_project_secrets(project, provider, secrets=[])` (delete all).
- Assert `mlrun.retrievable-keys` is also removed from the K8s secret.

**`test_internal_keys_not_exposed_in_retrievable_listing`**
- Directly inject `mlrun.some-internal` key with a value.
- Call `list_retrievable_project_secrets`.
- Assert `mlrun.some-internal` is never returned.

### 6b. Add `server/py/services/api/tests/unit/api/test_project_retrievable_secrets.py`

A new file for endpoint-level tests using `fastapi.testclient.TestClient` and the
`k8s_secrets_mock` fixture. This mirrors the structure of `test_project_secrets.py`.

**`test_store_and_retrieve_retrievable_secret`**
- POST to `/projects/{project}/secrets` with `{"secrets": {"K": "ciphertext"}, "retrievable_keys": ["K"]}`.
- Assert `HTTP 201`.
- GET `/projects/{project}/secrets/retrievable`.
- Assert `HTTP 200`, body `{"provider": "kubernetes", "secrets": {"K": "ciphertext"}}`.

**`test_retrieve_non_retrievable_secret_returns_empty`**
- POST to `/projects/{project}/secrets` with `{"secrets": {"K": "v"}}` (no `retrievable_keys`).
- GET `/projects/{project}/secrets/retrievable`.
- Assert `HTTP 200`, body `{"provider": "kubernetes", "secrets": {}}`.

**`test_retrieve_specific_key_not_in_retrievable_returns_404`**
- POST a retrievable key `KEY_A`.
- GET `/projects/{project}/secrets/retrievable?secret=KEY_B` (KEY_B not retrievable).
- Assert `HTTP 404`.

**`test_existing_list_secrets_endpoint_unchanged`**
- POST a retrievable key.
- GET `/projects/{project}/secrets` (existing endpoint).
- Assert it still returns `MLRunAccessDeniedError` (HTTP 403) — existing behavior preserved.

**`test_internal_keys_not_exposed_via_endpoint`**
- GET `/projects/{project}/secrets/retrievable`.
- Assert response body does not contain any key starting with `mlrun.`.

---

## Summary of Changed Files

| File | Change type | Description |
|---|---|---|
| `mlrun/common/schemas/secret.py` | Extend + Add | `retrievable_keys` on `SecretsData`; new `RetrievableSecretsData` |
| `server/py/services/api/crud/secrets.py` | Extend + Add | `retrievable_keys_secret_key` constant; enrich `store_project_secrets`; `list_retrievable_project_secrets`; `_get_retrievable_keys_list`; update `delete_project_secrets` |
| `server/py/services/api/api/endpoints/project_secrets.py` | Add | New `GET /projects/{project}/secrets/retrievable` endpoint |
| `mlrun/db/httpdb.py` | Extend + Add | `retrievable_keys` param on `create_project_secrets`; new `get_project_retrievable_secrets` |
| `mlrun/db/base.py` | Extend + Add | `retrievable_keys` param on `create_project_secrets`; new `get_project_retrievable_secrets` |
| `server/py/services/api/tests/unit/crud/test_secrets.py` | Extend | 11 new test functions |
| `server/py/services/api/tests/unit/api/test_project_retrievable_secrets.py` | Create | 5 endpoint-level tests |
| `server/py/framework/utils/singletons/k8s.py` | None | No changes required |

---

## Design Decisions and Rationale

### Single K8s write per store call

The `mlrun.retrievable-keys` update is injected into the `secrets_to_store` dict before the
single call to `secrets_provider.store_project_secrets`. This means the metadata key and the
data key are written in one `store_secrets_with_retry` call, making the operation atomic under
conflict-retry. There is no separate metadata write.

### Stale metadata entries are benign

If a user deletes a key without going through the delete endpoint (e.g., direct K8s manipulation),
`mlrun.retrievable-keys` may contain entries for non-existent data keys. The
`list_retrievable_project_secrets` method intersects the metadata list with the actual data keys
present, so stale entries are silently ignored. The `delete_project_secrets` path also cleans up
stale entries when it processes deletions.

### Endpoint path `/secrets/retrievable` vs. query parameter

A dedicated path makes the intent unambiguous and avoids ambiguity in the FastAPI router between
`?retrievable_only=true` and the existing `?secret=KEY` parameter on the blocked endpoint. The
existing `GET /projects/{project}/secrets` endpoint is not modified, preserving all existing
client behavior.

### `RetrievableSecretsData` vs. reusing `SecretsData`

A dedicated response model avoids confusion about when `allow_secrets_from_k8s` is implicitly
true. Clients that receive a `RetrievableSecretsData` know they are getting pre-encrypted
ciphertext, not plaintext.

### Vault provider is out of scope

The new `retrievable_keys` marking and the retrieval endpoint are kubernetes-only. If `provider`
is `vault` on the new endpoint, the endpoint raises `MLRunInvalidArgumentError`. This matches the
problem scope and the existing pattern (the problem document calls out K8s as the sole backend).

### No changes to `K8sHelper`

All the necessary K8s operations (read, write with retry) already exist. The CRUD layer
orchestrates them without requiring new K8s helper methods.
