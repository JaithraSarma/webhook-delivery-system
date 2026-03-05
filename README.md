# Multi-User Webhook Delivery System

A containerized webhook delivery system that supports multi-user webhook registration, event-driven delivery, configurable rate limiting, and fair scheduling across users.

## Quick Start

```bash
docker compose up --build -d
```

That's it. All 5 services (API, Worker, PostgreSQL, Redis, Mock Receiver) start automatically with health checks — no manual steps needed.

The API is available at `http://localhost:5000` and the mock receiver at `http://localhost:9000`.

To run the automated test suite:

```bash
pip install requests
python test_system.py
```

To stop everything:

```bash
docker compose down -v
```

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Client     │────▶│   Flask API  │────▶│    Redis     │
│ (X-User-Id)  │     │  (port 5000) │     │  (queues)    │
└──────────────┘     └──────┬───────┘     └──────┬───────┘
                            │                     │
                            ▼                     ▼
                     ┌──────────────┐     ┌──────────────┐
                     │  PostgreSQL  │◀────│   Worker     │
                     │  (storage)   │     │ (delivery)   │
                     └──────────────┘     └──────┬───────┘
                                                  │
                                                  ▼
                                          ┌──────────────┐
                                          │  Target URL  │
                                          │  (receiver)  │
                                          └──────────────┘
```

**Services:**

| Service | Tech | Role |
|---------|------|------|
| `api` | Flask + Gunicorn | REST API for webhook CRUD, event ingestion, rate limit config |
| `worker` | Python | Polls Redis queues, delivers webhooks with rate limiting |
| `db` | PostgreSQL 15 | Stores webhooks and delivery logs |
| `redis` | Redis 7 | Per-user delivery queues + rate limit state |
| `mock_receiver` | Flask | Test endpoint that receives and logs deliveries |

**Key Design Decisions:**

- **PostgreSQL** for webhooks/logs because we need relational integrity (FK from logs → webhooks)
- **Redis** for queuing because it's fast for push/pop operations and supports atomic counters for rate limiting
- **Per-user queues** in Redis (`user_queue:{user_id}`) — each user gets their own FIFO queue
- **Round-robin scheduling** in the worker — cycles through all active users, processing one job per user per cycle

## API Endpoints

### Webhook Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/webhooks` | Register a new webhook |
| `GET` | `/api/webhooks` | List webhooks (`?status=active\|disabled`) |
| `GET` | `/api/webhooks/:id` | Get a specific webhook |
| `PUT` | `/api/webhooks/:id` | Update URL or event types |
| `DELETE` | `/api/webhooks/:id` | Delete a webhook |
| `PATCH` | `/api/webhooks/:id/toggle` | Enable/disable a webhook |

### Event Ingestion

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/events` | Publish an event (fans out to matching webhooks) |

### Delivery Logs

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/webhooks/:id/deliveries` | View recent delivery attempts |

### Rate Limit Configuration

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/rate-limit` | Get current rate limit |
| `PUT` | `/api/rate-limit` | Set rate limit (deliveries/second) |

**All endpoints require the `X-User-Id` header** for user identification.

### Example Usage

```bash
# Register a webhook
curl -X POST http://localhost:5000/api/webhooks \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user1" \
  -d '{"url": "http://mock_receiver:9000/webhook", "event_types": ["request.created"]}'

# Publish an event
curl -X POST http://localhost:5000/api/events \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user1" \
  -d '{"event_type": "request.created", "payload": {"message": "hello"}}'

# Set rate limit
curl -X PUT http://localhost:5000/api/rate-limit \
  -H "Content-Type: application/json" \
  -d '{"rate_limit_per_second": 10}'
```

## Queuing, Rate Limiting & Fairness Strategy

### Queuing Strategy

Events are routed to **per-user Redis queues** (`user_queue:{user_id}`). When an event is published, the API finds all active webhooks for that user subscribed to the event type, and pushes individual delivery jobs into that user's queue. A Redis set (`active_users`) tracks which users have pending deliveries.

**Why per-user queues over a single global queue:** A single queue means one user flooding events would push all their jobs to the front, making every other user wait. Per-user queues isolate users so the worker can pick from each user independently.

### Rate Limiting Strategy

The worker uses a **sliding window counter** backed by Redis. Each second gets a unique Redis key (`rate_limit_window:{unix_timestamp}`). Before delivering, the worker atomically increments the counter — if it exceeds the configured limit, the increment is rolled back and the worker sleeps until the next second.

**Why sliding window counter:** Token bucket and leaky bucket algorithms are more complex to implement correctly with Redis. The per-second counter is simple, accurate, and uses only `INCR`/`DECR` — both atomic Redis operations. The keys auto-expire after 3 seconds so there's no cleanup needed.

The rate limit is configurable at runtime via the `PUT /api/rate-limit` endpoint. Setting it to `0` disables rate limiting entirely.

### Fairness Strategy

The worker implements **round-robin fair scheduling**. On each cycle, it fetches all active users and pops exactly **one job per user** before moving to the next user. This guarantees that no user can monopolize the delivery pipeline.

**What this achieves:** If User A has 100 pending deliveries and User B has 1, User B's delivery gets processed within the first cycle — not after all 100 of A's jobs. In our tests, User B's latency was ~1 second even when User A had flooded 100 events.

**Alternatives considered:**
- **Weighted fair queuing:** Give different priorities to different users. Rejected because the problem doesn't require differentiated priority.
- **Strict priority queues:** Would require defining priority levels, adds complexity with no benefit for this use case.
- **Random selection:** Simpler but doesn't guarantee fairness — a user with 100 jobs would still get selected ~99% of the time.

## Project Structure

```
webhook-delivery-system/
├── api/
│   ├── app.py              # Flask REST API (CRUD + events + rate limit config)
│   ├── config.py           # Flask configuration
│   ├── models.py           # SQLAlchemy models (Webhook, DeliveryLog)
│   ├── requirements.txt
│   └── Dockerfile
├── worker/
│   ├── worker.py           # Delivery worker (rate limiting + fair scheduling)
│   ├── requirements.txt
│   └── Dockerfile
├── mock_receiver/
│   ├── server.py           # Test endpoint that logs received webhooks
│   ├── requirements.txt
│   └── Dockerfile
├── docker-compose.yml      # Orchestrates all 5 services
├── test_system.py          # End-to-end test suite (Parts A, B, C)
└── README.md
```
