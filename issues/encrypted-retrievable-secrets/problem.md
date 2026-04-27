# Problem: Encrypted Retrievable Project Secrets

## Summary

MLRun's project secrets mechanism stores values in Kubernetes Opaque secrets but intentionally
blocks value retrieval through the SDK and REST API (`allow_secrets_from_k8s=False` by default).
Users can only retrieve secret **keys**, not values. This is appropriate for confidential secrets
that should only be consumed by workloads running inside the cluster (injected as env vars), but
it prevents a valid use case: storing secrets that users deliberately want to retrieve through the
API, accepting the responsibility for keeping those values safe via encryption.

The request is to add support for **encrypted retrievable secrets**: secrets that users
pre-encrypt before storing (Option 1) or that MLRun encrypts on their behalf using a
user-provided public key (Option 2). In either case, the API returns the ciphertext, and only
the holder of the private/symmetric key can recover plaintext. The existing unencrypted-secret
storage path is preserved without change.

---

## Current State

### Storage

All project secrets are stored in a single K8s Opaque secret per project:

```
Secret name: mlrun-project-{project}
Data:        { "key1": base64(value1), "key2": base64(value2), ... }
```

Key files:
- `server/py/framework/utils/singletons/k8s.py` – `K8sHelper.store_project_secrets` / `get_project_secret_data`
- `server/py/services/api/crud/secrets.py` – `Secrets` CRUD class
- `server/py/services/api/api/endpoints/project_secrets.py` – REST endpoints
- `mlrun/db/httpdb.py` – SDK methods `create_project_secrets`, `list_project_secret_keys`, `list_project_secrets`
- `mlrun/common/schemas/secret.py` – `SecretsData`, `SecretKeysData`

### Existing API

| SDK method | What it returns |
|---|---|
| `create_project_secrets(project, secrets={k:v})` | Stores key-value pairs (write-only) |
| `list_project_secret_keys(project)` | Returns key names only |
| `list_project_secrets(project)` | **Blocked** – returns empty dict for k8s provider |
| `delete_project_secrets(project, secrets=[k])` | Deletes keys |

### Critical Constraint: No Per-Key Metadata in K8s Secrets

A K8s secret has labels and annotations at the **object level**, not per data key. The single
project secret for `my-project` is one K8s object. There is no native mechanism to attach
`is_encrypted=true` to the key `MY_API_KEY` without creating a second data entry or using the
object-level annotations.

### Internal Key Convention

MLRun already reserves keys prefixed with `mlrun.` for internal use (schedules, model-monitoring,
etc.), enforced in `Secrets.validate_internal_project_secret_key_allowed`. This convention is
available as a building block.

---

## Goals

1. Allow users to store secrets that can later be retrieved through the SDK/API.
2. Ensure that returned values are either:
   - (Option 1) Already encrypted by the caller — MLRun is a dumb transport.
   - (Option 2) Encrypted by MLRun using a project-scoped symmetric key that is itself wrapped
     by a user-provided asymmetric public key (JWE/hybrid encryption).
3. Preserve the existing unencrypted non-retrievable secret flow entirely.
4. Maintain K8s as the sole backend storage — no new DB tables.

---

## Option 1: Caller-Managed Encryption (Simple)

### Description

The user encrypts the secret value themselves (any algorithm: AES-GCM, RSA-OAEP, etc.) and
stores the resulting ciphertext string via the existing `create_project_secrets` API, adding a
flag that marks the key as retrievable. MLRun stores the ciphertext as-is and allows retrieval
of marked keys only.

### Tracking "Is This Key Retrievable"

Because K8s secrets have no per-key metadata, we need a side-channel to record which keys are
user-encrypted and therefore safe to expose via the API. Three approaches:

#### 1a. Internal Metadata Key (Recommended for Option 1)

Store a reserved internal key `mlrun.retrievable-keys` in the same K8s secret. Its value is a
JSON-encoded list of key names that are marked as retrievable:

