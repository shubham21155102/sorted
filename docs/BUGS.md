# Real bugs hit while building

Each of these showed up in the eval run or a trace while I was building, not in a planned test. Each now has a regression test in `tests/test_regressions.py`, and the related eval scenario also covers it.

## 1. Every provider-cancellation event crashed the event poller
- **What broke:** `proactive_provider_cancellation` and `proactive_ignores_past_booking` failed with `TypeError: Tracer.emit() got multiple values for argument 'event'`. No proactive follow-up ever ran.
- **Why:** `process_events` called `tracer.start_run("proactive_event", event=ev)`. `start_run` forwards `**kw` to `emit(self, event, **kw)`, so the keyword `event` collided with `emit`'s own positional parameter. The server's poller catches exceptions, so in the demo the failure would have been silent: the user would never hear that their provider cancelled.
- **Fix:** pass the event payload as `trigger=ev`.
- **Guard:** `test_bug1_proactive_event_does_not_crash_poller` (and the two proactive eval scenarios).

## 2. The tool-call cap didn't stop the run
- **What broke:** `limit_tool_calls_loop` reported "I hit my **step** limit" (the model-call cap) instead of the tool-call cap. With `max_tool_calls=5, max_model_calls=20`, a looping model made 20 model calls.
- **Why:** when the cap was hit, the loop returned `tool_call_limit_reached` as an error tool_result and `continue`d. The model just tried again, so the run burned model calls (and money, live) until a different limit tripped, and the user was told the wrong reason.
- **Fix:** record the `LimitExceeded`, still return tool_results for this batch so the transcript stays valid, then raise to end the run with the correct reason.
- **Guard:** `test_bug2_tool_call_limit_ends_the_run`, which asserts that only a `tool_calls` limit event fires and that the model was called exactly 4 times.

## 3. The context budget was exceeded by 30 tokens
- **What broke:** `context_budget_long_history` failed with `context 4030 > budget 4000`.
- **Why:** history messages were admitted using each message's own estimate (+4 overhead). Afterwards, adjacent same-role messages are merged (the API needs alternating roles), and the merged JSON serialises to a different size. The "fixed" budget was only approximate, which matters because the assignment asks us to *prove* the cap.
- **Fix:** after merging, recompute the real total and drop the oldest history messages until it fits.
- **Guard:** `test_bug3_context_total_never_exceeds_budget_after_merge`, which sweeps budgets from 3,000 to 6,000 over 300 seeded messages, plus the global `context within budget` invariant that runs in every scenario.

## 4. The trace sanitizer redacted dates as phone numbers (bonus)
- **What broke:** while reading the timeout-after-success trace for the design note, every slot read `"[phone]T09:00"`. The trace could no longer show which slot was approved or booked.
- **Why:** the PII pattern `\+?\d[\d\-\s]{8,}\d` also matches ISO dates like `2026-11-09`.
- **Fix:** match only international-prefixed numbers or bare 10-digit mobiles.
- **Guard:** `test_bug4_trace_sanitizer_keeps_dates`.
