# Mandate Guard (by Rakesh) — Build Spec v1.0

**Event:** Agentic Commerce Hackathon (Prava × Devfolio) · Build window: Fri Jul 31, 9:00 PM CT → Sun Aug 2, 5:00 PM CT (hard stop, confirmed)
**Builder:** Shiv (solo, pairing with Claude Code) · **Method:** Fenced Generation — LLMs write language and judgment only; deterministic code owns all logic, money, and verification.

**One-line pitch:** Mandate Guard decides *whether* an agent may pay; Prava enforces *how* it pays; the hash-chained ledger proves both happened — and exports the evidence packet an issuer, merchant, or finance team consumes when an agent purchase is contested.

---

## 0. Architecture summary

```
User request (NL)
  → Mandate Compiler   [LLM extracts → code validates → user confirms → hash]
  → Shopping Agent     [OpenAI-powered; proposes transaction; never pays]
  → Verification Gate  [pure functions; PASS/FAIL per named rule]
  → (PASS) Prava session → passkey → poll → one-time credentials
  → Checkout Executor  [pre-submit re-verification → Playwright fills storefront checkout]
  → report-status      [APPROVED/DECLINED → visa_confirmation]
  → Evidence Ledger    [append-only, hash-chained, every stage]
  → Evidence Export    [JSON + PDF; deterministic facts + labeled LLM narrative]
```

Two verification points: the **gate** before any Prava call, and the **executor pre-check** before the card number is ever typed. A FAIL at the gate means no session is ever created.

---

## 1. Mandate schema v1

```json
{
  "mandate_id": "uuid4",
  "version": "1.0",
  "created_at": "ISO-8601 UTC",
  "user": { "user_id": "string", "user_email": "string" },
  "intent_text": "verbatim natural-language request",
  "constraints": {
    "merchant": {
      "name": "string",
      "url": "https://... (https required by Prava)",
      "country_code_iso2": "US"
    },
    "items": [
      {
        "product_id": "string (exact-match key, v1)",
        "description": "string",
        "max_unit_price": "decimal string, 2dp",
        "quantity": 1
      }
    ],
    "price_ceiling_total": "decimal string, 2dp",
    "currency": "USD",
    "effective_minutes": 15,
    "substitution_policy": "none"
  },
  "status": "draft | confirmed | consumed | expired | revoked",
  "confirmed_at": "ISO-8601 | null",
  "mandate_hash": "sha256 hex"
}
```

