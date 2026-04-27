# Review: Encrypted Retrievable Project Secrets (Option 1 / Approach 1a)

**Verdict: REVISE**

The proposal is well-structured and technically sound in its overall direction, but contains
several specific issues that must be corrected before implementation begins. None are
architectural blockers — they are concrete, fixable problems with exact corrections listed below.

---

## Summary of Findings

| Area | Status | Note |
|---|---|---|
| Schema changes | Minor fix needed | `create_project_secrets` is `@abstractmethod` in `base.py` — proposal says it is not |
| Atomicity of store | Correct | Single dict passed to `store_secrets_with_retry` — atomic |
| Deletion cleanup | Bug | Proposed delete-all path has a structural flaw |
| `_get_retrievable_keys_list` design | Bug | Recursive `list_project_secrets` call creates a re-entrant read before the write |
| Stale-key intersection | Correct | Read-time intersection is safe |
| Edge case: no `mlrun.retrievable-keys` | Handled | Empty-list default on missing key is correct |
| Authorization | Correct | Same `AuthorizationResourceTypes.secret` / `read` action is appropriate |
| Scope | Minor | One REVISE item on error behavior for filtered key not in retrievable set |
| `SecretsData` `secrets` field nullability | Bug | `secrets.secrets.copy()` in `_validate_and_enrich` crashes when `secrets.secrets is None` |
| Missing: `provider` validation in CRUD | Missing | Proposal only validates at endpoint level; CRUD should guard too |

---

## Required Changes (Numbered for Implementer)

### 1. `base.py`: `create_project_secrets` IS an `@abstractmethod` — extend accordingly

The proposal (section 5a) states:

> Add a new abstract method (not `@abstractmethod` — follow the existing pattern of
> `create_project_secrets` which is a non-abstract pass-through)

This is wrong. Confirmed in `mlrun/db/base.py` lines 614–623:

```python
@abstractmethod
def create_project_secrets(
    self,
    project: str,
    provider: ...,
    secrets: dict | None = None,
):
    pass
```

`create_project_secrets` **is** decorated with `@abstractmethod`. Similarly,
`list_project_secrets`, `list_project_secret_keys`, and `delete_project_secrets` are all
`@abstractmethod`. The new `get_project_retrievable_secrets` must also be `@abstractmethod` in
`base.py`. The implementer must not add it as a bare `def ... pass` — that would silently break
any non-HTTP `RunDB` subclass that doesn't implement it.

**Fix**: Add `@abstractmethod` to `get_project_retrievable_secrets` in `base.py`. Add
`retrievable_keys` to the `@abstractmethod` signature of `create_project_secrets` in `base.py`.

---

### 2. `_get_retrievable_keys_list` must call the provider directly, not `list_project_secrets`

The proposal (section 2d) says `_get_retrievable_keys_list` reads `mlrun.retrievable-keys` "from
K8s using `list_project_secrets` with `allow_secrets_from_k8s=True` and
`allow_internal_secrets=True`".

However, `store_project_secrets` calls `_validate_and_enrich_project_secrets_to_store` (which
validates the incoming `secrets` dict) and then — according to the proposal — would call
`_get_retrievable_keys_list` to read the current metadata before writing. At that point the
CRUD is mid-store: calling `list_project_secrets` from within `store_project_secrets` is
safe for reading, but is unnecessarily layered. More importantly, `list_project_secrets` in the
CRUD method returns a `SecretsData` object (with a non-trivial filter), while
`_get_retrievable_keys_list` only needs a `dict[str, str]` or a raw string from the provider.

The simpler and more direct path is: call `self.secrets_provider.get_project_secret_data(project,
[self.retrievable_keys_secret_key])` directly. This bypasses the `allow_secrets_from_k8s` guard
(which is the right thing to do for an internal server read), avoids constructing an unnecessary
`SecretsData` object, and exactly mirrors how `_get_project_secret_key_map` works (lines
908–923 of `secrets.py` — that method calls `list_project_secrets` with internal flags to read
a map key, which is acceptable but the pattern is already inconsistent).

**Fix**: Implement `_get_retrievable_keys_list` by calling
`self.secrets_provider.get_project_secret_data(project, [self.retrievable_keys_secret_key])`
directly, then JSON-decoding the value at `result.get(self.retrievable_keys_secret_key)`. Return
`[]` on `None` or missing key. Do not route through `list_project_secrets`.

