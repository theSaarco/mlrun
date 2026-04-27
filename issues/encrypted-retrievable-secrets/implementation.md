# Implementation: Encrypted Retrievable Project Secrets

## Files Changed

### `mlrun/common/schemas/secret.py`
- Added `retrievable_keys: list[str] = Field(default_factory=list)` field to `SecretsData`.
- Added new `RetrievableSecretsData(BaseModel)` response model with fields `provider: SecretProviderName` and `secrets: dict[str, str]`.

### `mlrun/common/schemas/__init__.py`
- Exported `RetrievableSecretsData` from the `secret` submodule.

### `server/py/services/api/crud/secrets.py`
- Added class constant `retrievable_keys_secret_key = f"{internal_secrets_key_prefix}retrievable-keys"` on `Secrets`.
- Extended `store_project_secrets`: after `_validate_and_enrich_project_secrets_to_store` returns `secrets_to_store`, validates that every key in `secrets.retrievable_keys` is present in `secrets_to_store` and not an internal key, merges with the existing retrievable list via `_get_retrievable_keys_list`, and injects `mlrun.retrievable-keys` into `secrets_to_store` before the K8s write. This ensures the internal key bypasses the per-key validation loop and `None.copy()` crash is avoided.
- Added `_get_retrievable_keys_list(self, project) -> list[str]`: reads `mlrun.retrievable-keys` directly via `self.secrets_provider.get_project_secret_data` (not via `list_project_secrets`) and JSON-decodes the value.
- Added `list_retrievable_project_secrets(self, project, secrets=None) -> RetrievableSecretsData`: performs a single `get_project_secret_data(project, None)` call, extracts both the metadata list and the data values from the response, filters to requested keys (or all if `None`), raises `MLRunNotFoundError` for any requested key not in the retrievable set, silently skips stale metadata entries, and never returns internal keys.
- Extended `delete_project_secrets`:
  - Delete-all path: after deleting user-visible keys, calls `self.secrets_provider.delete_project_secrets(project, [self.retrievable_keys_secret_key])` directly to clean up the metadata key.
  - Per-key path: reads current retrievable list, computes new list after removing deleted keys, and either writes the updated list back or deletes the metadata key entirely via direct provider calls (not via `store_project_secrets`).

### `server/py/services/api/api/endpoints/project_secrets.py`
- Added `GET /projects/{project}/secrets/retrievable` endpoint (`get_project_retrievable_secrets`). Follows the same auth pattern as existing endpoints: `ensure_project`, `query_project_resource_permissions` with `AuthorizationResourceTypes.secret` / `read`, validates `provider == kubernetes`, then delegates to `Secrets().list_retrievable_project_secrets`. Includes `db_session` dependency as required by the framework.

### `mlrun/db/base.py`
- Added `retrievable_keys: list[str] | None = None` parameter to the `@abstractmethod create_project_secrets` signature.
- Added new `@abstractmethod get_project_retrievable_secrets(self, project, secrets=None, provider=...) -> RetrievableSecretsData`.

### `mlrun/db/httpdb.py`
- Extended `create_project_secrets` with `retrievable_keys: list[str] | None = None` parameter; passes `retrievable_keys or []` in the `SecretsData` payload.
- Added `get_project_retrievable_secrets(self, project, secrets=None, provider=...)` calling `GET /projects/{project}/secrets/retrievable` and returning `RetrievableSecretsData`.

### `mlrun/db/nopdb.py`
- Added `retrievable_keys` parameter to `create_project_secrets` stub.
- Added `get_project_retrievable_secrets` stub (returns empty `RetrievableSecretsData`).

### `server/py/framework/rundb/sqldb.py`
- Added `retrievable_keys` parameter to `create_project_secrets` stub.
- Added `get_project_retrievable_secrets` stub (raises `NotImplementedError`).

## Review Revisions Applied

1. Both new `base.py` methods decorated with `@abstractmethod`.
2. `_get_retrievable_keys_list` calls provider directly, not via `list_project_secrets`.
3. Retrievable-keys validation and injection occur after `_validate_and_enrich_project_secrets_to_store` returns.
4. Delete-all and per-key delete paths call provider directly for metadata cleanup.
5. `list_retrievable_project_secrets` uses a single K8s read for both metadata and data.
6. Error behavior: requested key not in retrievable set raises `MLRunNotFoundError` regardless of whether the key exists as a non-retrievable data key (information not leaked).