```
mlrun.retrievable-keys  →  ["MY_API_KEY", "WEBHOOK_SECRET"]
```

- Gated by `allow_internal_secrets=True` on writes (server-internal only, not user-writable directly).
- Reads: the GET endpoint checks this list before returning any value.
- Pros: self-contained in K8s, no DB changes, consistent with existing internal-key pattern.
- Cons: the metadata key must be updated atomically with the secret; retry-on-conflict logic
  already exists in `K8sHelper.store_secrets_with_retry`.

#### 1b. Key Naming Convention

Reserve a prefix, e.g., `enc.`, that signals the value is user-encrypted and retrievable.
`enc.MY_API_KEY` is visible via key listing and retrievable; `MY_API_KEY` is not.

- Pros: zero extra storage; no atomic-update concern.
- Cons: leaks intent through key name; breaks existing key naming; users must rename keys;
  could collide with real key names.

#### 1c. K8s Object-Level Annotation

Encode the list of retrievable keys as a JSON annotation on the K8s secret object:
`mlrun.io/retrievable-keys: '["MY_API_KEY"]'`.

- Pros: clean separation of data and metadata.
- Cons: annotations are limited in length (256 KiB total per object, 63-char key name length
  limit for annotation keys). More importantly, updating an annotation requires a PATCH on the
  secret object, which is a separate Kubernetes API call — still needs atomic coordination but
  with a different API surface.

**Recommended for Option 1:** approach 1a (internal metadata key).

### API Changes

**Store:**
```python
project.set_secrets(
    {"MY_API_KEY": "<ciphertext>"},
    retrievable=True,   # new parameter
)
# or directly:
client.create_project_secrets(
    project,
    secrets={"MY_API_KEY": "<ciphertext>"},
    retrievable_keys=["MY_API_KEY"],
)
```

**Retrieve:**
```python
data = client.get_project_secrets(
    project,
    secrets=["MY_API_KEY"],
)
# → {"MY_API_KEY": "<ciphertext>"}
# User decrypts locally with their own key.
```

### Schema Changes

- `SecretsData`: add optional `retrievable_keys: list[str] = []`.
- New response model `RetrievableSecretsData` (or reuse `SecretsData`).

### New Endpoint

```
GET /projects/{project}/secrets/encrypted
```

Returns only keys that are in the `mlrun.retrievable-keys` list. Requires the same auth as
`list_project_secrets` but passes `allow_secrets_from_k8s=True` only for those keys.

Alternatively, extend the existing `GET /projects/{project}/secrets` endpoint with a query
parameter `?retrievable_only=true`.

### Security Model

- MLRun never sees plaintext — it receives and returns ciphertext.
- If an attacker gains K8s access, they get ciphertext only (assuming the user encrypted
  before storing).
- MLRun validates that only keys listed in `mlrun.retrievable-keys` are returned; other
  keys remain unretrievable.
- Users accept responsibility that the ciphertext is their own concern.

### Advantages

| + | Detail |
|---|---|
| Simplicity | No crypto in MLRun; no key management burden |
| Flexibility | User may choose any encryption scheme (symmetric, asymmetric, envelope) |
| No new dependencies | authlib / cryptography not required server-side |
| Auditable | User sees exactly what is stored and retrieved |

### Disadvantages

| - | Detail |
|---|---|
| UX burden | User must perform encryption/decryption themselves |
| No standard | Different users may use incompatible schemes; no SDK helper |
| Metadata race | Updating `mlrun.retrievable-keys` requires atomic K8s patch; needs care |
| No integrity check | MLRun cannot verify the stored value is actually encrypted |

---

## Option 2: MLRun-Managed JWE Hybrid Encryption (Complex)

### Description

MLRun manages a per-project **symmetric content-encryption key (CEK)**, itself wrapped with a
user-supplied **asymmetric public key**. This follows the JWE (JSON Web Encryption, RFC 7516)
standard. The user never sees the plaintext CEK; they only need their private key at
decryption time.

