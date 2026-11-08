# Design note — Errand Agent (shipped as *Sorted*)

**Provider world.** The 8 tools run against a seeded SQLite marketplace (`mock_env.py`): 367 providers across 6 services × 12 Bangalore areas. Each has price, rating and review count, jobs done, experience, working hours and days off, plus reviews (one per area carries a prompt injection). The contract odds and failure modes are unchanged. An admin dashboard edits the same tables, so changes reach the next tool call. Evals use an in-memory copy, so they stay reproducible.

**Shape.** A single agent loop (`agent.py`). The LLM (via LiteLLM) reads the conversation and picks tools. Code owns the consequential parts: phase, approvals, idempotency, side-effect verification, memory gating, limits, and the status lines users see. More agents wouldn't make the hard problems (enforcement and correctness) any easier.

**State model.** `WorkflowState` is a small state machine: `idle → gathering → searching → choosing → awaiting_approval → executing → completed | failed`. The model never sets the phase. Phases change only on events the code observes, against an allow-list, and every transition is traced. `ErrandRequest` holds the slots and where each value came from (user / memory / event). Updates are merges, so "actually make it Thursday" changes only the date. Code computes `missing`, and the model asks only for those fields. State is saved to SQLite after every step.

**Approval enforcement.**
1. `request_approval` builds a Proposal from the code's own records: the hold the provider returned, or the booking from `list_bookings`, never the model's arguments. It mints an idempotency key, and the code renders the exact text the user sees.
2. Approval comes from a deterministic parser over the next user message. A bare affirmative or `confirm <id>` counts, and only in reply to the turn that showed the proposal. Anything with "but", numbers, negations or a modification does not.
3. `book`, `cancel` and `reschedule` are wrapped. They run only if an approved proposal's material fields (provider, slot, price / booking, new slot) equal the current facts. Any difference invalidates the approval and records the diff.
4. Re-holding the same provider, slot and price is allowed because it isn't a material change. A different re-quote voids the approval and asks the user again.

Reviews, hostile users and compromised models can't produce an approval. The evals check this with a model that deliberately obeys injections.

**Idempotency and recovery.** The proposal row is the write-ahead ledger: `pending → approved → executing → executed | failed`. It is marked `executing` before the first provider call. On a timeout, 5xx, exception or malformed reply, the executor checks `get_booking_status(idempotency_key)` first. Applied means success. Not applied means a bounded retry with the same key. Unreachable means *unknown*, never success. On startup, any proposal still `executing` is resolved from the source of truth: a crash after the side effect becomes a verified booking, and a crash before it becomes "nothing was booked". Repeated "yes" or `book()` calls are answered from the ledger. `✅ Verified` lines come only from executor results, and a claim guard rewrites unverified success claims from the model.

**Memory.** Each fact carries `{key, value, scope (global or service), source explicit|inferred, confirmed, status active|superseded|forgotten}`. An explicit write needs an evidence quote that appears in the user's own messages, so text from a review can't become a memory. Code infers facts (for example, the last completed technician) as *unconfirmed*, and they're excluded until the user confirms them, again with a quote. Correcting a fact supersedes the old one. Forgetting tombstones the record, wipes the value and scrubs it from stored history.

**Context.** `ContextBuilder` works under a fixed token budget, in priority order: rules and tools, then state and notes, then the current turn (older tool results get elided), then gated memories (capped at 15%), then as much history as still fits. Memories reach the model only if they're active, explicit-or-confirmed, and relevant (global preference keys, or scope equal to the current service). Each build writes a manifest to the trace: tokens per section, memory included/excluded *with reason*, and history included vs available. Tests replay the trace and fail if a forgotten or unconfirmed id ever appears.

**Limits and observability.** Each run caps model calls, tool calls, wall-clock time and consecutive tool errors. Every exit gets a code-written outcome: what happened, what changed, and the saved progress. The trace is JSONL grouped by `run_id`: context manifests, model calls (latency, tokens, cost), model- and provider-level tool calls (args, sanitized result, latency), transitions, approvals, executions, recovery, memory, limits.

**Trace excerpt — timeout after success** (`docs/traces/timeout_after_success.jsonl`, abridged):
```
approval        approved PFE3F6  user_text="yes"                  ← parser, not model, grants approval
state           awaiting_approval → executing
execution       executing  idempotency_key=idem-book-734c…         ← ledger written before the call
tool_call       book(H-0001, idem-book-734c…) → {"error":"timeout"}
recovery        timeout → query get_booking_status before deciding ← no blind retry
tool_call       get_booking_status(idem-book-734c…) → applied BK-1004
execution       executed  verified_by=get_booking_status note="timeout after success"
run_end         "✅ Verified: booking BK-1004 confirmed — QuickPipe, Mon 09 Nov, 10:00, ₹990"
```

**Trade-offs.** The approval parser is strict: "yeah go for it at that time" re-prompts. A false negative costs one turn; a false positive could book something unwanted. Token estimates are chars/4, which keeps evals deterministic; live runs also log the API's real counts.
