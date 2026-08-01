# Disclosure — Mandate Guard by Rakesh

**Event:** Agentic Commerce Hackathon (Prava × Devfolio)
**Official build window opens:** Fri Jul 31, 2026, 7:00 PM PT

This document states plainly what existed before the build window opened, what is
pre-existing intellectual work, and what is being built inside the window. It is
written before kickoff, deliberately, so the record is not reconstructed after the
fact.

## 1. Pre-event work in this repository

The following was committed **before** the official 7:00 PM PT build window and is
**not** offered as judged build-window work — plus local tooling config
(`CLAUDE.md`, since untracked):

| Path | What it is |
|---|---|
| `SPEC.md` | Build specification — architecture, schemas, rule set, tiered plan. Planning, no product code. |
| `evidence/` | Rules-clearance evidence (Prava support confirmations on sandbox/test-merchant policy). |
| `spike/spike_checkout.py` | Sandbox validation script — proves the Prava REST checkout seam end to end with hardcoded values. |

This pre-event activity is what the event handbook's own timeline instructs
participants to do ahead of kickoff — **"test your merchant or sandbox"** before the
event. `spike/spike_checkout.py` is exactly that: a sandbox connectivity and
integration test against `sandbox.api.prava.space`, using hardcoded values and no
product logic. It contains no mandate compiler, no verification gate, no ledger, no
storefront, and no user interface — none of the system that constitutes the
submission.

**Proof of what preceded kickoff.** Git commit timestamps, in the event's local
timezone (America/Los_Angeles), all of them hours before the 7:00 PM PT window:

```
f240550  2026-07-31 10:34:32 PDT  Add build spec, gitignore, and rules-clearance evidence
5c21503  2026-07-31 10:35:41 PDT  Populate build spec and evidence
64fb368  2026-07-31 10:46:06 PDT  Add working agreements for pair development
0cf059c  2026-07-31 10:46:06 PDT  Patch SPEC per vendor doc reconciliation
c44de02  2026-07-31 11:06:52 PDT  Add Prava sandbox checkout spike
35ab97f  2026-07-31 11:09:26 PDT  Patch SPEC for attempt-scoped external_order_ref and spike findings
8821320  2026-07-31 11:12:08 PDT  Bound payment-result polling by session expires_at
```

Every commit after `8821320` is build-window work. The boundary is auditable in the
commit history without relying on this document's say-so.

Four Prava sandbox sessions were created during this pre-event validation. No real
money moved; sandbox runs the card network in test mode.

## 2. Pre-existing ideas (predate the event)

- **Fenced Generation** — the deterministic-first LLM architecture methodology
  (LLMs write language and judgment only; deterministic code owns all logic, money,
  and verification). Pre-existing as an idea and a public write-up.
- **The Rakesh venture thesis** — agent-purchase dispute and mandate-evidence
  infrastructure. Pre-existing as an idea and a public write-up.

These are ideas and prior writing, not code. No pre-existing implementation of
either is being submitted.

## 3. Build-window work

**All product code is written inside the official window, starting 7:00 PM PT
Fri Jul 31, 2026.** That is everything the submission actually is:

- `backend/` — mandate compiler, verification gate, evidence ledger, Prava client, executor orchestration
- `frontend/` — mandate card, gate verdict panel, ledger browser and export
- `storefront/` — demo merchant and its mock processor
- `fake_prava/` — local dev server mirroring the Prava endpoints
- `tests/` — gate rules, hash chain, payload mapping

## 4. AI development tools

- **Claude Code** — pair development throughout (pre-event planning and in-window build).
- **OpenAI APIs** — in-product, not as a coding tool: mandate parsing, shopping-agent
  reasoning, and the evidence-export narrative section.

## 5. Demo merchant

The demo merchant is a **self-hosted storefront**, disclosed openly. Sandbox-issued
test credentials cannot clear a real merchant's processor, so a test merchant is
required. Prava support confirmed (Jul 29) that a self-built, clearly disclosed
storefront is explicitly acceptable and "will not be penalized as a 'mocked
transaction.'" That exchange is captured in `evidence/`.

Every Prava-side step is real sandbox, end to end: session creation, passkey
approval, credential issuance, `report-status`, and the returned Visa confirmation.

### Declared origin mapping

Prava requires `merchant_details.url` to use **https** and forwards it to Visa as
the merchant of record, so the mandate carries the canonical
`https://beanline.example.com`. The demo storefront is self-hosted and actually
served from a local origin (e.g. `http://127.0.0.1:8200`).

Rather than weaken the executor's pre-check or put a false URL in the mandate, the
substitution is explicit: a configured mapping declares which origin serves a given
merchant, and pre-check **E2** verifies the observed page host against that
*declared origin*. The mapping redirects the comparison; it does not relax it — a
page served from any other host still fails E2, and there is a test for exactly
that case.

All three values — the canonical merchant URL, the declared origin, and the
observed host — are written into every `EXECUTION_PRECHECK` ledger event, so the
mapping is disclosed inside the evidence record itself rather than living only in
configuration.

## 6. Credential handling

Full payment tokens and dynamic CVVs exist in memory only. They are never persisted,
logged, screenshotted, committed, or shown on camera. The evidence ledger stores the
token's last 4 digits, its expiry, and the transaction reference id — nothing more.
Sandbox test card numbers do not appear anywhere in this repository.
