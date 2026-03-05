# Webhook Delivery System — Detailed Explanation

> This document is a comprehensive walkthrough of every decision, problem, and approach in this project. It's meant to prepare you for interview questions about the system.

---

## Table of Contents

1. [Glossary — Key Concepts Explained](#glossary--key-concepts-explained)
2. [Problem Statement — What Is Actually Being Asked](#problem-statement--what-is-actually-being-asked)
3. [High-Level Architecture](#high-level-architecture)
4. [How Docker Compose Orchestrates Everything](#how-docker-compose-orchestrates-everything)
5. [Technology Choices and Why](#technology-choices-and-why)
6. [Part A: Core Webhook Delivery — Deep Dive](#part-a-core-webhook-delivery--deep-dive)
7. [API Request/Response Examples (Every Endpoint)](#api-requestresponse-examples-every-endpoint)
8. [Part B: Rate Limiting — Deep Dive](#part-b-rate-limiting--deep-dive)
9. [Part C: Multi-User Fairness — Deep Dive](#part-c-multi-user-fairness--deep-dive)
10. [Edge Cases and What Happens](#edge-cases-and-what-happens)
11. [Problems Faced and How They Were Solved](#problems-faced-and-how-they-were-solved)
12. [Writeup: Queuing, Rate Limiting, and Fairness Strategy](#writeup-queuing-rate-limiting-and-fairness-strategy)
13. [Testing Strategy](#testing-strategy)
14. [Actual Test Output (Proof It Works)](#actual-test-output-proof-it-works)
15. [What I'd Improve With More Time](#what-id-improve-with-more-time)

---

## Glossary — Key Concepts Explained

Before diving in, here's every concept you need to understand:

### What Is a Webhook?

A webhook is a URL that an application registers to receive **automatic notifications** when something happens. Instead of constantly polling an API asking "did anything change?", a webhook **pushes** data to you the moment something happens.

**Real-world analogy:** Instead of refreshing your email inbox every 5 seconds, you get a push notification when a new email arrives. The webhook is the push notification.

**Real-world examples:**
- **Stripe** sends a webhook to your server when a payment succeeds
- **GitHub** sends a webhook when someone pushes code to a repository
- **Shopify** sends a webhook when an order is placed

In all these cases, the service (Stripe/GitHub/Shopify) makes an HTTP POST request to a URL **you** registered. That URL is the "webhook."

### What Is a Webhook Delivery System?

It's the backend infrastructure that:
1. Lets users **register** their webhook URLs ("send order events to https://myserver.com/orders")
2. Accepts **events** when something happens ("an order was just created")
3. **Delivers** those events by making HTTP POST requests to the registered URLs
4. **Logs** whether the delivery succeeded or failed

Our system is this infrastructure.

### What Is an Event Type?

An event type is a label that categorizes what happened. In our system, there are three:
- `request.created` — a new request was created
- `request.updated` — an existing request was modified
- `request.deleted` — a request was deleted

When a user registers a webhook, they choose which event types they care about. If they subscribe to `["request.created", "request.updated"]`, they'll only receive deliveries for those two types — `request.deleted` events will be silently ignored for that webhook.

### What Is "Fan Out"?

Fan-out means taking one event and distributing it to multiple recipients. If User A has 3 webhooks all subscribed to `request.created`, and a `request.created` event is published, the system "fans out" that event into 3 separate delivery jobs — one for each webhook URL.

### What Is Rate Limiting?

Rate limiting means controlling how many operations happen per unit of time. In our case, it controls how many webhook deliveries the worker makes per second. If the rate limit is 5/sec, the worker will deliver at most 5 webhooks per second — even if there are 1000 jobs waiting in the queue.

**Why rate limit?** To protect both our system and the receiving servers from being overwhelmed. If we delivered 1000 webhooks per second, the receiving server might crash or start rejecting requests.

### What Is Fair Scheduling?

Fair scheduling ensures that no single user can monopolize the system. Without fairness, if User A queues 1000 deliveries and User B queues 1, User B would have to wait for all 1000 of A's deliveries to complete before getting their single delivery. With fair scheduling, User B gets served quickly regardless of how busy User A is.

### What Is a Producer-Consumer Pattern?

A design pattern where:
- **Producer** creates work items and puts them in a queue (our API server)
- **Consumer** takes work items from the queue and processes them (our worker)
- **Queue** sits between them, decoupling production from consumption (Redis)

The producer doesn't wait for the consumer to finish. The consumer works at its own pace. This is why our API returns `202 Accepted` instantly — it doesn't wait for the actual delivery.

### What Is Docker? What Is Docker Compose?

**Docker** packages an application and all its dependencies into a "container" — a lightweight, portable, isolated environment. It guarantees that if it works on your machine, it works on any machine.

**Docker Compose** lets you define and run multi-container applications. Instead of starting 5 separate Docker containers manually, you write one `docker-compose.yml` file and run `docker compose up`. It handles networking (containers can talk to each other by name), health checks (wait until the database is ready before starting the API), and volume management (persist database data).

### What Is an ORM?

ORM (Object-Relational Mapping) lets you interact with the database using Python objects instead of writing raw SQL. Instead of:
```sql
SELECT * FROM webhooks WHERE user_id = 'user1' AND is_active = true;
```
We write:
```python
Webhook.query.filter_by(user_id='user1', is_active=True).all()
```
We use **SQLAlchemy** (via Flask-SQLAlchemy) as the ORM.

### What Are HTTP Methods and Status Codes?

**Methods (what are you trying to do?):**
- `GET` — Read/retrieve data
- `POST` — Create new data or trigger an action
- `PUT` — Replace/update existing data entirely
- `PATCH` — Partially modify existing data
- `DELETE` — Remove data

**Status codes used in this project:**
- `200 OK` — Request succeeded, here's the data
- `201 Created` — A new resource was created (webhook registered)
- `202 Accepted` — Request accepted for processing but not completed yet (event queued for delivery)
- `400 Bad Request` — Client sent invalid data (missing required field, bad event type)
- `404 Not Found` — The requested webhook doesn't exist (or doesn't belong to this user)

### What Is FIFO?

FIFO = First In, First Out. Like a queue at a store — the first person in line is served first. Our Redis lists work as FIFO queues: LPUSH adds to the front, RPOP removes from the back.

### What Is Redis?

Redis is an **in-memory data store** that supports multiple data structures:
- **Strings** — simple key-value (we use this for the rate limit config: `rate_limit → "10"`)
- **Lists** — ordered sequences with push/pop (we use these as per-user queues)
- **Sets** — unordered collections of unique members (we use this for `active_users`)

Redis is extremely fast because everything is in memory (no disk reads). Operations like INCR (increment), LPUSH (push to list), RPOP (pop from list) are all **O(1)** and **atomic** (thread-safe without locks).

---

## Problem Statement — What Is Actually Being Asked

The screening round asks us to build a **multi-user webhook delivery system** in three progressive parts:

### Part A: Core Delivery (the foundation)

**What's needed:**
- Users register webhooks: "When event X happens, POST the payload to this URL"
- Users publish events: "Event X just happened, here's the data"
- The system matches events to webhooks and delivers them (HTTP POST to the registered URLs)
- Full CRUD: create, read, update, delete webhooks
- Enable/disable: temporarily turn off a webhook without deleting it
- Delivery logging: record every delivery attempt (success, failure, HTTP status code)

**What this means in practice:**

```
User registers:  "Send request.created events to http://myserver.com/hook"
User publishes:  {event_type: "request.created", payload: {order_id: 123}}
System does:     HTTP POST http://myserver.com/hook with {event_type, payload, webhook_id, timestamp}
System logs:     webhook_id=1, status_code=200, success=true
```

### Part B: Rate Limiting (protect the system)

**What's needed:**
- A configurable global rate limit: "deliver at most N webhooks per second"
- The limit can be changed at runtime (no restart needed)
- The system must respect the limit — never exceed N deliveries per second

**What this means in practice:**

```
Admin sets:     rate_limit = 5 per second
100 jobs queued: Worker delivers 5, waits 1 second, delivers 5 more, ...
Total time:     ~20 seconds to deliver all 100
Admin changes:  rate_limit = 20 per second
Next 100 jobs:  ~5 seconds to deliver (immediate effect)
```

### Part C: Multi-User Fairness (don't let one user starve others)

**What's needed:**
- If User A publishes 100 events and User B publishes 1, User B shouldn't wait behind all 100 of A's deliveries
- User B's delivery should complete with "reasonable latency" (the test checks < 10 seconds)

**What this means in practice:**

```
User A queues:   100 deliveries
User B queues:   1 delivery
Without fairness: B waits for all 100 of A → 10+ seconds at 10/sec rate limit
With fairness:   B gets served in cycle 1 → ~1 second latency
```

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

## API Request/Response Examples (Every Endpoint)

Here's what every single API call looks like — the exact request and response:

### 1. Register a Webhook

**Request:**
```http
POST /api/webhooks
Content-Type: application/json
X-User-Id: user1

{
  "url": "http://mock_receiver:9000/webhook",
  "event_types": ["request.created", "request.updated"]
}
```

**Response (201 Created):**
```json
{
  "message": "Webhook created",
  "webhook": {
    "id": 1,
    "user_id": "user1",
    "url": "http://mock_receiver:9000/webhook",
    "event_types": ["request.created", "request.updated"],
    "is_active": true,
    "created_at": "2026-03-05T09:30:00",
    "updated_at": "2026-03-05T09:30:00"
  }
}
```

### 2. List Webhooks

**Request:**
```http
GET /api/webhooks
X-User-Id: user1
```

**Response (200 OK):**
```json
{
  "webhooks": [
    {
      "id": 1,
      "user_id": "user1",
      "url": "http://mock_receiver:9000/webhook",
      "event_types": ["request.created", "request.updated"],
      "is_active": true,
      "created_at": "2026-03-05T09:30:00",
      "updated_at": "2026-03-05T09:30:00"
    }
  ]
}
```

**With filter:**
```http
GET /api/webhooks?status=active
GET /api/webhooks?status=disabled
```

### 3. Get a Specific Webhook

**Request:**
```http
GET /api/webhooks/1
X-User-Id: user1
```

**Response (200 OK):** Same structure as above, single webhook object.

**If webhook doesn't exist or belongs to another user (404):**
```json
{"error": "Webhook not found"}
```

### 4. Update a Webhook

**Request (update URL only):**
```http
PUT /api/webhooks/1
Content-Type: application/json
X-User-Id: user1

{"url": "http://new-server.com/hook"}
```

**Request (update event types only):**
```http
PUT /api/webhooks/1
Content-Type: application/json
X-User-Id: user1

{"event_types": ["request.deleted"]}
```

**Request (update both):**
```http
PUT /api/webhooks/1
Content-Type: application/json
X-User-Id: user1

{"url": "http://new-server.com/hook", "event_types": ["request.deleted"]}
```

**Response (200 OK):**
```json
{
  "message": "Webhook updated",
  "webhook": { ...updated webhook object... }
}
```

### 5. Delete a Webhook

**Request:**
```http
DELETE /api/webhooks/1
X-User-Id: user1
```

**Response (200 OK):**
```json
{"message": "Webhook deleted"}
```

This also deletes all delivery logs associated with the webhook.

### 6. Toggle (Enable/Disable) a Webhook

**Request (disable):**
```http
PATCH /api/webhooks/1/toggle
Content-Type: application/json
X-User-Id: user1

{"is_active": false}
```

**Request (enable):**
```http
PATCH /api/webhooks/1/toggle
Content-Type: application/json
X-User-Id: user1

{"is_active": true}
```

**Response (200 OK):**
```json
{
  "message": "Webhook disabled",
  "webhook": { ...webhook with is_active=false... }
}
```

### 7. Publish an Event

**Request:**
```http
POST /api/events
Content-Type: application/json
X-User-Id: user1

{
  "event_type": "request.created",
  "payload": {"order_id": 123, "amount": 49.99}
}
```

**Response (202 Accepted — webhooks matched):**
```json
{
  "message": "Event accepted, 1 deliveries queued",
  "deliveries_queued": 1
}
```

**Response (200 OK — no matching webhooks):**
```json
{
  "message": "No matching webhooks found",
  "deliveries_queued": 0
}
```

Note: 202 means "accepted for async processing" — the delivery hasn't happened yet, it's been queued.

### 8. View Delivery Logs

**Request:**
```http
GET /api/webhooks/1/deliveries
X-User-Id: user1
```

**Response (200 OK):**
```json
{
  "deliveries": [
    {
      "id": 1,
      "webhook_id": 1,
      "event_type": "request.created",
      "payload": {"order_id": 123, "amount": 49.99},
      "status_code": 200,
      "success": true,
      "error_message": null,
      "delivered_at": "2026-03-05T09:31:00"
    }
  ]
}
```

**When delivery failed (timeout):**
```json
{
  "status_code": null,
  "success": false,
  "error_message": "Request timed out"
}
```

### 9. Get Rate Limit

**Request:**
```http
GET /api/rate-limit
```

**Response (200 OK):**
```json
{"rate_limit_per_second": 10}
```

(`0` means unlimited)

### 10. Set Rate Limit

**Request:**
```http
PUT /api/rate-limit
Content-Type: application/json

{"rate_limit_per_second": 5}
```

**Response (200 OK):**
```json
{
  "message": "Rate limit updated to 5/second",
  "rate_limit_per_second": 5
}
```

### What the Worker Sends to Webhook URLs

When the worker delivers a webhook, it sends an HTTP POST to the registered URL:

```http
POST http://your-registered-url.com/webhook
Content-Type: application/json

{
  "event_type": "request.created",
  "payload": {"order_id": 123, "amount": 49.99},
  "webhook_id": 1,
  "timestamp": "2026-03-05T09:31:00.123456"
}
```

The worker considers the delivery successful if the HTTP status code is 2xx (200-299). Any other status code (4xx, 5xx) or connection error is logged as a failure.

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

## Edge Cases and What Happens

Here's every edge case and how the system handles it:

### What if the webhook URL is down / unreachable?

The worker catches `ConnectionError` and logs the failure with the error message. The delivery is logged as `success=false`, `status_code=null`, `error_message="Connection refused"`. The job is **not retried** (a production system would add retry logic).

### What if the webhook URL takes too long to respond?

The worker has a **5-second timeout** (configurable via `DELIVERY_TIMEOUT` env var). If the target server doesn't respond within 5 seconds, the delivery is logged as `success=false`, `error_message="Request timed out"`.

### What if a user publishes an event but has no webhooks?

The API returns `200 OK` with `"deliveries_queued": 0` and `"No matching webhooks found"`. Nothing is pushed to Redis.

### What if a user publishes an event but all their webhooks are disabled?

Same as above — the query filters by `is_active=True`, finds no matches, returns 0 deliveries queued.

### What if a user publishes an event type that none of their webhooks subscribe to?

Same — the matching logic checks `if event_type in w.event_types`. No matches → 0 deliveries queued.

### What if the X-User-Id header is missing?

Every endpoint returns `400 Bad Request` with `{"error": "X-User-Id header is required"}`.

### What if a user tries to access another user's webhook?

All queries filter by `user_id`. If User A tries `GET /api/webhooks/5` but webhook 5 belongs to User B, the query returns nothing → `404 Not Found`. User A can never see, modify, or delete User B's webhooks.

### What if an invalid event type is provided?

The API validates event types against `["request.created", "request.updated", "request.deleted"]`. Any other value returns `400 Bad Request` with `{"error": "Invalid event type: request.foo. Valid types: ['request.created', 'request.updated', 'request.deleted']"}`.

### What if the rate limit is set to 0?

`0` means **unlimited** — the worker delivers as fast as it can without any throttling.

### What if Redis goes down while the worker is running?

The worker catches `redis.exceptions.ConnectionError` and retries after 2 seconds. It doesn't crash — it keeps trying until Redis is back.

### What if PostgreSQL goes down?

The `log_delivery` function catches all exceptions. If it can't log to the database, it prints an error to stdout but the worker continues delivering. Delivery logging fails gracefully without blocking the delivery pipeline.

### What happens to pending jobs if the worker restarts?

Jobs stay in Redis. Redis lists persist as long as Redis is running. When the worker restarts, it'll pick up where it left off — the `active_users` set and per-user queues are still in Redis.

### What if the same user publishes events from two different API requests simultaneously?

Both requests independently push jobs to `user_queue:{user_id}`. Redis LPUSH is atomic, so there's no corruption. The jobs are added to the same queue and processed in order.

### What if there are 100 active users?

The round-robin loop iterates through all 100 in each cycle. Each user gets one delivery per cycle. With a rate limit of 10/sec, each cycle processes 10 deliveries across all users, so each user gets ~1 delivery every 10 seconds. This is fair — everyone gets equal throughput.

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

## Actual Test Output (Proof It Works)

Here's the actual output from running `python test_system.py` against the live system:

```
Webhook Delivery System - End-to-End Tests
============================================================

Waiting for services to be ready...
Services are ready!

============================================================
  PART A: Core Webhook Delivery Tests
============================================================

  Test 1: Register a webhook
  [PASS] Webhook created (status=201)
  Webhook ID: 1
  Event types: ['request.created', 'request.updated']

  Test 2: List webhooks
  [PASS] Listed webhooks
  [PASS] Found 1 webhook(s)

  Test 3: Publish request.created event (should be delivered)
  [PASS] Event accepted (status=202)
  [PASS] 1 delivery queued
  [PASS] Mock receiver got 1 delivery (expected 1)
  [PASS] Correct event type delivered

  Test 4: Publish request.deleted event (should NOT be delivered)
  [PASS] Event accepted but no matching webhooks
  [PASS] 0 deliveries queued
  [PASS] Mock receiver got 0 deliveries (correct - not subscribed)

  Test 5: Disable webhook, then publish (should NOT be delivered)
  [PASS] Webhook disabled
  [PASS] Webhook is_active=False
  [PASS] 0 deliveries queued (webhook disabled)
  [PASS] Mock receiver got 0 deliveries (webhook was disabled)

  Test 6: Re-enable webhook, then publish (should be delivered)
  [PASS] Webhook re-enabled
  [PASS] Webhook is_active=True
  [PASS] 1 delivery queued
  [PASS] Mock receiver got 1 delivery after re-enable

  Test 7: Update webhook event types
  [PASS] Webhook updated
  [PASS] Now subscribed to request.deleted

  Test 8: Verify request.deleted now delivers
  [PASS] 1 delivery queued for request.deleted
  [PASS] Mock receiver got the request.deleted delivery

  Test 9: Delete webhook
  [PASS] Webhook deleted
  [PASS] Webhook no longer in list

  ALL PART A TESTS PASSED!

============================================================
  PART B: Rate Limiting Tests
============================================================

  Test B1: Set rate limit to 5/second
  [PASS] Rate limit set to 5/second
  [PASS] Rate limit confirmed as 5/second

  Test B2: Publish 20 events with rate limit 5/sec (expect ~4 seconds)
  [PASS] All 20 deliveries received (got 20)
  Time elapsed: 2.9s (expected ~4s with 5/sec limit)
  [PASS] Deliveries were rate limited (took 2.9s, not instant)

  Test B3: Update rate limit to 20/second
  [PASS] Rate limit updated to 20/second

  Test B4: Publish 20 more events with rate limit 20/sec (should be faster)
  [PASS] All 20 deliveries received (got 20)
  Time elapsed: 0.8s (should be faster than 2.9s)

  ALL PART B TESTS PASSED!

============================================================
  PART C: Multi-User Fairness Tests
============================================================

  Test C1: Set rate limit to 10/second
  [PASS] Rate limit set to 10/second

  Test C2: User A publishes 100 events, User B publishes 1

  Test C3: Check User B's delivery latency
  [PASS] User B's delivery was received
  User B delivery latency: 1.0s
  [PASS] User B latency (1.0s) is under 10 seconds (fair scheduling)
  At time of B delivery: 19 of A's deliveries done, 1 of B's done
  User A still has 81 deliveries pending (proves B wasn't blocked)

  Test C4: Verify all deliveries eventually complete
  [PASS] All deliveries received (got 101)
  User A total deliveries: 100
  User B total deliveries: 1
  [PASS] User A got all 100 deliveries
  [PASS] User B got their 1 delivery

  ALL PART C TESTS PASSED!

All tests completed successfully!
```

**Key numbers that prove each part works:**
- **Part A:** Subscribed events deliver (1 received), unsubscribed events don't (0 received), disabled webhooks don't deliver (0), re-enabled ones do (1)
- **Part B:** 20 events at 5/sec took 2.9s (rate limited, not instant). Same 20 events at 20/sec took 0.8s (faster, proving the rate change worked)
- **Part C:** User B latency was 1.0s despite User A flooding 100 events. At the time B was served, only 19 of A's 100 deliveries were done — proving B wasn't starved

---

## How Docker Compose Orchestrates Everything

The `docker-compose.yml` defines 5 services. Here's what each section does:

```yaml
services:
  db:
    image: postgres:15-alpine        # use official PostgreSQL 15 image
    environment:                       # configure the database
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: webhooks
    ports:
      - "5432:5432"                    # expose on host for debugging
    volumes:
      - pgdata:/var/lib/postgresql/data  # persist data across restarts
    healthcheck:                       # how Docker knows db is ready
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
```

**Health checks are critical.** Without them, the API would start before PostgreSQL is ready, causing connection errors. The `depends_on` with `condition: service_healthy` ensures the API only starts after both `db` and `redis` pass their health checks.

```yaml
  api:
    depends_on:
      db:
        condition: service_healthy     # wait for db to be ready
      redis:
        condition: service_healthy     # wait for redis to be ready
```

**Networking:** All containers are on the same Docker network. They can reach each other by service name — the API connects to `db:5432`, the worker connects to `redis:6379`. No IP addresses needed.

**Volumes:** The `pgdata` volume persists PostgreSQL data. Without it, the database would be wiped every time you run `docker compose down`. Using `docker compose down -v` explicitly deletes volumes (clean slate).

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
