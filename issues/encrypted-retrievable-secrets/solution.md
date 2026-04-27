# Solution: Encrypted Retrievable Project Secrets

## Feature Summary

Added support for **retrievable project secrets**: users can now mark individual secret keys as
retrievable when storing them. The server will return ciphertext values (supplied by the caller)
for those keys via a new dedicated endpoint. All other keys remain non-retrievable — the existing
behaviour is unchanged.

MLRun acts as a dumb transport: it never inspects or encrypts the stored value. The caller is
responsible for encrypting before store and decrypting after retrieval (Option 1 /
caller-managed encryption). The retrievable-key set is tracked in an internal metadata entry
(`mlrun.retrievable-keys`) stored atomically in the same Kubernetes secret.

Option 2 (JWE / MLRun-managed encryption) was evaluated but deferred — see "What Was NOT Done"
below.

---

## Files Changed

| File | Change |
|---|---|
| `mlrun/common/schemas/secret.py` | Added `retrievable_keys` field to `SecretsData`; added new `RetrievableSecretsData` response model |
| `mlrun/common/schemas/__init__.py` | Exported `RetrievableSecretsData` |
| `server/py/services/api/crud/secrets.py` | Core logic: metadata tracking via `mlrun.retrievable-keys`, new `list_retrievable_project_secrets`, extended store/delete |
| `server/py/services/api/api/endpoints/project_secrets.py` | New `GET /projects/{project}/secrets/retrievable` endpoint with full auth |
| `mlrun/db/base.py` | Added `retrievable_keys` param to `create_project_secrets`; added abstract `get_project_retrievable_secrets` |
| `mlrun/db/httpdb.py` | Implemented `retrievable_keys` param forwarding and `get_project_retrievable_secrets` HTTP call |
| `mlrun/db/nopdb.py` | Stubs for `retrievable_keys` param and `get_project_retrievable_secrets` (no-op) |
| `server/py/framework/rundb/sqldb.py` | Stubs raising `NotImplementedError` (Kubernetes-only feature) |
| `server/py/services/api/tests/unit/crud/test_secrets.py` | 13 new unit tests covering all new code paths |

---

## How to Use

### Store a retrievable secret

```python
import mlrun

db = mlrun.get_run_db()

# Encrypt your value with any scheme before storing
ciphertext = my_encrypt("plaintext-value", my_key)

db.create_project_secrets(
    project="my-project",
    secrets={"MY_API_KEY": ciphertext},
    retrievable_keys=["MY_API_KEY"],
)
```

Via the project API:

```python
project = mlrun.get_or_create_project("my-project")
project.set_secrets(
    {"MY_API_KEY": ciphertext},
    retrievable_keys=["MY_API_KEY"],
)
```

### Retrieve a retrievable secret

```python
result = db.get_project_retrievable_secrets(
    project="my-project",
    secrets=["MY_API_KEY"],   # omit to retrieve all retrievable keys
)
# result.secrets == {"MY_API_KEY": "<ciphertext>"}

plaintext = my_decrypt(result.secrets["MY_API_KEY"], my_key)
```

### Behaviour notes

- Keys **not** marked as retrievable remain unretrievable — existing behaviour preserved.
- Requesting a key that was never marked retrievable raises `MLRunNotFoundError` (no information leakage about whether the key exists as a non-retrievable secret).
- `mlrun.*` internal keys are never exposed, even if somehow present in metadata.
- Delete operations keep the `mlrun.retrievable-keys` metadata consistent: per-key delete removes only that key from the list; delete-all removes the metadata key entirely.

---

## Tests Added

**13 new unit tests** in:
`server/py/services/api/tests/unit/crud/test_secrets.py`

Full suite now has **46 tests** (33 pre-existing + 13 new). All pass.

Test coverage:
- Store marks retrievable key in metadata
- Two store calls merge into a single metadata list
- Key in `retrievable_keys` but missing from `secrets` raises `MLRunInvalidArgumentError`
- `mlrun.*` key in `retrievable_keys` raises `MLRunAccessDeniedError`
- Retrieval returns only marked keys; non-retrievable keys excluded
- `secrets=[...]` filter parameter limits returned keys
- Non-existent key request raises `MLRunNotFoundError`
- Non-retrievable key request raises `MLRunNotFoundError` (no leakage)
- Stale metadata entries (key deleted externally) silently skipped
- Empty result when no keys are marked retrievable
- Per-key delete updates metadata correctly
- Delete-all removes `mlrun.retrievable-keys` metadata key
- Internal `mlrun.*` keys in metadata are never returned

---

## What Was NOT Done

**Option 2 / JWE (MLRun-Managed Encryption) — Deferred**

Option 2 would have MLRun encrypt values on behalf of the user using a project-scoped
AES-256 content-encryption key (CEK), itself wrapped by a user-supplied RSA/EC public key
(JWE, RFC 7516). It was evaluated in detail (see `problem.md`) but deferred for the following
reasons:

1. **MLRun would see plaintext on store** — the plaintext value passes through the API server,
   which is a weaker security posture than Option 1 (where MLRun never sees plaintext).
2. **New server dependency** — requires `authlib` (or `cryptography`) to be added to the
   server requirements.
3. **CEK rotation is complex** — rotating the user's key pair requires the server to re-encrypt
   all stored values, which either needs the old private key or results in permanent data loss.
4. **Higher implementation surface** — new endpoints, new schemas, CEK lifecycle management.

Option 2 can be layered on top of the current foundation in a follow-up: the same
`mlrun.retrievable-keys` metadata key and `GET /projects/{project}/secrets/retrievable`
endpoint are reusable, and the stored value format (ciphertext string) is compatible.