Rules:
- All money values are **decimal strings, 2dp** end to end (mirrors Prava's API; never floats).
- `mandate_hash` = SHA-256 over the **canonical JSON** of `{user, intent_text, constraints, created_at}` — sorted keys, UTF-8, no insignificant whitespace. Mutable fields (`status`, `confirmed_at`) are excluded.
- The compiler is two steps: (1) LLM (small OpenAI model) extracts a draft `constraints` object from `intent_text`; (2) deterministic validator enforces schema, normalizes (currency code, URL host, decimal strings), rejects on any missing/invalid field. The user confirms the rendered mandate card; only then is the hash computed and `status → confirmed`.
- v1.1 stretch fields (do NOT build until Tier 1): `substitution_policy: "same_product_line"`, fuzzy descriptor matching with a fixed deterministic similarity threshold.

## 2. Proposed Transaction schema (Shopping Agent output)

```json
{
  "proposal_id": "uuid4",
  "mandate_id": "uuid4",
  "created_at": "ISO-8601 UTC",
  "merchant": { "name": "", "url": "" },
  "line_items": [
    { "product_id": "", "description": "", "unit_price": "2dp string", "quantity": 1 }
  ],
  "proposed_total": "2dp string",
  "agent_meta": { "model": "", "rationale": "1-2 sentences" }
}
```

The agent proposes; it holds no credentials and cannot call Prava.

## 3. Verification Gate — rule set

Pure functions. Each returns `{ rule_id, name, pass, expected, actual }`. Verdict = PASS iff all rules pass; FAIL lists every failing rule (do not short-circuit — full results go to the ledger and the UI).

| Rule | Name | Check |
|---|---|---|
| R1 | merchant_match | Normalized host of `proposal.merchant.url` == mandate merchant host; names equal after Visa-safe sanitization (strip non-alphanumerics, e.g. `H&M → HM`) |
| R2 | item_identity | Every proposal `product_id` ∈ mandate item `product_id`s; no extra line items |
| R3 | unit_price_cap | Each proposal `unit_price` ≤ matching mandate `max_unit_price` |
| R4 | quantity_exact | Each proposal `quantity` == mandate `quantity` (exact in v1) |
| R5 | total_ceiling | `proposed_total` ≤ `price_ceiling_total` **and** `proposed_total` == Σ(unit_price × quantity) — internal-consistency check catches hidden fees. (Demo storefront charges no tax; tax/shipping handling is a documented v1 limitation.) |
| R6 | window_valid | now < `created_at + effective_minutes` and `status == confirmed` |
| R7 | single_use | `status != consumed` (and not revoked/expired) |

**Executor pre-check (second gate, inside Checkout Executor, before card entry):**
- E1: storefront page total == Prava session `total_amount`
- E2: storefront host == mandate merchant host
- E3: order line items on page match verified proposal
Any E-failure → abort before card entry, ledger `EXECUTION_PRECHECK` fail event, report nothing to Prava (no token used → no report needed; if token was already entered/attempted, report DECLINED per Prava docs).

**pytest cases (minimum):** happy pass · price drift (unit price above cap) · total above ceiling · hidden-fee mismatch (total ≠ Σ lines) · merchant swap · substituted product_id · extra line item · quantity inflation · expired window · unconfirmed mandate · consumed mandate reuse · executor E1 mismatch.

## 4. Evidence Ledger

Append-only table (Supabase Postgres). Event shape:

```json
{
  "event_id": "uuid4",
  "ts": "ISO-8601 UTC",
  "mandate_id": "uuid4",
  "type": "see taxonomy",
  "payload": { },
  "prev_hash": "sha256 hex",
  "event_hash": "sha256(canonical(event minus event_hash) + prev_hash)"
}
```

Genesis: first event's `prev_hash` = `mandate_hash`.

**Event taxonomy (in causal order):**
1. `MANDATE_CREATED` — full mandate JSON + hash
2. `MANDATE_CONFIRMED` — user confirmation ts
3. `AGENT_PROPOSAL` — full proposal + agent_meta
4. `GATE_VERDICT` — per-rule results array + verdict
5. `GATE_BLOCKED` — (fail path terminal event; no session follows)
6. `SESSION_CREATED` — session_id, order_id, expires_at, `external_order_ref` (= mandate_hash), full `purchase_context` echo
7. `APPROVAL_OBSERVED` — poll transition into `awaiting_result` (implies passkey approval) + ts
8. `CREDENTIALS_RECEIVED` — txn_ref_id, token **last 4 only**, expiry_month/year. **Never the full token or dynamic CVV.**
9. `EXECUTION_PRECHECK` — E1–E3 results
10. `CHECKOUT_EXECUTED` — storefront order number, confirmation screenshot file path + screenshot SHA-256
11. `STATUS_REPORTED` — txn_status, authorization_code, response_code, `visa_confirmation`
12. `MANDATE_CONSUMED` / `MANDATE_EXPIRED`

**Verify-chain endpoint + UI button:** recomputes every hash live and renders green/red per link. This is a demo moment — build it early in Tier 1.

## 5. Prava API mapping (sandbox: `https://sandbox.api.prava.space`)

**Integration mode: Hosted (full API).** Zero frontend for the payment surface; card entry lives entirely on Prava's hosted page — a real security argument for a trust product (our system never renders a card form). The spike follows Prava's official **REST Checkout Walkthrough** (`/guides/rest-checkout-walkthrough.md`) verbatim, with auth per `/authentication.md`. Heads-up from `/concepts/checkout-flow.md`: first checkout includes verification steps a first-timer doesn't expect — passkey/device binding **and an issuer test OTP** (sandbox OTP is on `/api-reference/test-cards.md`).

**POST /v1/sessions** ← built ONLY from the gate-verified proposal, never from raw agent output:
- `user_id`, `user_email` ← mandate.user
- `total_amount` ← verified `proposed_total` (2dp string) — becomes the authorized amount cap
- `currency` ← mandate
- `purchase_context[0].merchant_details` ← mandate merchant (name, https url, country_code_iso2)
- `purchase_context[0].product_details[]` ← verified line items (description, unit_price, product_id, quantity)
- `purchase_context[0].effective_until_minutes` ← mandate `effective_minutes`
- `external_order_ref` ← **mandate_hash** (64 hex chars, fits 255 limit) — ties Prava's records to the evidence chain
- `integration_type: "full_checkout"` (hosted redirect; simplest solo path)
- **Omit `callback_url`** (optional per docs) — rely on polling
- Errors to handle: `VAL_2001` (surface field details), `429 TRIES_EXHAUSTED` (sandbox quota — back off, alert loudly). Quota size/reset is unpublished; if hit, email support@prava.space with the sanitized error, timestamp + timezone, environment, and `X-Response-ID` — they reply within minutes and can reset the allowance. All routine iteration runs against `fake_prava/` precisely to avoid this.

Flow: open `iframe_url` in the user's browser → user selects sandbox Visa test card → real WebAuthn passkey prompt.

**GET /v1/sessions/{id}/payment-result** — poll every 2s, back off to 5s, 3-minute timeout:
- Status machine: `pending → awaiting_result → completed | failed`
- Credentials (`token`, `dynamic_cvv`, expiry) appear **only while `awaiting_result`** — grab from `transactions[].line_items[]`, hold in memory only
- Log each status transition as a ledger event; `error {code,message}` present on failure

**POST /v1/sessions/{id}/report-status** — ALWAYS after any credential use:
- `txn_ref_id` ← from payment-result line item
- `txn_status`: `APPROVED` on storefront success; `DECLINED` if a token was used but checkout failed (Prava docs mandate this)
- `authorization_code` ← generated by the storefront's mock processor; `response_code`: `"00"` approved / `"05"` declined
- Record `visa_confirmation` (SUCCESS/FAILURE) in the ledger — the card-network acknowledgment closes the chain
- One-time mandate is consumed on APPROVED → ledger `MANDATE_CONSUMED`

**Full doc index for Claude Code:** `https://docs.prava.space/llms.txt`

## 6. Demo storefront spec ("Beanline Coffee" or similar — deliberately boring)

- 3 products with stable `product_id`s (one is the mandate target), single catalog page, cart, guest checkout form (card number, expiry, CVV, name)
- Mock processor endpoint: validates card format (Luhn + Visa prefix), returns `authorization_code` + `response_code`, persists order, renders a **confirmation page with an order number** — this page is the completed-checkout proof the handbook demands; executor screenshots it
- **Drift toggle (admin route, visible on camera):** perturbs state honestly for the fail demo — e.g. raises a price server-side or swaps the product the agent is steered toward. Disclosed on screen as a simulated agent/merchant error injection. Never fake the agent's output silently.
- Storefront is disclosed plainly in the submission: sandbox test cards cannot clear a real merchant's processor, so the merchant is self-hosted; every Prava-side step (session, passkey, token issuance, report-status, visa_confirmation) is real sandbox end to end.
- **CONFIRMED by Prava support (Birdie, Jul 29):** sandbox cards must be executed against a test merchant, and a self-built, clearly disclosed storefront is explicitly acceptable — "it will not be penalized as a 'mocked transaction.'" Screenshot of the Discord exchange is attached to the submission as pre-cleared rules evidence.

## 7. Checkout Executor

- Playwright (headed for the demo recording): navigate storefront → add verified items → run E1–E3 pre-check against the rendered page → fill card form with in-memory credentials → submit → wait for confirmation → capture order number + full-page screenshot → SHA-256 the screenshot → ledger `CHECKOUT_EXECUTED` → then report-status
- Redact any Playwright traces/videos of the card-entry step, or disable tracing on that step entirely

## 8. Evidence Export

- **JSON**: the full event chain, verbatim
- **PDF** (Playwright print-to-PDF, same pattern as the profile generator — pattern reused, code written in-window): cover summary → mandate card → proposal → gate verdict table → Prava session scope (+ external_order_ref) → approval + credential events (masked) → checkout proof (order no., screenshot) → report-status + visa_confirmation → chain-verification result
- Fields annotated as **"mapped to the evidence categories issuers evaluate under frameworks like Visa CE 3.0"** — never claim it IS a CE 3.0 packet
- One **LLM-written plain-English narrative section**, clearly labeled "Narrative (AI-generated summary of the deterministic record below)" — OpenAI track depth

## 9. Stack & repo layout

- Backend: **FastAPI** (Python) — compiler, gate, ledger, Prava client, executor orchestration
- Frontend: **Next.js/React** (Claude Code builds) — three screens that must look good: mandate card, gate verdict panel (named checks, green/red), ledger browser + export button. Everything else minimal.
- DB: **Supabase Postgres** (mandates, proposals, ledger, storefront orders)
- **fake_prava/** dev server mirroring the three endpoints — all local iteration runs against it to protect the `TRIES_EXHAUSTED` sandbox quota; real sandbox runs are deliberate integration checkpoints only; the fake NEVER appears in the demo or video
- OpenAI credit allocation: small model for mandate parsing · stronger model for agent shopping reasoning · narrative section in export
- Secrets in `.env` only; sandbox test card numbers never in the repo or README; no commit trailers

```
mandate-guard/
  backend/   (fastapi: compiler/, gate/, ledger/, prava_client/, executor/)
  frontend/  (next.js)
  storefront/ (fastapi or next route group + mock processor)
  fake_prava/
  tests/     (gate rules, hash chain, payload mapping)
  SPEC.md    (this file)  ·  DISCLOSURE.md  ·  README.md
```

## 10. Tiered build plan (hard stop: Sun 5:00 PM CT; submit by 3:00 PM CT)

**Tier 0 — demo spine (Fri 9 PM → Sat ~2 PM). Nothing else starts first.**
1. Fri night spike: real sandbox loop with hardcoded values — session → open iframe_url → test card + passkey → poll → credentials → manual/scripted card entry into a stub page → report-status → `visa_confirmation: SUCCESS`. Prove the seam.
2. Sat AM: mandate compiler (+ confirm UI + hash), gate (+ pytest), ledger writes, storefront v1, executor, happy path wired end to end.

**Tier 1 — the real app (Sat PM → Sun ~11 AM), in this order, cut from the bottom:**
3. Drift path + drift toggle · 4. Evidence export JSON + PDF · 5. Ledger browser + live verify-chain button · 6. Mandate lifecycle dashboard (active/consumed/expired) · 7. Failure handling as first-class UX (declined card → report DECLINED path, expired mandate) · 8. Substitution policy + fuzzy matching · 9. PDF polish

**Tier 2 — stretch only if Tier 1 is green:**
- **(a) Real-merchant declined-run** (Prava team confirmed, Jul 30: a test-card transaction at a live merchant will decline at checkout — expected sandbox behavior and acceptable to demo). Full flow against a real guest-checkout merchant → processor declines → `report-status: DECLINED` → ledger records the complete attempted transaction. Proves the executor generalizes beyond the demo store and exercises the failure path on real rails. Limit to 1–2 attempts (unpublished quota + merchant courtesy). DECLINED does not consume the one-time mandate, so the mandate stays active afterward.
- **(b) Production access request** (dashboard + email support@prava.space, Aug 1 morning, include project name) only if a real *completed* purchase is wanted for the video — bonus, never a dependency.
- **(c) Standing-mandate mode** (only if far ahead of schedule): Prava exposes standing mandates (`/concepts/mandates.md`, `mandate-charge`, `mandate-list`, `mandate-report`) — approve once by passkey, agent charges later **with no passkey per charge**. That is the regime where Mandate Guard matters most: the gate + ledger become the only per-charge control. Gate every `mandate-charge` the same way sessions are gated. Mention this in the submission narrative regardless of whether it's built.

**Sun 11 AM → 3 PM CT:** demo video, README, DISCLOSURE.md, submission form, buffer. Submit two hours early. Non-negotiable.

## 11. Demo script (3:00)

- **0:00–0:20** — Problem: agents can spend money; nothing proves what they were *allowed* to buy when it's contested.
- **0:20–1:20** — Act 1, happy path: NL request → mandate card confirmed → agent shops Beanline → gate: 7 green checks → Prava passkey prompt on camera (Touch ID) → executor fills checkout → **order confirmation on screen** → visa_confirmation SUCCESS in ledger.
- **1:20–2:10** — Act 2, drift: flip the visible drift toggle → agent proposes the perturbed cart → gate throws red, names the failed rule → **no Prava session is ever created**. The applause line.
- **2:10–2:45** — Act 3, artifact: open ledger browser → click verify chain (all links green) → export the evidence PDF, scroll it.
- **2:45–3:00** — B2B closer: "Everything you watched was recorded in a hash-chained ledger. This export is what a finance team, a merchant, or a card issuer consumes when an agent purchase is contested. Everyone else is teaching agents to spend money; Mandate Guard is built for the day a purchase gets contested."
- **Bonus footage (Tier 2a, separate clip or README link — do not squeeze into the 3:00):** the real-merchant declined-run, framed as "same system, real merchant, sandbox card declines as expected — and the ledger documents the attempted transaction end to end, which is exactly what this product is for."

## 12. Security rules (hard)

- Full token + dynamic CVV: memory only. Never in ledger, DB, logs, repo, traces, or the video frame.
- Ledger stores masked token (last 4) + expiry + txn_ref_id only.
- Submission includes one line stating this credential-handling policy (two Visa judges will notice).

## 13. Disclosure text (draft for DISCLOSURE.md)

> Pre-existing: the Fenced Generation methodology (deterministic-first LLM architecture) and the Rakesh venture thesis (agent-purchase dispute/mandate-evidence infrastructure) predate the event as ideas and public write-ups. All code in this repository was written during the official build window. AI development tools used: Claude Code (pair development) and OpenAI APIs (in-product: mandate parsing, shopping-agent reasoning, evidence narrative). The demo merchant is a self-hosted storefront because sandbox-issued test credentials cannot clear a real merchant's processor; all Prava-side steps (session, passkey approval, credential issuance, report-status, Visa confirmation) run live against the Prava sandbox.

## 14. Positioning FAQ (for README + judge Q&A)

- **"Prava already has Guardrails — why Mandate Guard?"** Prava's Guardrails (`/concepts/guardrails.md`) are account/agent-level spend controls enforced at payment time. Mandate Guard verifies **per-purchase intent fidelity** — this specific cart against this user's confirmed mandate — *before* a session exists, and produces the exportable dispute-evidence artifact afterward. Complementary layers: their controls constrain spend; ours proves authorization.
- **"Prava's Browser Harness already confirms the true total before charging."** It does — at charge time, against the order. Mandate Guard verifies against the **user's mandate** (intent), upstream of session creation, and records the entire chain as evidence. Our executor pre-check mirroring their harness is convergent design — say so openly.
- **"Won't AP2 solve this?"** AP2 specifies mandate data formats but requires ecosystem-wide adoption and ships no adjudication artifact. Mandate Guard works today on card rails via Prava; when AP2 matures, the ledger maps onto its mandate objects — an evidence layer on top of the protocol, not a competitor to it.

## 15. Open items (resolve before Friday)

- [ ] **Prava dashboard account + sandbox API keys — TODAY**
- [x] Birdie answers (Jul 29): sandbox requires test merchant ✓ · self-built disclosed storefront explicitly acceptable ✓ · TRIES_EXHAUSTED quota unpublished, support resets via email within minutes ✓ · production access: request via dashboard + email support@prava.space from registered email with project name ✓
- [ ] Screenshot the Birdie Discord exchange → save for DISCLOSURE.md / submission
- [ ] Tier 2 only: submit production-access request Aug 1 morning (dashboard + email, include project name "Mandate Guard by Rakesh") so unknown review time runs in parallel — treat as bonus footage, never a dependency
- [ ] Passkey enrolled on demo machine (Mac Touch ID; pick Chrome or Safari and test WebAuthn once)
- [ ] Pick storefront name + 3 products (with product_ids) — cosmetic, 10 minutes
- [ ] Tracks to enter on Devfolio: Open, OpenAI, Visa, Localhost (skip Linq, NANDA, Senso)