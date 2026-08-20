# Local Role-Based Portal Contract

**Status:** verified by controlled local runtime on 2026-08-19

**Contract version:** v1

## Purpose

This checkpoint makes the accepted local workflow usable through server-rendered
HTML without adding a frontend framework or duplicating domain rules. It covers
password login, signed session logout, role-filtered case lists and details, and
all 6 accepted human-decision form shapes.

## Authentication and Session

Login verifies an active fictional user with PostgreSQL `crypt` against the
existing one-way password hash. The application never reads or returns the hash.
Pre-authentication forms require a short-lived HttpOnly nonce and an HMAC CSRF
token. Only `/cases` and `/requests/new` are accepted post-login redirects.

The successful 8-hour local session is signed with the runtime secret and stored
in an HttpOnly, SameSite cookie. Logout is POST-only and requires a session-bound
CSRF token. HTTPS-only cookie enforcement, rate limiting, password reset, MFA,
and real identity-provider integration remain production boundaries.

## Role and Object Access

- requesters see their own cases;
- service agents see the `NEEDS_REVIEW` queue plus cases they own as requesters;
- approvers see cases assigned to them plus cases they own as requesters; and
- administrators see all local cases.

The same object-level rule protects both list and detail access. Subjects,
messages, event reasons, identifiers, and user labels are escaped before HTML
rendering.

## Human Actions

The UI exposes only actions allowed by the current role, assignment, state, and
case version: requester information; service-agent confirmation, structured
correction, or rejection; and assigned-approver approval or rejection. Every
form has a fresh command UUID and session-bound HMAC CSRF token. The POST handler
constructs the strict existing `HumanDecisionCommand` and calls the verified
`execute_human_decision` service; it does not implement a second state machine.

## Evidence Gate

Exactly 1 concise disposable runner covers 6 groups:

1. password verification, login CSRF, redirect allowlist, and cookie flags;
2. requester, service-agent, approver, and object-level visibility;
3. requester-information form and action CSRF;
4. all service-agent review choices and a committed confirmation;
5. assigned-approver choices and a committed approval; and
6. administrator visibility, HTML escaping, logout, aggregate evidence, and AI
   isolation.

The runner used 4 fictional users, 5 fictional cases, temporary secrets,
disposable PostgreSQL, the current FastAPI image, 0 AI calls, and no paid or
hosted service. It assigned a test-only password inside the disposable database;
the repository seed hashes remained intentionally unusable. All 6 groups passed,
with 4 role-filtered views, 3 committed human actions, 0 unauthorized case
disclosures, and cleanup `PASS`.

## Deferred Boundaries

The full end-to-end integration and locked 50-case evaluation were completed
through separate contracts. The recovery-and-operations checkpoint verified
the local admin dashboard and scheduled expired-claim sweep. Deployment, real
users, and production authentication remain outside the local v1 evidence.
