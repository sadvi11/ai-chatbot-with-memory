# Architecture — AI Chatbot with Memory

Design notes for the stateful conversation layer: where time goes, how it scales, what
breaks first, and what it costs.

---

## Request path

```
CLIENT
  ▼
FastAPI (async)
  ▼
DynamoDB — read history for user_id
  ▼
Claude API — inference over history + new message
  ▼
DynamoDB — append turns, refresh TTL
  ▼
RESPONSE
```

### Latency budget

These are design targets, not measurements. They exist to answer one question — *where is
optimization actually worth doing* — and the answer is visible at a glance: the inference
call and the read dominate, and everything else is noise.

| Stage | Budget |
|---|---|
| Receive and validate request | ~2 ms |
| DynamoDB query (history) | ~100 ms |
| Claude API inference | 200–300 ms |
| DynamoDB write (append) | ~50 ms |
| Serialize and return | ~4 ms |

Roughly 360 ms end to end, with the model call the single largest term. Optimizing the
FastAPI layer buys nothing; caching the read or shortening the prompt is where the time is.

---

## Data model

Table `chatbot-conversations`, partition key `user_id`:

```json
{
  "user_id": "user_123",
  "messages": [
    {"role": "user",      "content": "What is ML?",          "timestamp": "..."},
    {"role": "assistant", "content": "Machine learning is…", "timestamp": "..."}
  ],
  "created_at": "...",
  "last_updated": "...",
  "ttl": 1718981400
}
```

### Why `user_id` is the partition key

DynamoDB distributes items across partitions by hashing the partition key, so the key
choice decides whether load spreads or concentrates.

`user_id` is high-cardinality and evenly distributed, which means:

- writes spread across partitions instead of landing on one
- every read for a conversation is a single-partition lookup — no scatter-gather
- DynamoDB splits partitions automatically as the table grows past 10 GB

A monotonically increasing key such as a timestamp would do the opposite: every concurrent
write lands on the newest partition, creating a hot partition that throttles under the load
the system was built to handle. This is the classic NoSQL key-design mistake, and it only
shows up under concurrency — never in testing.

### TTL

Conversations expire on a TTL rather than living forever. Storage that only grows is a slow
leak: it never fails, it just gets more expensive, and nobody notices until the bill does.

---

## Scaling

Worked example — 1M daily active users at 10 requests each:

| Quantity | Calculation | Result |
|---|---|---|
| Reads/day | 1M × 10 × 2 | 20M |
| Writes/day | 1M × 10 × 1 | 10M |
| Sustained reads | 20M ÷ 86,400 | ~231/sec |
| Sustained writes | 10M ÷ 86,400 | ~116/sec |

Both are comfortably inside what on-demand DynamoDB absorbs without provisioning. The
constraint at that scale is not the database — it is the inference API's rate limits and
cost, which is the usual shape for LLM-backed systems.

### If it needed to go further

1. **Read cache (Redis or DAX)** — conversation history is read far more often than written; caching removes the read leg from the hot path
2. **Multi-region** — DynamoDB global tables, inference in-region, to cut round-trip latency
3. **Smaller model for simple turns** — route by complexity rather than sending everything to the largest model
4. **Compress stored messages** — meaningful storage reduction for a small CPU cost
5. **Batch writes** — trade a little latency for throughput

---

## Failure modes

| # | Failure | Symptom | Mitigation |
|---|---|---|---|
| 1 | Claude API timeout or rate limit | Requests hang, then fail | Hard timeout, retry with exponential backoff, graceful fallback response |
| 2 | DynamoDB throttling | `ProvisionedThroughputExceeded` | On-demand billing, circuit breaker, cache layer |
| 3 | Runaway conversation length | Latency and cost climb steadily | Trim history before assembling the prompt |
| 4 | Concurrent writes, same conversation | Lost turns | Atomic append with optimistic locking |
| 5 | Process restart | — | No state in process memory, so nothing is lost |

Failure 3 is the one worth naming, because it does not announce itself. There is no error
and no alarm — the system just gets slower and more expensive every turn. Bounding history
is the fix, and it has to be deliberate.

---

## Monitoring

Metrics that matter:

- request latency at P50 / P95 / P99 — the mean hides the users having a bad time
- error rate by cause, separating model failures from database failures
- DynamoDB consumed capacity and throttle count
- token usage per request, as the cost driver
- conversation length distribution — the early warning for failure mode 3

Alarms:

| Condition | Why |
|---|---|
| P99 latency > 1000 ms | User-visible slowness |
| Error rate > 1% | Something upstream is degraded |
| Any DynamoDB throttling | Capacity or key-design problem |
| Inference API failure rate rising | Rate limit or outage |

---

## Cost

At ~10K requests/day:

| Component | Monthly |
|---|---|
| DynamoDB (on-demand) | $5–10 |
| CloudWatch | ~$2 |
| Claude API | $100–200 |

Inference is the cost, by roughly an order of magnitude. That is worth stating plainly,
because it means cost optimization work belongs in prompt size and model choice — not in
the infrastructure, where it is tempting to look first.
