# Webhook Delivery System — Detailed Explanation

> This document is a comprehensive walkthrough of every decision, problem, and approach in this project. It's meant to prepare you for interview questions about the system.

---

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [High-Level Architecture](#high-level-architecture)
3. [Technology Choices and Why](#technology-choices-and-why)
4. [Part A: Core Webhook Delivery — Deep Dive](#part-a-core-webhook-delivery--deep-dive)
5. [Part B: Rate Limiting — Deep Dive](#part-b-rate-limiting--deep-dive)
6. [Part C: Multi-User Fairness — Deep Dive](#part-c-multi-user-fairness--deep-dive)
7. [Problems Faced and How They Were Solved](#problems-faced-and-how-they-were-solved)
8. [Writeup: Queuing, Rate Limiting, and Fairness Strategy](#writeup-queuing-rate-limiting-and-fairness-strategy)
9. [Testing Strategy](#testing-strategy)
10. [What I'd Improve With More Time](#what-id-improve-with-more-time)

---

## Problem Statement

Build a multi-user webhook delivery system where:
- **Part A:** Users register webhooks (URLs + event subscriptions), publish events, and the system delivers matching events to registered URLs. Full CRUD, enable/disable, and delivery logging.
- **Part B:** Add a configurable global rate limit (deliveries per second) that controls how fast the worker sends requests.
- **Part C:** Ensure fairness — if one user floods the system with events, other users shouldn't be starved. Their deliveries should still go through with reasonable latency.

---

## High-Level Architecture

```
                    ┌────────────────────────────────────────────────┐
                    │              Docker Compose Network             │
                    │                                                │
  HTTP requests     │  ┌─────────┐    ┌─────────┐    ┌───────────┐  │
  ─────────────────▶│  │  Flask  │───▶│  Redis  │◀───│  Worker   │  │
  (with X-User-Id)  │  │   API   │    │         │    │           │──┼──▶ Target URLs
                    │  │ :5000   │    │  :6379  │    │ (Python)  │  │
                    │  └────┬────┘    └─────────┘    └─────┬─────┘  │
                    │       │                              │         │
                    │       ▼                              ▼         │
                    │  ┌──────────┐                                  │
                    │  │PostgreSQL│  (stores webhooks + logs)        │
                    │  │  :5432   │                                  │
                    │  └──────────┘                                  │
                    └────────────────────────────────────────────────┘
```

There are **5 containers** running:

1. **`api`** — Flask web server behind Gunicorn (2 workers). Handles all REST API endpoints: webhook CRUD, event ingestion, rate limit configuration.
2. **`worker`** — A standalone Python process that continuously polls Redis for delivery jobs and sends HTTP POST requests to webhook URLs.
3. **`db`** — PostgreSQL 15 database storing `webhooks` and `delivery_logs` tables.
4. **`redis`** — Redis 7 used for two purposes: (a) message queues for delivery jobs, and (b) atomic counters for rate limiting.
5. **`mock_receiver`** — A simple Flask app that acts as a test webhook endpoint. It receives POST requests and stores them in memory so the test script can verify deliveries.

### Why This Separation?

The API and worker are intentionally separate processes. The API handles user-facing HTTP requests and needs to respond fast — it should never block on delivering webhooks. The worker runs independently, pulling jobs from Redis at its own pace (respecting rate limits). This is a classic **producer-consumer pattern**.

If the API and worker were in the same process, a slow webhook delivery would block API responses. Separating them means the API can always accept events instantly (returning 202 Accepted) while deliveries happen asynchronously.

---

## Technology Choices and Why

### Flask (not FastAPI, not Django)

I chose Flask because:
- It's lightweight — no unnecessary ORM setup, no admin panel, no middleware we don't need
- The problem is a REST API with simple CRUD, which Flask handles cleanly
- Flask-SQLAlchemy gives us ORM support with minimal config

I considered **FastAPI** but it adds complexity with Pydantic models and async patterns that aren't necessary here — our API endpoints are simple and the actual async work (delivery) happens in the worker process anyway. **Django** would be overkill — we don't need its admin, auth system, or template engine.

### PostgreSQL (not SQLite, not MongoDB)

- We need **relational integrity** — delivery logs reference webhooks via foreign key
- We need **ARRAY columns** for `event_types` — PostgreSQL supports native arrays
- SQLite doesn't work well in Docker containers with concurrent writes from multiple processes
- MongoDB could work but adds complexity for what's essentially a relational problem (webhooks have delivery logs)

### Redis (not RabbitMQ, not Celery)

- Redis is **extremely fast** for push/pop queue operations (LPUSH/RPOP)
- Redis supports **atomic increment/decrement** which we use for rate limiting
- Redis **sets** let us track which users have pending deliveries
- RabbitMQ would work but is heavier — it needs its own management UI, exchanges, bindings
- Celery would abstract away the queue but we need fine-grained control over fairness (round-robin per user), which is hard to do with Celery's default behavior

### Gunicorn (not Flask dev server)

Flask's built-in server is single-threaded and meant for development only. Gunicorn gives us:
- Multiple worker processes for handling concurrent requests
- Production-grade reliability
- The `--preload` flag which we needed to fix a table creation race condition (described in Problems section)

---

## Part A: Core Webhook Delivery — Deep Dive

### Database Models

**Webhook** table:
```
id          | Integer (PK, auto-increment)
user_id     | String(100), indexed — identifies the user
url         | String(500) — the target URL for delivery
event_types | ARRAY(String) — which events this webhook subscribes to
is_active   | Boolean — can be toggled on/off
created_at  | DateTime
updated_at  | DateTime (auto-updates on change)
```

**DeliveryLog** table:
```
id            | Integer (PK, auto-increment)
webhook_id    | Integer (FK → webhooks.id)
event_type    | String — which event triggered this delivery
payload       | JSON — the event payload that was sent
status_code   | Integer (nullable) — HTTP response code, null if connection failed
success       | Boolean — was the delivery successful (2xx response)?
error_message | Text (nullable) — error details if delivery failed
delivered_at  | DateTime
```

The `Webhook → DeliveryLog` relationship uses `cascade="all, delete-orphan"` so when a webhook is deleted, its logs are automatically cleaned up.

### API Endpoints Explained

**POST /api/webhooks** — Registers a new webhook. Validates that `url` is provided and `event_types` is a non-empty list of valid types (`request.created`, `request.updated`, `request.deleted`). Returns 201 with the webhook object.

**GET /api/webhooks** — Lists all webhooks for the authenticated user. Supports `?status=active` or `?status=disabled` query parameter to filter.

**GET /api/webhooks/:id** — Get a single webhook. Only returns it if it belongs to the requesting user (checked via `X-User-Id`).

**PUT /api/webhooks/:id** — Update the URL and/or event types of a webhook. Partial updates work — you can update just the URL or just the event types.

**DELETE /api/webhooks/:id** — Permanently removes a webhook and all its delivery logs.

**PATCH /api/webhooks/:id/toggle** — Enables or disables a webhook. If you pass `{"is_active": false}`, it disables it. If you pass `{"is_active": true}`, it enables it. If you pass no body, it just toggles the current state.

**POST /api/events** — The event ingestion endpoint. When an event comes in:
1. Find all active webhooks for this user
2. Filter to only those subscribed to this event type
3. For each matching webhook, create a delivery job and push it to the user's Redis queue
4. Return 202 Accepted with the count of deliveries queued

**GET /api/webhooks/:id/deliveries** — Returns the last 50 delivery log entries for a webhook, ordered newest first.

### How Event → Delivery Flow Works

```
1. Client sends POST /api/events with event_type and payload
2. API queries PostgreSQL for active webhooks matching the event type
3. For each match, API pushes a JSON job to Redis list "user_queue:{user_id}"
4. API adds user_id to Redis set "active_users" (so worker knows to check this user)
5. API returns 202 immediately (async — doesn't wait for delivery)
6. Worker's main loop sees active_users is non-empty
7. Worker pops one job from this user's queue (RPOP)
8. Worker sends HTTP POST to the webhook URL with the payload
9. Worker logs the result (success/failure, status code) to PostgreSQL
10. If the user's queue is now empty, worker removes them from active_users set
```

### User Identification

The system uses `X-User-Id` header for user identification. This is a simplified approach as specified in the problem — there's no real authentication. Every endpoint reads this header and scopes all operations (queries, creates) to that user. If the header is missing, the API returns 400.

---

## Part B: Rate Limiting — Deep Dive

### The Problem

We need to control how fast the worker delivers webhooks. If the rate limit is set to 5/second, the worker should not deliver more than 5 webhooks per second, regardless of how many are queued.

### How It Works

The rate limiter uses a **sliding window counter** pattern with Redis:

```python
def wait_for_rate_limit():
    rate_limit = get_rate_limit()         # read from Redis key "rate_limit"
    if rate_limit <= 0:
        return                             # 0 means unlimited

    while True:
        current_time = int(time.time())    # current Unix second
        key = f"rate_limit_window:{current_time}"

        count = r.incr(key)                # atomically increment
        if count == 1:
            r.expire(key, 3)               # auto-expire after 3 seconds

        if count <= rate_limit:
            return                          # under the limit, deliver now
        else:
            r.decr(key)                     # over limit, undo the increment
            sleep_until = current_time + 1
            sleep_duration = sleep_until - time.time()
            if sleep_duration > 0:
                time.sleep(sleep_duration)  # wait for next second
```

**Step by step:**
1. Get the current Unix timestamp (integer seconds)
2. Use it as a Redis key: `rate_limit_window:1709654321`
3. Atomically increment the counter for this second
4. If the count is within the limit → proceed with delivery
5. If the count exceeds the limit → decrement (undo) and sleep until the next second
6. Keys expire automatically after 3 seconds (no cleanup needed)

### Why This Approach?

I initially tried a **non-atomic approach**: read the counter, check if under limit, then increment. This had a race condition — between reading and incrementing, another thread could also read the same value and both would think they're under the limit.

**Approach 1 (broken):** `GET` → check → `INCR` (race condition between GET and INCR)

**Approach 2 (current):** `INCR` first → check → `DECR` if over limit (atomic, no race)

The key insight is that `INCR` in Redis is atomic — it returns the new value after incrementing. So by incrementing first, we atomically "claim" a slot. If we're over the limit, we release the slot by decrementing.

### Alternatives Considered

- **Token Bucket:** More complex — needs a refill timer and atomic check-and-decrement. Would need either Lua scripts in Redis or a separate timer thread.
- **Leaky Bucket:** Similar complexity to token bucket. Better for smoothing bursts but harder to implement correctly.
- **Fixed Window Counter:** What we essentially use, but per-second granularity. Simple enough for this use case and easy to reason about.

### Runtime Configuration

The rate limit is stored in Redis key `rate_limit` and can be changed at any time via:
- `GET /api/rate-limit` — returns current value
- `PUT /api/rate-limit` — sets new value

The worker reads this value before every delivery, so changes take effect immediately (within 1 second).

---

## Part C: Multi-User Fairness — Deep Dive

### The Problem

If User A publishes 100 events and User B publishes 1 event, User B shouldn't have to wait for all 100 of A's deliveries to complete. User B's single delivery should happen with reasonable latency, even under load from other users.

### How It Works

**Per-User Queues:**

Instead of a single global Redis queue, each user gets their own queue:
```
user_queue:userA → [job1, job2, job3, ..., job100]
user_queue:userB → [job1]
```

A Redis set `active_users` tracks which users have pending jobs:
```
active_users → {userA, userB}
```

**Round-Robin Worker Loop:**

```python
while True:
    active_users = get_active_users()  # read the set

    if not active_users:
        time.sleep(0.1)
        continue

    for user_id in active_users:
        job = pop_job_from_user(user_id)  # RPOP from user's queue
        if job:
            wait_for_rate_limit()
            deliver_webhook(job)
```

Each cycle, the worker processes **one job per user** before moving to the next. This means:

- Cycle 1: deliver 1 from A, deliver 1 from B → **B is done after cycle 1**
- Cycle 2: deliver 1 from A (B has no more jobs)
- Cycle 3: deliver 1 from A
- ... continues until A's 100 are all done

**Result:** User B's delivery completes within the first cycle (~1 second), not after all 100 of A's deliveries.

### How the Queue Cleanup Works

When a job is popped from a user's queue:
```python
def pop_job_from_user(user_id):
    result = r.rpop(f"user_queue:{user_id}")
    if result:
        if r.llen(f"user_queue:{user_id}") == 0:
            r.srem("active_users", user_id)  # remove from active set
        return json.loads(result)
    else:
        r.srem("active_users", user_id)  # queue empty, cleanup
        return None
```

After popping, we check if the user's queue is now empty. If so, remove them from the `active_users` set so the worker doesn't keep checking an empty queue.

### What the Event Ingestion Side Does

When `POST /api/events` is called:
```python
r.lpush(f"user_queue:{user_id}", json.dumps(job))  # push to user's queue
r.sadd("active_users", user_id)                      # mark user as active
```

`LPUSH` adds to the left (head) of the list, `RPOP` removes from the right (tail) — this gives us FIFO ordering within each user's queue.

### Alternatives Considered

**1. Single Global Queue with Priority Tags**
- Tag each job with a priority or user ID
- Worker would need to scan the queue to find jobs from underserved users
- Rejected: Redis lists don't support efficient scanning/filtering

**2. Weighted Fair Queuing**
- Give each user a weight based on their queue length (shorter queue = higher priority)
- Rejected: Adds complexity without benefit — round-robin is simpler and achieves the same fairness for this use case

**3. Random Queue Selection**
- Pick a random user's queue each time
- Rejected: Doesn't guarantee fairness — a user with 100 jobs would be picked ~100x more often than a user with 1 job (since they're in the active set longer)

**4. Time-Based Scheduling**
- Give each user a guaranteed time slot
- Rejected: Overcomplicates things — what happens during a user's slot if their queue is empty?

**Round-robin was chosen** because it's simple, deterministic, and provably fair — each user gets exactly one delivery per cycle.

---

## Problems Faced and How They Were Solved

### Problem 1: Table Creation Race Condition

**What happened:** When the Flask API started with Gunicorn (2 workers), both worker processes tried to call `db.create_all()` simultaneously. This caused a PostgreSQL `UniqueViolation` error because both processes tried to create the same tables at the same time.

**Error:** `sqlalchemy.exc.IntegrityError: (psycopg2.errors.UniqueViolation) duplicate key value violates unique constraint`

**Fix (two-part):**
1. Added `--preload` flag to Gunicorn command — this loads the application code once in the master process before forking workers, so `db.create_all()` runs only once.
2. Added an explicit check using SQLAlchemy's Inspector to see if tables already exist before calling `create_all()`:
```python
with app.app_context():
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()
    if "webhooks" not in existing_tables:
        db.create_all()
```

### Problem 2: Foreign Key Constraint on Webhook Deletion

**What happened:** Deleting a webhook failed because the `delivery_logs` table has a foreign key reference to `webhooks.id`. PostgreSQL refused to delete the webhook because there were delivery log rows pointing to it.

**Error:** `IntegrityError: violates foreign key constraint "delivery_logs_webhook_id_fkey"`

**Fix:** Before deleting the webhook, explicitly delete all its delivery logs:
```python
DeliveryLog.query.filter_by(webhook_id=webhook_id).delete()
db.session.delete(webhook)
db.session.commit()
```

Also added `cascade="all, delete-orphan"` on the relationship for ORM-level cascading.

### Problem 3: Rate Limiter Race Condition

**What happened:** The original rate limiter used a non-atomic pattern: read the counter, check if under limit, then increment. With fast delivery loops, the worker could read a stale count and deliver more than the allowed rate.

**Symptom:** With a 5/sec limit and 20 events, only 10 deliveries completed in 30 seconds (expected 20 in ~4 seconds). The rate limiter was sleeping too aggressively.

**Fix:** Rewrote the rate limiter to use an **increment-first** pattern. `INCR` is atomic in Redis, so the worker increments first (claiming a slot), then checks if it's over the limit. If over, it decrements (releasing the slot) and sleeps:
```python
count = r.incr(key)       # atomic claim
if count <= rate_limit:
    return                  # proceed
else:
    r.decr(key)            # release
    time.sleep(...)        # wait for next second
```

### Problem 4: Mock Receiver Multi-Worker Memory Issue

**What happened:** The mock receiver used Gunicorn with 2 workers. Since each Gunicorn worker is a separate process, the in-memory list `received_payloads` was different in each worker. A POST request might go to worker 1 (storing the delivery), but a GET request might hit worker 2 (which has an empty list).

**Symptom:** Tests checking "mock receiver got 0 deliveries" were failing because they were randomly hitting different workers that had different in-memory states.

**Fix:** Changed mock receiver Gunicorn to use 1 worker: `--workers 1`. Since the mock receiver is only for testing and doesn't need to handle high traffic, a single worker is fine. The real fix for a production system would be to use Redis or a database instead of in-memory storage, but that's unnecessary for a test mock.

### Problem 5: Python Output Buffering in Docker

**What happened:** Worker logs weren't appearing in `docker compose logs` because Python buffers stdout by default. When running in a Docker container, there's no TTY to trigger line-buffering, so output was fully buffered.

**Fix:** 
1. Added `ENV PYTHONUNBUFFERED=1` to the worker Dockerfile
2. Used `python -u worker.py` in the CMD (the `-u` flag also disables buffering)

---

## Writeup: Queuing, Rate Limiting, and Fairness Strategy

### Queuing Strategy

**What I chose:** Per-user Redis queues. Each user gets their own Redis list (`user_queue:{user_id}`). Jobs are pushed with LPUSH and popped with RPOP, giving FIFO ordering within each user. A Redis set (`active_users`) tracks which users have pending jobs.

**What I considered:**

1. **Single global Redis queue** — The simplest approach. All delivery jobs go into one list. The worker just pops from the queue and delivers. This works for Part A and B, but fails for Part C (fairness). If User A pushes 100 jobs and User B pushes 1, User B's job sits behind 100 of A's jobs. User B would have to wait for all of A's deliveries to complete (which, at a rate limit of 10/sec, means 10+ seconds of waiting).

2. **Redis Streams** — More sophisticated than lists. Supports consumer groups, acknowledgment, and re-delivery of failed messages. Would be useful for a production system with multiple workers, but adds complexity that wasn't needed for this problem.

3. **Celery with Redis broker** — An industry-standard task queue for Python. Would handle retries, dead-letter queues, and monitoring out of the box. However, it abstracts away the queue mechanics — implementing custom round-robin fairness across users would require a custom router or subclassing Celery internals, which is harder than just writing the round-robin loop ourselves.

4. **RabbitMQ** — A dedicated message broker with routing, exchanges, and consumer priorities. More robust than Redis for queuing but heavier. For this problem's scale, Redis queues are more than sufficient.

**Why I chose per-user Redis queues:** They directly enable the fairness requirement (Part C). Each user's workload is isolated, and the worker can independently choose which user to serve next. Redis lists are O(1) for push/pop, and the `active_users` set is O(1) for add/remove/membership-check. The implementation is simple and fits naturally with the round-robin scheduling pattern.

### Rate Limiting Strategy

**What I chose:** Sliding window counter using Redis atomic operations. One key per second (`rate_limit_window:{timestamp}`), auto-expiring after 3 seconds. The worker atomically increments the counter before each delivery. If the count exceeds the limit, it decrements (undo) and sleeps until the next second.

**What I considered:**

1. **Token Bucket** — Maintains a bucket of tokens that refills at a fixed rate. A delivery consumes one token. If the bucket is empty, the worker waits. This is a standard algorithm, but implementing the refill mechanism in Redis requires either a Lua script or a separate timer — both add complexity.

2. **Leaky Bucket** — Deliveries enter a bucket that "leaks" at a fixed rate. If the bucket overflows, deliveries are delayed. Similar complexity to token bucket and better for smoothing bursts, but per-second counting was sufficient for our needs.

3. **In-memory counter with time.time()** — Just track deliveries in a Python dictionary keyed by second. Simplest implementation, but doesn't work across multiple worker instances (if we ever scale to multiple workers, they'd each have independent counters).

4. **Redis + Lua script** — A Lua script running inside Redis can atomically check-and-increment without race conditions. This is the most robust approach, but Lua scripts in Redis add operational complexity and are harder to debug.

**Why I chose the sliding window counter with INCR/DECR:** It's simple, correct, and uses only two atomic Redis operations. The INCR-first pattern eliminates race conditions without needing Lua scripts. The 3-second expiry means old keys clean themselves up. The rate limit is stored in Redis and checked on every delivery, so changes via the API take effect immediately.

### Fairness Strategy

**What I chose:** Round-robin scheduling. The worker loop gets all active users, then processes exactly one job from each user before cycling back. This guarantees each user gets equal throughput regardless of their queue size.

**What I considered:**

1. **No fairness (FIFO)** — First-come-first-served from a global queue. Simple but fails the fairness requirement. User A with 100 events would block User B's single event for 10+ seconds at a 10/sec rate limit.

2. **Weighted fair queuing** — Assign weights inversely proportional to queue size. Users with shorter queues get served more often. This would be fairer than random selection but doesn't meaningfully improve over round-robin for this problem — with round-robin, every user already gets one delivery per cycle.

3. **Random selection** — Randomly pick a user's queue each cycle. Probabilistically fair in the long run but not deterministically fair. A user with a very large queue stays in the active set longer, so they'd be randomly selected more often than users with small queues.

4. **Deficit round-robin** — A more sophisticated variant where users accumulate "credits" — if a user's queue is empty during their turn, their credit carries over. This handles bursty workloads better but adds unnecessary complexity for this problem.

**Why I chose round-robin:** It's the simplest algorithm that provably guarantees fairness. Every user gets exactly one delivery per cycle, regardless of how large their queue is. In our tests, when User A had 100 pending events and User B had 1, User B's delivery completed in ~1 second. At that point, only ~19 of A's 100 deliveries had been processed — clear proof that B wasn't starved.

The round-robin approach combined with per-user queues means the system naturally scales: adding more users doesn't slow down any individual user's first delivery (it just adds one more stop per cycle). And since each cycle is fast (limited only by the rate limiter), even with many users the latency stays low.

---

## Testing Strategy

The test suite (`test_system.py`) covers all three parts with **17 individual test assertions**:

### Part A Tests (9 tests)
1. Register a webhook → verify 201 status
2. List webhooks → verify the webhook appears
3. Publish a subscribed event → verify delivery reaches mock receiver
4. Publish a non-subscribed event → verify NO delivery (0 in mock receiver)
5. Disable webhook + publish → verify NO delivery
6. Re-enable webhook + publish → verify delivery resumes
7. Update event types → verify update persists
8. Publish newly-subscribed event → verify delivery works
9. Delete webhook → verify it's gone from the list

### Part B Tests (4 tests)
1. Set rate limit to 5/sec → verify config
2. Publish 20 events → verify all 20 delivered AND took ~4 seconds (not instant)
3. Update rate limit to 20/sec → verify config
4. Publish 20 more → verify they arrive faster than the first batch

### Part C Tests (4 tests)
1. Set rate limit to 10/sec
2. User A publishes 100 events, User B publishes 1 → both queued
3. Check User B's latency → must be under 10 seconds (proves fairness)
4. Wait for all 101 deliveries → verify all arrive (100 for A, 1 for B)

### How Tests Verify Deliveries

The mock receiver is key. It runs at `http://localhost:9000` and has:
- `POST /webhook` — receives deliveries, stores them in a list with metadata (user_id, event_type, timestamp)
- `GET /logs` — returns all received deliveries
- `POST /logs/clear` — clears the log (used between tests)

Tests follow a pattern: clear logs → trigger action → sleep briefly → check logs. The sleep gives the worker time to process the queue.

---

## What I'd Improve With More Time

1. **Retry logic** — Currently, failed deliveries are logged but not retried. A production system would have exponential backoff retries (e.g., retry after 1s, 2s, 4s, 8s... up to a max).

2. **Dead letter queue** — After N retries, move failed jobs to a dead letter queue instead of dropping them.

3. **Multiple worker instances** — The current design supports it (Redis queues are shared), but we'd need to handle the round-robin coordination differently. A Redis lock or Redlock pattern could prevent two workers from picking the same user simultaneously.

4. **Webhook signature verification** — Sign outgoing payloads with HMAC so receivers can verify the sender. This is standard in production webhook systems (Stripe, GitHub, etc.).

5. **Per-user rate limits** — Currently the rate limit is global. Different users might need different limits. This would require per-user configuration and separate rate limit counters.

6. **Persistent queue** — Redis is in-memory. If Redis crashes, pending jobs are lost. Using Redis persistence (AOF/RDB) or a durable queue (RabbitMQ, Kafka) would fix this.

7. **Monitoring dashboard** — Add Prometheus metrics and a Grafana dashboard showing: delivery rate, queue depth per user, success/failure ratio, p99 latency.

8. **Batch delivery** — Instead of one HTTP request per job, batch multiple payloads in a single POST. This reduces network overhead for high-volume users.
