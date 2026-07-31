# Mandate Guard by Rakesh — working agreements

**SPEC.md is the authority; stop and ask before any deviation from it.**

**Commit at each completed component** (end of spike, compiler, gate, ledger, storefront, executor, wired happy path) **and pause for review at each Tier boundary.**

## Working agreements (non-negotiable)

- **Fenced Generation:** LLM calls exist only in the mandate compiler, agent rationale, and export narrative. The gate, executor, ledger, and every payload builder are deterministic and unit-tested. Session payloads are built ONLY from gate-verified proposals.
- **Money = decimal strings, 2dp, everywhere.** No floats touch a price.
- **Secrets:** `.env` only; full token + dynamic CVV never persisted, logged, screenshotted, or committed; ledger stores last-4 + expiry + txn_ref_id. Test card numbers never appear in the repo or README.
- **Commits:** small, imperative, no trailers of any kind.
- **Every gate rule lands with its failing test first** (test list in SPEC.md §3).
- When judged behavior is ambiguous, ask me — judges may request the repo, logs, and transaction evidence, and I must be able to explain every line.
