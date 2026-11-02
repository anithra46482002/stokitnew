# Delivery Boy Authentication — Independent Multi-Device Sessions

## Root cause and architecture

Previously, `POST /api/delivery/auth/logout/` wrote `AdminCredentialState.tokens_valid_after` for the entire `DELIVERY_BOY` principal. `_validate_token()` compared every token's signing timestamp against that account-wide marker. Logging out on one phone therefore invalidated every older access and refresh token for that Delivery Boy. Password change and password reset used the same account-wide marker, which is the desired security behavior for those events.

Delivery Boy authentication now uses a dedicated, persisted session row for every successful login. Each login generates a fresh UUID session ID (`jti`), a distinct signed access token, and a distinct signed refresh token. Both tokens carry the same session ID, principal type, account ID, token type, issue time and expiry. A random per-token nonce ensures a refresh rotation produces a genuinely new token even within the same second.

The signed token format remains Django signing (`stokit.delivery-token`), not a standards-based JWT. Access and refresh validation checks signature, signing age, explicit expiry, principal type, role, token type, UUID session ID, session owner, active/revoked state, account active metadata, and (for refresh) the stored SHA-256 refresh-token digest. The access token never trusts a client-supplied account ID.

## Database and migration

A new model `accounts.DeliveryBoyAuthSession` maps to `accounts_deliveryboyauthsession`. `delivery_boy_id` is a numeric reference to the existing unmanaged `legacydb.delivery_boys` identity; no foreign-key constraint is added to the legacy table. Session IDs and refresh-token hashes are unique. The table stores optional device name/platform/identifier, request IP and user-agent, created/last-used timestamps, access/refresh expiry, active state and revocation timestamp. It stores no raw access or refresh token.

Migration `accounts.0005_delivery_boy_auth_sessions` is generated and inspected using `sqlmigrate`. It creates only the new session table and its indexes. **It was applied to `stokit_postgres_dev.public` on 2026-10-30 after explicit authorization.** The session table and migration record were verified. Existing migrations remain unchanged; no OTP tables or Delivery Boy account records are duplicated or altered.

## Endpoints

All paths are under `/api/`:

| Method | Path | Auth | Behavior |
|---|---|---|---|
| POST | `delivery/auth/login/` | Public | Creates an independent session; optional `device_name`, `device_platform`, `device_identifier` |
| POST | `delivery/auth/refresh/` | Refresh token in body | Validates and rotates only that session's refresh token; returns new access and refresh tokens |
| POST | `delivery/auth/logout/` | Access token | Revokes only the current access token's session; no refresh token or client-supplied account ID required |
| GET | `delivery/auth/sessions/` | Access token | Lists only the authenticated Delivery Boy's active sessions and marks the current session |
| POST | `delivery/auth/sessions/revoke/` | Access token | Revokes only the supplied session ID when it belongs to the authenticated Delivery Boy |
| GET | `delivery/auth/profile/` | Access token | Existing authenticated profile |
| GET | `delivery/profile/image/` | Access token | Existing authenticated profile image |

Refresh request body: `{ "refresh": "<refresh-token>" }`. The old refresh token is invalid immediately after successful rotation. Other session refresh tokens are unaffected. Refresh keeps the original absolute refresh expiry (7 days from login); it does not extend the lifetime indefinitely. Access expiry is 30 minutes and is renewed by refresh.

Logout revokes only `request.user.session_id`. Other sessions for the same account stay active. The optional session-revoke endpoint enforces owner filtering in the database query, so another Delivery Boy's session cannot be revoked by guessing its UUID.

## Password security and concurrency

A successful OTP-backed password change or reset updates the legacy Delivery Boy password hash and revokes all active session rows for that Delivery Boy in the same transaction. Existing OTP purposes, HMAC storage, expiry, resend cooldown, attempt limit, rate limiting, and password hashing are unchanged.

Login locks the legacy Delivery Boy row while checking the password and creating a session. Password completion locks that same legacy row before changing the password and revoking sessions. This serializes login against password change/reset: an old-password login cannot create a surviving session after the password-change transaction completes. Refresh, logout and per-session revoke lock the relevant session row to serialize token rotation and revocation.

Admin and Customer authentication code and their existing session behavior are not changed. The shared AdminCredentialState remains available for Admin authentication; Delivery Boy token validation no longer uses its account-wide marker.

## Postman test sequence

1. POST `/api/delivery/auth/login/` with the same Delivery Boy username/password and `{ "device_name": "Android A", "device_platform": "Android", "device_identifier": "install-A" }`. Save `access_A`, `refresh_A`, and `session_id_A`.
2. Repeat login with `device_name: "iPhone B"` and another identifier. Save `access_B`, `refresh_B`, and `session_id_B`. Verify the session IDs differ.
3. GET `/api/delivery/auth/profile/` with `Bearer access_A`, then `Bearer access_B`; both should return 200.
4. GET `/api/delivery/auth/sessions/` using either access token; only that Delivery Boy's sessions should be listed.
5. POST `/api/delivery/auth/logout/` with `Bearer access_A` and an empty JSON object. A subsequent profile request with A should return 401; B should still return 200.
6. POST `/api/delivery/auth/refresh/` with `{ "refresh": "refresh_B" }`. Save both returned tokens. Reusing the old `refresh_B` must fail; the rotated refresh token must work. This must not affect other device sessions.
7. Optionally POST `/api/delivery/auth/sessions/revoke/` with `Bearer access_B` and `{ "session_id": "<another-owned-session-uuid>" }`; that session alone is revoked. A UUID belonging to another Delivery Boy must return 404.
8. Complete the OTP-backed password change or forgot-password reset. All sessions for that Delivery Boy must then fail, while unrelated Delivery Boy accounts remain unaffected.

Use a controlled test account and do not paste real passwords or bearer tokens into logs or reports.

## Verification status

Focused Delivery Boy authentication tests and the existing Admin/Customer authentication regression suites are run separately and reported in the task completion summary. The new session migration remains pending approval. No React, Flutter or legacy PHP files are changed. No commit, push, deployment, database reset or SQL import is performed.