### Encryption Flow

```
┌─────────────┐           ┌─────────────────────────────────────────────┐
│   User      │           │               MLRun API Server               │
│             │           │                                               │
│ 1. Register │─(pub_key)→│ 2. Generate AES-256 CEK per project          │
│    public   │           │    Store CEK as JWE(pub_key, CEK)            │
│    key      │           │    in k8s secret: mlrun.jwe-cek              │
│             │           │                                               │
│ 3. Store    │─(k, v)───→│ 4. Load CEK, encrypt v with AES-256-GCM     │
│    secret   │           │    Store JWE compact token as secret value   │
│             │           │                                               │
│ 5. Retrieve │──────────→│ 6. Return JWE compact token                  │
│    secret   │           │                                               │
│             │           └─────────────────────────────────────────────┘
│ 7. Decrypt  │
│    with     │  authlib: jwe.decrypt(token, private_key)
│    priv_key │
└─────────────┘
```

### Key Storage

Two internal keys per project in `mlrun-project-{project}`:

```
mlrun.jwe-cek          →  JWE compact token wrapping the AES-256 CEK
                           (encrypted with user's RSA/EC public key)
mlrun.retrievable-keys →  JSON list of keys stored as JWE ciphertexts
```

The CEK itself is never stored in plaintext anywhere. Only its JWE-wrapped form lives in K8s.

### Decryption

MLRun cannot decrypt stored secrets — it would need the user's private key. The user:

1. Calls `GET /projects/{project}/secrets/encrypted?secret=MY_API_KEY`
2. Gets back a JWE compact token (e.g., `eyJhbGciOiJSU0EtT0FFUCIsImVuYyI6IkEyNTZHQ00ifQ...`)
3. Uses the MLRun SDK helper or authlib directly:

```python
from mlrun.secrets import decrypt_project_secret
plaintext = decrypt_project_secret(jwe_token, private_key_pem)
```

Or without MLRun:
```python
from authlib.jose import JsonWebEncryption
jwe = JsonWebEncryption()
data = jwe.deserialize_compact(token, private_key)
```

### API Changes

**Register public key (new endpoint):**
```python
client.set_project_encryption_key(project, public_key_pem)
# PUT /projects/{project}/secrets/encryption-key
```

**Store encrypted secret (existing endpoint, new path):**
```python
project.set_secrets({"MY_API_KEY": "my-plaintext"}, encrypt=True)
# API server encrypts value with CEK before storing
```

**Retrieve:**
```python
data = client.get_project_secrets(project, secrets=["MY_API_KEY"], encrypted=True)
# → {"MY_API_KEY": "<JWE compact token>"}
```

**SDK decrypt helper:**
```python
from mlrun.secrets import decrypt_project_secret
plaintext = decrypt_project_secret(data["MY_API_KEY"], private_key_pem)
```

### Supported Algorithms

Using `authlib` (already a Python ecosystem standard):

| Algorithm | Key type | Use case |
|---|---|---|
| `RSA-OAEP-256` | RSA 2048/4096 | Common, widely supported |
| `ECDH-ES+A256KW` | EC P-256/P-384 | Smaller keys, modern TLS-like |

Content encryption: `A256GCM` (AES-256 in GCM mode) for all stored values.

### Schema Changes

- New schema: `ProjectEncryptionKeyData(public_key_pem: str, algorithm: str = "RSA-OAEP-256")`
- `SecretsData`: add optional `encrypt: bool = False` field.

### New Endpoints

```
PUT    /projects/{project}/secrets/encryption-key   # register/replace public key
DELETE /projects/{project}/secrets/encryption-key   # remove public key + invalidate CEK
GET    /projects/{project}/secrets/encrypted        # retrieve JWE-encrypted values
```

### CEK Rotation