---

### 3. `_validate_and_enrich_project_secrets_to_store` crashes when `secrets.secrets is None`

Confirmed in `secrets.py` line 846:

```python
secrets_to_store = secrets.secrets.copy()
```

`SecretsData.secrets` is typed as `dict | None = {}`. If a caller passes `secrets=None`
explicitly, `secrets.secrets` is `None` and `.copy()` raises `AttributeError`. This is a
pre-existing bug in the base class. However, the new code in `store_project_secrets` must read
`secrets.retrievable_keys` and then inject `mlrun.retrievable-keys` into `secrets_to_store`. If
`secrets.secrets` is `None`, the whole `secrets_to_store` dict is `None` and the injection
path cannot proceed.

In `create_project_secrets` in `httpdb.py` the proposal sets `retrievable_keys or []`, so the
SDK client would never send `None` for `retrievable_keys`. But `secrets.secrets` could still be
`None` (e.g., a caller that only wants to update the metadata list without adding new data keys).
The proposal validates "every key in `retrievable_keys` is also present in `secrets.secrets`",
which means `secrets.secrets=None` with a non-empty `retrievable_keys` would fail validation
correctly. But the crash happens before that validation if `secrets_to_store = secrets.secrets.copy()`
runs first.

**Fix**: The injection of `mlrun.retrievable-keys` into `secrets_to_store` must happen after
`secrets_to_store` is built by `_validate_and_enrich_project_secrets_to_store`. That private
method returns the enriched dict. The proposed validation (step 1 in section 2b) and the
metadata-key injection (step 5) must both run **after** the call to
`_validate_and_enrich_project_secrets_to_store`, in `store_project_secrets` itself (not inside
the private method). This is already the intent of the proposal's description ("Steps 3–5 happen
inside `_validate_and_enrich_project_secrets_to_store` or in a new private helper"), but the
implementer must ensure validation runs on the already-enriched `secrets_to_store` dict, not on
the possibly-`None` raw `secrets.secrets`.

Concretely: move retrievable-keys validation and injection into `store_project_secrets` **after**
the call to `_validate_and_enrich_project_secrets_to_store` returns `secrets_to_store`.

---

### 4. Delete-all path in `delete_project_secrets` has a structural flaw

The proposal (section 2e) says:

> When `secrets` is empty/None (delete-all), also delete the `mlrun.retrievable-keys` metadata
> key by including it in the deletion list only when `allow_internal_secrets=False` (the normal
> delete-all path already leaves internal keys intact per existing logic; the implementation must
> explicitly delete `mlrun.retrievable-keys` when performing a user-triggered delete-all since
> the user's regular keys are being wiped).

Look at the actual delete-all path in `delete_project_secrets` (lines 225–269 of `secrets.py`):

```python
if not allow_internal_secrets:
    if secrets:
        # per-key delete path: validate no internal keys
        ...
    else:
        # delete-all path: list non-internal keys, set secrets = those keys
        secrets = self.list_project_secret_keys(
            project, provider, allow_internal_secrets=False
        ).secret_keys
        if not secrets:
            return  # <-- early return if no user-visible keys
```

Then execution falls through to `self.secrets_provider.delete_project_secrets(project, secrets)`.
At this point `secrets` is a list of **non-internal** keys (no `mlrun.*` entries). The
`mlrun.retrievable-keys` internal key is therefore **not** deleted by this path.

The proposal says to add "a second write to purge `mlrun.retrievable-keys`". The issue is that
`InMemorySecretProvider.delete_project_secrets` when called with a list of specific keys only
removes those keys; the internal `mlrun.retrievable-keys` key remains. The proposal's fix is
correct in concept (explicitly include `mlrun.retrievable-keys` in a subsequent delete), but the
proposed mechanism (using `store_project_secrets` with `allow_internal_secrets=True`) is more
expensive than necessary.

**Fix**: After the list-based delete call in the delete-all path, check whether the deleted
keys included any that were in the retrievable set. If any were deleted, call
`self.secrets_provider.delete_project_secrets(project, [self.retrievable_keys_secret_key])`
directly to clean up the metadata key. Do not route through `store_project_secrets` (which
triggers event emission unnecessarily for an internal metadata cleanup).

Additionally, for the per-key delete path (when a specific list of `secrets` is given), the
proposal correctly says to check each key against `mlrun.retrievable-keys` and remove matching
entries. The implementation must:
1. Read the current `mlrun.retrievable-keys` list.
2. Compute the new list = `current - set(secrets)`.
3. If the new list is empty, delete `mlrun.retrievable-keys` outright.
4. If the new list is shorter but non-empty, write the updated list back via a direct
   provider call.
5. Both the user-key deletion and the metadata update must be in a single batch call to
   `secrets_provider.store_project_secrets` (for the update case) or two sequential provider
   calls (delete user keys, then delete/update metadata). The two-call path is not atomic, but
   since stale entries are benign (read-time intersection handles them), this is acceptable.

The proposal mentions this but is vague about the exact call sequence. The implementer must
make a clear, explicit choice and document it.

---

### 5. Proposal raises `MLRunNotFoundError` for a filtered key not in the retrievable set — reconsider

The proposal (section 2c, step 3) says:

> If the caller passed a `secrets` filter list, intersect it with the retrievable set. Return
> `MLRunNotFoundError` for any requested key not in the retrievable set.

This is a security-information-leakage decision. Returning 404 tells the caller "this key exists
but is not retrievable" vs. "this key does not exist at all". Both are distinct from a plaintext
retrieval denial. However, from a UX standpoint this is reasonable — the key either is not
marked retrievable or does not exist.

The problem statement says: "Non-marked secrets remain unretrievable". A 404 for "key not
retrievable" is consistent with that goal and is no worse than the current behavior (the blocked
endpoint returns 403 for all k8s secrets regardless). However, the test
`test_retrieve_specific_key_not_in_retrievable_returns_404` in section 6b tests for HTTP 404
even though the key (`KEY_B`) was never stored at all, not just "not retrievable". These two
cases (never stored vs. stored but not marked retrievable) are conflated.

