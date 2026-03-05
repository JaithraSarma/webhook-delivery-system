"""
End-to-end test script for the Webhook Delivery System.
Run this after docker compose up to verify all functionality.

Usage: python test_system.py
"""

import requests
import time
import json
import sys

API_URL = "http://localhost:5000"
RECEIVER_URL = "http://localhost:9000"

USER_ID = "user_1"
HEADERS = {"X-User-Id": USER_ID, "Content-Type": "application/json"}


def log(msg):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


def check(condition, msg):
    if condition:
        print(f"  [PASS] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        sys.exit(1)


def clear_receiver_logs():
    requests.post(f"{RECEIVER_URL}/logs/clear")


def get_receiver_logs():
    resp = requests.get(f"{RECEIVER_URL}/logs")
    return resp.json()


def wait_for_delivery(expected_count, timeout=10):
    """Wait until the receiver has the expected number of logs."""
    start = time.time()
    while time.time() - start < timeout:
        logs = get_receiver_logs()
        if logs["total"] >= expected_count:
            return logs
        time.sleep(0.5)
    return get_receiver_logs()


# ============================================
# Test Part A: Core Webhook Delivery
# ============================================

def test_part_a():
    log("PART A: Core Webhook Delivery Tests")

    # clear any previous state
    clear_receiver_logs()

    # 1. Register a webhook
    log("Test 1: Register a webhook")
    resp = requests.post(f"{API_URL}/api/webhooks", headers=HEADERS, json={
        "url": "http://mock_receiver:9000/webhook",
        "event_types": ["request.created", "request.updated"]
    })
    check(resp.status_code == 201, f"Webhook created (status={resp.status_code})")
    webhook = resp.json()["webhook"]
    webhook_id = webhook["id"]
    print(f"  Webhook ID: {webhook_id}")
    print(f"  Event types: {webhook['event_types']}")

    # 2. List webhooks
    log("Test 2: List webhooks")
    resp = requests.get(f"{API_URL}/api/webhooks", headers=HEADERS)
    check(resp.status_code == 200, "Listed webhooks")
    check(len(resp.json()["webhooks"]) >= 1, f"Found {len(resp.json()['webhooks'])} webhook(s)")

    # 3. Publish request.created -> should be delivered
    log("Test 3: Publish request.created event (should be delivered)")
    clear_receiver_logs()
    resp = requests.post(f"{API_URL}/api/events", headers=HEADERS, json={
        "event_type": "request.created",
        "payload": {"request_id": "req_001", "title": "Test Request"}
    })
    check(resp.status_code == 202, f"Event accepted (status={resp.status_code})")
    check(resp.json()["deliveries_queued"] == 1, "1 delivery queued")

    logs = wait_for_delivery(1)
    check(logs["total"] == 1, f"Mock receiver got {logs['total']} delivery (expected 1)")
    check(logs["payloads"][0]["data"]["event_type"] == "request.created", "Correct event type delivered")

    # 4. Publish request.deleted -> should NOT be delivered (not subscribed)
    log("Test 4: Publish request.deleted event (should NOT be delivered)")
    clear_receiver_logs()
    resp = requests.post(f"{API_URL}/api/events", headers=HEADERS, json={
        "event_type": "request.deleted",
        "payload": {"request_id": "req_001"}
    })
    check(resp.status_code == 200, "Event accepted but no matching webhooks")
    check(resp.json()["deliveries_queued"] == 0, "0 deliveries queued")

    time.sleep(2)
    logs = get_receiver_logs()
    check(logs["total"] == 0, "Mock receiver got 0 deliveries (correct - not subscribed)")

    # 5. Disable webhook -> publish request.created -> should NOT be delivered
    log("Test 5: Disable webhook, then publish (should NOT be delivered)")
    clear_receiver_logs()
    resp = requests.patch(f"{API_URL}/api/webhooks/{webhook_id}/toggle", headers=HEADERS, json={
        "is_active": False
    })
    check(resp.status_code == 200, "Webhook disabled")
    check(resp.json()["webhook"]["is_active"] == False, "Webhook is_active=False")

    resp = requests.post(f"{API_URL}/api/events", headers=HEADERS, json={
        "event_type": "request.created",
        "payload": {"request_id": "req_002"}
    })
    check(resp.json()["deliveries_queued"] == 0, "0 deliveries queued (webhook disabled)")

    time.sleep(2)
    logs = get_receiver_logs()
    check(logs["total"] == 0, "Mock receiver got 0 deliveries (webhook was disabled)")

    # 6. Re-enable webhook -> publish -> should be delivered
    log("Test 6: Re-enable webhook, then publish (should be delivered)")
    clear_receiver_logs()
    resp = requests.patch(f"{API_URL}/api/webhooks/{webhook_id}/toggle", headers=HEADERS, json={
        "is_active": True
    })
    check(resp.status_code == 200, "Webhook re-enabled")
    check(resp.json()["webhook"]["is_active"] == True, "Webhook is_active=True")

    resp = requests.post(f"{API_URL}/api/events", headers=HEADERS, json={
        "event_type": "request.created",
        "payload": {"request_id": "req_003"}
    })
    check(resp.json()["deliveries_queued"] == 1, "1 delivery queued")

    logs = wait_for_delivery(1)
    check(logs["total"] == 1, f"Mock receiver got {logs['total']} delivery after re-enable")

    # 7. Update webhook
    log("Test 7: Update webhook event types")
    resp = requests.put(f"{API_URL}/api/webhooks/{webhook_id}", headers=HEADERS, json={
        "event_types": ["request.created", "request.updated", "request.deleted"]
    })
    check(resp.status_code == 200, "Webhook updated")
    check("request.deleted" in resp.json()["webhook"]["event_types"], "Now subscribed to request.deleted")

    # 8. Verify request.deleted now works
    log("Test 8: Verify request.deleted now delivers")
    clear_receiver_logs()
    resp = requests.post(f"{API_URL}/api/events", headers=HEADERS, json={
        "event_type": "request.deleted",
        "payload": {"request_id": "req_001"}
    })
    check(resp.json()["deliveries_queued"] == 1, "1 delivery queued for request.deleted")

    logs = wait_for_delivery(1)
    check(logs["total"] == 1, "Mock receiver got the request.deleted delivery")

    # 9. Delete webhook
    log("Test 9: Delete webhook")
    resp = requests.delete(f"{API_URL}/api/webhooks/{webhook_id}", headers=HEADERS)
    check(resp.status_code == 200, "Webhook deleted")

    resp = requests.get(f"{API_URL}/api/webhooks", headers=HEADERS)
    remaining = [w for w in resp.json()["webhooks"] if w["id"] == webhook_id]
    check(len(remaining) == 0, "Webhook no longer in list")

    log("ALL PART A TESTS PASSED!")


# ============================================
# Test Part B: Rate Limiting
# ============================================

def test_part_b():
    log("PART B: Rate Limiting Tests")

    clear_receiver_logs()

    # setup: create a webhook for testing
    resp = requests.post(f"{API_URL}/api/webhooks", headers=HEADERS, json={
        "url": "http://mock_receiver:9000/webhook",
        "event_types": ["request.created", "request.updated", "request.deleted"]
    })
    webhook_id = resp.json()["webhook"]["id"]

    # 1. Set rate limit to 5/second
    log("Test B1: Set rate limit to 5/second")
    resp = requests.put(f"{API_URL}/api/rate-limit", json={"rate_limit_per_second": 5})
    check(resp.status_code == 200, "Rate limit set to 5/second")

    resp = requests.get(f"{API_URL}/api/rate-limit")
    check(resp.json()["rate_limit_per_second"] == 5, "Rate limit confirmed as 5/second")

    # 2. Publish 20 events rapidly
    log("Test B2: Publish 20 events with rate limit 5/sec (expect ~4 seconds)")
    clear_receiver_logs()
    start_time = time.time()

    for i in range(20):
        requests.post(f"{API_URL}/api/events", headers=HEADERS, json={
            "event_type": "request.created",
            "payload": {"request_id": f"rate_test_{i}", "batch": "b1"}
        })

    # wait for all deliveries
    logs = wait_for_delivery(20, timeout=30)
    elapsed = time.time() - start_time

    check(logs["total"] == 20, f"All 20 deliveries received (got {logs['total']})")
    print(f"  Time elapsed: {elapsed:.1f}s (expected ~4s with 5/sec limit)")
    check(elapsed >= 2.0, f"Deliveries were rate limited (took {elapsed:.1f}s, not instant)")

    # 3. Update rate limit to 20/second
    log("Test B3: Update rate limit to 20/second")
    resp = requests.put(f"{API_URL}/api/rate-limit", json={"rate_limit_per_second": 20})
    check(resp.status_code == 200, "Rate limit updated to 20/second")

    # 4. Publish 20 more events (should be faster)
    log("Test B4: Publish 20 more events with rate limit 20/sec (should be faster)")
    clear_receiver_logs()
    start_time = time.time()

    for i in range(20):
        requests.post(f"{API_URL}/api/events", headers=HEADERS, json={
            "event_type": "request.created",
            "payload": {"request_id": f"rate_test_fast_{i}", "batch": "b2"}
        })

    logs = wait_for_delivery(20, timeout=15)
    elapsed_fast = time.time() - start_time

    check(logs["total"] == 20, f"All 20 deliveries received (got {logs['total']})")
    print(f"  Time elapsed: {elapsed_fast:.1f}s (should be faster than {elapsed:.1f}s)")

    # 5. Set rate limit back to 0 (unlimited) for other tests
    requests.put(f"{API_URL}/api/rate-limit", json={"rate_limit_per_second": 0})

    # cleanup
    requests.delete(f"{API_URL}/api/webhooks/{webhook_id}", headers=HEADERS)

    log("ALL PART B TESTS PASSED!")


if __name__ == "__main__":
    print("\nWebhook Delivery System - End-to-End Tests")
    print("=" * 60)

    # wait for services
    print("\nWaiting for services to be ready...")
    for i in range(30):
        try:
            requests.get(f"{API_URL}/health", timeout=2)
            requests.get(f"{RECEIVER_URL}/health", timeout=2)
            print("Services are ready!")
            break
        except:
            time.sleep(1)
    else:
        print("ERROR: Services not ready after 30s")
        sys.exit(1)

    test_part_a()

    test_part_b()

    print("\n\nAll tests completed successfully!")