When the user rotates their key pair:
1. `DELETE /projects/{project}/secrets/encryption-key` (removes `mlrun.jwe-cek`).
2. `PUT /projects/{project}/secrets/encryption-key` with new public key.
3. Server re-wraps the CEK: it must **first decrypt all existing encrypted values** with the old
   CEK and re-encrypt with a new CEK wrapped by the new public key.
   - This requires the user to provide the **old private key** during rotation so the server can
     unwrap the old CEK — or the server generates a fresh CEK and the user loses the old secrets.
   - **Key rotation is the hardest operational concern for Option 2**.

### Security Model

- MLRun never stores or sees user private keys.
- MLRun stores the CEK only in JWE-wrapped form; without the user's private key it cannot
  decrypt stored secrets.
- If an attacker gains K8s access, they get JWE tokens. Without the user's private key, they
  cannot decrypt.
- If the MLRun API server is compromised during a `store` call, the plaintext value is visible
  **in transit** on the server. This is an inherent limitation vs. Option 1 (where MLRun never
  sees plaintext).

### Advantages

| + | Detail |
|---|---|
| Better UX | User provides only a public key; MLRun handles encryption transparently |
| Standard format | JWE (RFC 7516) is well-specified; interoperates with authlib, jose, etc. |
| SDK helper | MLRun can provide `decrypt_project_secret(token, priv_key)` for ergonomics |
| No user crypto code | User does not need to write encryption logic |

### Disadvantages

| - | Detail |
|---|---|
| MLRun sees plaintext on store | On `set_secrets(encrypt=True)`, plaintext value passes through API server |
| New dependency | `authlib` (or `cryptography`) must be added to server requirements |
| Key management complexity | CEK rotation is operationally non-trivial |
| New endpoints & schemas | More surface area to maintain and secure |
| Per-project CEK lifecycle | CEK must be generated, wrapped, stored, and rotated correctly |

---

## Comparison

| Concern | Option 1 (Caller-Encrypted) | Option 2 (JWE / MLRun-Encrypted) |
|---|---|---|
| Plaintext ever in MLRun | Never | Yes — on `store` call |
| User crypto burden | High (must encrypt before store) | Low (just provide public key) |
| MLRun crypto burden | Low (dumb store/retrieve) | High (key wrap, AES-GCM, JWE) |
| New dependencies | None | `authlib` or `cryptography` |
| K8s attack surface | Ciphertext stored | JWE tokens stored (still safe) |
| Key rotation | User's problem | Complex server-side re-encryption |
| Standard / interop | User-defined | JWE (RFC 7516) |
| Implementation effort | Low | High |
| Recommended for | Power users, security-first | End-to-end MLRun UX |

---

## Constraints

- K8s remains the only backend — no new DB tables.
- Internal key prefix `mlrun.` continues to gate metadata keys from direct user writes.
- The existing unencrypted secret path (`create_project_secrets` / `list_project_secret_keys`)
  is unchanged.
- Both options require a new GET endpoint (or extension of the existing one) that passes
  `allow_secrets_from_k8s=True` only for retrievable/encrypted keys.
- Auth/authz remains project-scoped via `AuthVerifier.query_project_resource_permissions`
  with `AuthorizationResourceTypes.secret`.

---

## Success Criteria

- [ ] Users can mark a secret as retrievable when storing it.
- [ ] The API returns ciphertext (Option 1) or JWE tokens (Option 2) for marked secrets only.
- [ ] Non-marked secrets remain unretrievable (existing behaviour preserved).
- [ ] Internal metadata keys (`mlrun.*`) are not exposed to users.
- [ ] Existing tests for `create_project_secrets` and `list_project_secret_keys` pass unchanged.
- [ ] New unit tests cover: marking as retrievable, retrieval of marked keys, rejection of
  unmarked keys, and (Option 2) JWE round-trip with authlib.

---

## Recommended Next Step

Proceed with **Option 1** first. It has minimal risk, no new server-side dependencies, and
delivers the core value (retrievable encrypted secrets) quickly. Option 2 can be layered on top
as an SDK convenience wrapper, reusing the same API extension, if the team decides the UX
improvement justifies the added complexity.