**Fix**: Decide explicitly on the error response for each of these two cases:
- Key is not in `mlrun.retrievable-keys` (may or may not exist as a data key): return 404 with
  a message that does NOT reveal whether the key exists as a non-retrievable secret.
- Key does not exist in the K8s secret data at all (stale in metadata): silently skip (per the
  proposal's "stale entries are benign" design decision).

Update the test for `test_retrieve_specific_key_not_in_retrievable_returns_404` to only test the
"exists but not marked retrievable" case, and add a separate test for "never stored".

---

### 6. `list_retrievable_project_secrets` reads K8s data twice — consolidate

The proposal (section 2c) describes:
1. Read `mlrun.retrievable-keys` from K8s.
2. Read the actual values for the intersected keys via `list_project_secrets`.

This is two calls to `self.secrets_provider.get_project_secret_data`. Since the single K8s
Opaque secret contains all keys, both reads can be combined into one
`self.secrets_provider.get_project_secret_data(project, None)` call that returns all data, from
which the implementation extracts both the metadata key value and the data values. This halves
the number of K8s API calls for the retrieval path.

**Fix (optional but strongly recommended)**: Call `get_project_secret_data(project, None)` once
to get the full secret data dict. Extract `self.retrievable_keys_secret_key` from that dict to
get the metadata list. Then filter the remaining keys to only those in the metadata list and
present in the dict. This is a single K8s call.

---

### 7. Endpoint must pass `db_session` consistently — it is unused but required

Looking at the existing endpoint signatures in `project_secrets.py`, `db_session` is a required
FastAPI dependency via `Depends(framework.api.deps.get_db_session)` even when it is not directly
used by the endpoint logic (the session management infra requires it for connection lifecycle).
The proposed endpoint signature (section 4a) includes `db_session` — this is correct. The
implementer must not omit it even though `list_retrievable_project_secrets` does not use the DB.

This is not a new issue — the proposal includes it — but it warrants explicit callout since
implementers sometimes omit unused parameters.

**Status**: Already correct in the proposal. No change needed; just verify during implementation.

---

### 8. `provider` check in endpoint vs. CRUD — CRUD should also guard

The proposal validates `provider == SecretProviderName.kubernetes` at the endpoint level
(section 4a, step 3). However, `list_retrievable_project_secrets` in the CRUD does not check
the provider — it always reads from `self.secrets_provider` which may not be K8s in all
test/deployment configurations.

For defense in depth, `list_retrievable_project_secrets` should also enforce that it only
operates on the kubernetes provider, raising `MLRunInvalidArgumentError` if called with
`provider=vault`. This mirrors the pattern in `list_project_secrets` which checks
`provider == vault` and requires a token.

**Fix**: Add a provider guard at the top of `list_retrievable_project_secrets`:

```python
if provider != mlrun.common.schemas.SecretProviderName.kubernetes:
    raise mlrun.errors.MLRunInvalidArgumentError(
        "list_retrievable_project_secrets is only supported for the kubernetes provider"
    )
```

---

## Non-Blocking Notes (Approve With These)

### A. `retrievable_keys_secret_key` constant

The proposal defines it as:
```python
retrievable_keys_secret_key = f"{internal_secrets_key_prefix}retrievable-keys"
```

This is correct and follows the existing pattern (`key_map_secrets_key_prefix` is defined the
same way). `mlrun.retrievable-keys` is not a valid K8s secret key name by default regex
(`mlrun.utils.regex.secret_key`), but since internal keys bypass the regex check via
`allow_internal_secrets=True`, this is fine.

Confirm that `validate_project_secret_key_regex` is not called on `mlrun.retrievable-keys`
during the internal write path. Looking at `_validate_and_enrich_project_secrets_to_store`
(line 849): `self.validate_project_secret_key_regex(secret_key)` is called for every key in
`secrets_to_store.keys()` unless `key_map_secret_key` is set. The internal key injection must
happen **after** this loop, not by injecting into the dict before `_validate_and_enrich` runs.
This is already implied by the fix in item 3 above (inject after `_validate_and_enrich` returns).

### B. Test for existing `list_project_secrets` endpoint returning 403 (not 404)

`test_existing_list_secrets_endpoint_unchanged` in section 6b asserts HTTP 403. Confirmed: the
server raises `MLRunAccessDeniedError` for the k8s provider when `allow_secrets_from_k8s=False`,
and MLRun maps `MLRunAccessDeniedError` to HTTP 403. This test assertion is correct.

### C. Atomicity claim is correct

The claim that "the metadata key and the data keys are written in one `store_secrets_with_retry`
call" is valid. `InMemorySecretProvider.store_project_secrets` does a single `dict.update()`.
`K8sHelper.store_secrets_with_retry` passes the entire dict as a single PATCH. The atomicity
guarantee holds for both the test provider and the real K8s provider.

### D. `SecretsData` pydantic v1 convention

The proposal uses `Field(default_factory=list)` for `retrievable_keys`. This is correct for
pydantic v1 — confirmed by the existing `from pydantic.v1 import BaseModel, Field` at the top
of `secret.py`. Do not use `default=[]` (shared mutable default). The proposal is correct here.

### E. Route ordering is not a concern

FastAPI matches paths exactly before matching path parameters. `/projects/{project}/secrets/retrievable`
is unambiguous vs. `/projects/{project}/secrets`. No route ordering issue.

---

## Summary of Required Changes

1. **`base.py`**: Add `@abstractmethod` to `get_project_retrievable_secrets`. Add
   `retrievable_keys` parameter to the existing `@abstractmethod create_project_secrets`.

2. **`secrets.py` `_get_retrievable_keys_list`**: Call
   `self.secrets_provider.get_project_secret_data()` directly; do not route through
   `list_project_secrets`.

3. **`secrets.py` `store_project_secrets`**: Perform retrievable-keys validation and
   metadata-key injection **after** `_validate_and_enrich_project_secrets_to_store` returns
   `secrets_to_store`, not inside it. This avoids the `None.copy()` crash path and avoids
   injecting an internal key through the validation loop.

4. **`secrets.py` `delete_project_secrets`**: Clarify and implement the delete path explicitly:
   - For delete-all: after deleting user-visible keys, separately delete `mlrun.retrievable-keys`
     via a direct provider call.
   - For per-key delete: read current metadata list, compute new list, and either delete or
     update `mlrun.retrievable-keys` in one additional provider call.

5. **`secrets.py` `list_retrievable_project_secrets`**: Add provider guard at the top of the
   method. Consolidate the two `get_project_secret_data` calls into one (retrieve all data,
   then extract both the metadata list and the data values from the single response).

6. **Tests**: Distinguish between "key not marked retrievable but exists" vs. "key never stored"
   in the error-behavior test. Both cases can return 404 but should be tested separately to
   ensure the intersection logic is correct.
