# Handoff criteria

The agent does the repetitive, deterministic part of the sale: it collects data, quotes
and explains the quote. It hands off whenever going on would need judgement, negotiation
or authority, or would risk a wrong answer such as an invented price.

Single source of truth: [`src/agent/handoff_policy.py`](../src/agent/handoff_policy.py).
Each handoff stores a ticket (`handoffs` table) with `reason`, `detail`, a masked
`summary` of everything already collected (so the seller never asks again), and the
`trace_id`.

| # | Trigger | Reason code | Why a human |
|---|---|---|---|
| 1 | Lead explicitly asks for a person ("atendente", "falar com vendedor", "me liga") — in **any** state | `customer_request` | Respecting the request matters more than finishing the flow. This intent comes from deterministic rules and an LLM can never override it. |
| 2 | Lead **accepts** a quoted plan | `plan_accepted` | Policy issuance, payment and the legal steps are out of the agent's authority. This is a warm handoff: the seller receives the quote and the profile. |
| 3 | `/quote` unavailable after the retry policy, or circuit open | `quote_unavailable` | We never show a price we did not get from the quote API. The lead is told honestly. A background worker keeps retrying, and if the API recovers before a seller picks the ticket up, the lead gets the quote automatically and the ticket is closed as `auto_requoted`. |
| 4 | Underwriting refusal: pre-screened from `/planos` rules (vehicle > 20 years, driver > 75) or HTTP 422 | `underwriting_refusal` | The automatic product cannot cover the lead. A human can offer alternatives, so the lead is not just dropped. |
| 5 | Price objection **after** a cheaper plan was already offered, or when the lead is on the cheapest plan | `negotiation` | Discounts and deductible adjustments, which is what sellers do in the dataset, are commercial decisions. The agent offers the cheaper plan once and then hands off. |
| 6 | `MAX_STALLED_TURNS` (default 3) consecutive turns without progress | `collection_stalled` | The bot is not understanding the lead. Looping is worse than a handoff. |
| 7 | Claims, accidents, cancellation, billing, complaints, other insurance products | `out_of_scope` | These are not sales conversations and need specialists. |
| 8 | Our own request rejected (HTTP 400) or an unparseable success response | `system_error` | This is a bug on our side. We fail safe: no price is shown, and we alert. |

## What does NOT trigger a handoff

- **A single failed or slow `/quote` call.** It is retried with backoff and jitter within a
  12 s budget, and the lead is told "just a moment" once.
- **Invalid data** (bad CEP, impossible age). The agent explains what is wrong and asks
  again. Only repeated failures count as a stall.
- **Media messages** (audio, image, document). The agent says it cannot open attachments
  and asks for text. These do not count as stalls.
- **The LLM being down or rate limited.** Extraction falls back to deterministic rules.

## After the handoff

The conversation stays in `HANDOFF`. Further lead messages are stored, so the seller sees
them, and acknowledged once each. The agent does not keep collecting data or quoting,
except for the `quote_unavailable` auto-recovery described above.
