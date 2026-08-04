# AI Chatbot with Memory

> A stateful conversational API — conversation history persisted in DynamoDB, inference
> through the Claude API, served by async FastAPI. Built to explore what changes when a
> chatbot has to remember things.

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![DynamoDB](https://img.shields.io/badge/DynamoDB-persistence-4053D6?logo=amazondynamodb&logoColor=white)](https://aws.amazon.com/dynamodb)
[![Claude](https://img.shields.io/badge/Claude_API-inference-D4A27F)](https://anthropic.com)

---

## The problem this solves

A stateless chatbot treats every message as the first one. That is fine for a demo and
useless for anything real — support, tutoring, advice — where the third question only
makes sense in light of the first two.

Holding history in process memory is the easy answer and the wrong one: it dies with the
process, it cannot be load-balanced across instances, and it caps how many users one
server can hold. Once conversation state has to survive a restart and be readable from any
instance, it stops being an application problem and becomes an infrastructure problem.

This project is that infrastructure problem, kept small enough to read in one sitting.

---

## How it works

```
Client
  │  POST /chat  { message, user_id }
  ▼
FastAPI (async)
  │
  ├──▶ DynamoDB — read conversation history for user_id
  │
  ├──▶ Claude API — send history + new message, get completion
  │
  ├──▶ DynamoDB — append both turns, refresh TTL
  ▼
Response
```

**Data model** — table `chatbot-conversations`, partition key `user_id`:

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

Partitioning on `user_id` is the decision that matters. It spreads load evenly, keeps every
read for one conversation on a single partition, and lets DynamoDB split as the table grows
without the application knowing. A monotonic key — a timestamp, say — would funnel every
write into one hot partition and throttle under exactly the load you built it for.

`ttl` means abandoned conversations expire on their own instead of accumulating forever.

Full design notes, failure modes and capacity maths: **[ARCHITECTURE.md](ARCHITECTURE.md)**

---

## Running it

```bash
git clone https://github.com/sadvi11/ai-chatbot-with-memory.git
cd ai-chatbot-with-memory
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # add your ANTHROPIC_API_KEY
python3 setup_dynamodb.py     # creates the table
python3 main.py
```

Send a message, then ask a follow-up that only works if it remembered:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is machine learning?", "user_id": "user_123"}'

curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Give me an example of that.", "user_id": "user_123"}'

curl http://localhost:8000/conversation/user_123
```

The second call has no subject of its own. If it answers sensibly, the memory layer works.

With Docker:

```bash
docker compose up
```

---

## Design decisions

**Async FastAPI, not Flask.** Nearly all of a request's wall-clock time is spent waiting on
two network calls — DynamoDB, then Claude. Async lets one worker hold many in-flight
requests during that wait instead of blocking a thread per request. The workload is
I/O-bound, which is where the concurrency actually comes from.

**DynamoDB, not Postgres.** Access is a single-key lookup by `user_id`, no joins, no ad-hoc
querying. That is the shape DynamoDB is built for, and on-demand billing means an idle
project costs cents rather than a running instance.

**History is trimmed before it is sent.** Every turn makes the next prompt longer, costing
both latency and tokens. Unbounded history degrades quietly — it never errors, it just gets
slower and more expensive until someone looks at the bill.

**Latency budget** — a design target, not a benchmark. The Claude leg dominates and varies
with prompt length and model:

| Stage | Budget |
|---|---|
| FastAPI overhead | ~5 ms |
| DynamoDB read | ~100 ms |
| Claude inference | 200–300 ms |
| DynamoDB write | ~50 ms |

Writing the budget down is what tells you where optimization is worth doing. Shaving the
FastAPI overhead is pointless; caching the read is not.

---

## Failure modes considered

| Failure | Handling |
|---|---|
| Claude API timeout or rate limit | Request timeout, graceful fallback response, retry with backoff |
| DynamoDB latency or throttling | Circuit breaker; DAX or Redis is the next step if reads become the bottleneck |
| Runaway conversation length | History trimmed before the prompt is assembled |
| Concurrent writes to one conversation | Atomic append with optimistic locking |

---

## Tests

```bash
pytest test_chatbot.py -v      # unit
python3 example_client.py      # end-to-end against a running server
```

---

## Author

**Sadhvi Sharma** — Cloud & AI Engineer
Nokia (5G Packet Core) → Cloud & AI Engineering
Calgary, AB, Canada · Permanent Resident · open to relocation

[LinkedIn](https://www.linkedin.com/in/sadhvi-sharma-5789a6249/) · [GitHub](https://github.com/sadvi11)
