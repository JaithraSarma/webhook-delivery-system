import json
import redis
from flask import Flask, request, jsonify
from config import Config
from models import db, Webhook, DeliveryLog

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

r = redis.Redis.from_url(app.config["REDIS_URL"])

VALID_EVENT_TYPES = ["request.created", "request.updated", "request.deleted"]


def get_user_id():
    """Extract user_id from X-User-Id header."""
    user_id = request.headers.get("X-User-Id")
    if not user_id:
        return None
    return user_id


# ============================================
# Webhook CRUD Endpoints
# ============================================


@app.route("/api/webhooks", methods=["POST"])
def create_webhook():
    """Register a new webhook for a user."""
    user_id = get_user_id()
    if not user_id:
        return jsonify({"error": "X-User-Id header is required"}), 400

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    url = data.get("url")
    event_types = data.get("event_types")

    if not url:
        return jsonify({"error": "url is required"}), 400
    if not event_types or not isinstance(event_types, list):
        return jsonify({"error": "event_types must be a non-empty list"}), 400

    # validate event types
    for et in event_types:
        if et not in VALID_EVENT_TYPES:
            return jsonify({"error": f"Invalid event type: {et}. Valid types: {VALID_EVENT_TYPES}"}), 400

    webhook = Webhook(
        user_id=user_id,
        url=url,
        event_types=event_types,
        is_active=True,
    )
    db.session.add(webhook)
    db.session.commit()

    return jsonify({"message": "Webhook created", "webhook": webhook.to_dict()}), 201


@app.route("/api/webhooks", methods=["GET"])
def list_webhooks():
    """List all webhooks for a user. Supports ?status=active or ?status=disabled."""
    user_id = get_user_id()
    if not user_id:
        return jsonify({"error": "X-User-Id header is required"}), 400

    status_filter = request.args.get("status")

    query = Webhook.query.filter_by(user_id=user_id)

    if status_filter == "active":
        query = query.filter_by(is_active=True)
    elif status_filter == "disabled":
        query = query.filter_by(is_active=False)

    webhooks = query.all()
    return jsonify({"webhooks": [w.to_dict() for w in webhooks]}), 200


@app.route("/api/webhooks/<int:webhook_id>", methods=["GET"])
def get_webhook(webhook_id):
    """Get a specific webhook by ID."""
    user_id = get_user_id()
    if not user_id:
        return jsonify({"error": "X-User-Id header is required"}), 400

    webhook = Webhook.query.filter_by(id=webhook_id, user_id=user_id).first()
    if not webhook:
        return jsonify({"error": "Webhook not found"}), 404

    return jsonify({"webhook": webhook.to_dict()}), 200


@app.route("/api/webhooks/<int:webhook_id>", methods=["PUT"])
def update_webhook(webhook_id):
    """Update a webhook's URL or event subscriptions."""
    user_id = get_user_id()
    if not user_id:
        return jsonify({"error": "X-User-Id header is required"}), 400

    webhook = Webhook.query.filter_by(id=webhook_id, user_id=user_id).first()
    if not webhook:
        return jsonify({"error": "Webhook not found"}), 404

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    if "url" in data:
        webhook.url = data["url"]

    if "event_types" in data:
        event_types = data["event_types"]
        if not isinstance(event_types, list) or len(event_types) == 0:
            return jsonify({"error": "event_types must be a non-empty list"}), 400
        for et in event_types:
            if et not in VALID_EVENT_TYPES:
                return jsonify({"error": f"Invalid event type: {et}"}), 400
        webhook.event_types = event_types

    db.session.commit()
    return jsonify({"message": "Webhook updated", "webhook": webhook.to_dict()}), 200


@app.route("/api/webhooks/<int:webhook_id>", methods=["DELETE"])
def delete_webhook(webhook_id):
    """Permanently remove a webhook."""
    user_id = get_user_id()
    if not user_id:
        return jsonify({"error": "X-User-Id header is required"}), 400

    webhook = Webhook.query.filter_by(id=webhook_id, user_id=user_id).first()
    if not webhook:
        return jsonify({"error": "Webhook not found"}), 404

    # delete associated delivery logs first
    DeliveryLog.query.filter_by(webhook_id=webhook_id).delete()
    db.session.delete(webhook)
    db.session.commit()
    return jsonify({"message": "Webhook deleted"}), 200


@app.route("/api/webhooks/<int:webhook_id>/toggle", methods=["PATCH"])
def toggle_webhook(webhook_id):
    """Enable or disable a webhook."""
    user_id = get_user_id()
    if not user_id:
        return jsonify({"error": "X-User-Id header is required"}), 400

    webhook = Webhook.query.filter_by(id=webhook_id, user_id=user_id).first()
    if not webhook:
        return jsonify({"error": "Webhook not found"}), 404

    data = request.get_json()
    if data and "is_active" in data:
        webhook.is_active = bool(data["is_active"])
    else:
        # just toggle
        webhook.is_active = not webhook.is_active

    db.session.commit()
    return jsonify({
        "message": f"Webhook {'enabled' if webhook.is_active else 'disabled'}",
        "webhook": webhook.to_dict(),
    }), 200


# ============================================
# Event Ingestion Endpoint
# ============================================


@app.route("/api/events", methods=["POST"])
def ingest_event():
    """Accept an event and fan out to matching webhooks."""
    user_id = get_user_id()
    if not user_id:
        return jsonify({"error": "X-User-Id header is required"}), 400

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    event_type = data.get("event_type")
    payload = data.get("payload")

    if not event_type:
        return jsonify({"error": "event_type is required"}), 400
    if event_type not in VALID_EVENT_TYPES:
        return jsonify({"error": f"Invalid event type: {event_type}"}), 400
    if payload is None:
        return jsonify({"error": "payload is required"}), 400

    # find all active webhooks for this user subscribed to this event type
    webhooks = Webhook.query.filter_by(user_id=user_id, is_active=True).all()

    matching_webhooks = [w for w in webhooks if event_type in w.event_types]

    if not matching_webhooks:
        return jsonify({"message": "No matching webhooks found", "deliveries_queued": 0}), 200

    # queue delivery jobs in per-user Redis queues for fair scheduling
    queued_count = 0
    for webhook in matching_webhooks:
        job = {
            "webhook_id": webhook.id,
            "url": webhook.url,
            "event_type": event_type,
            "payload": payload,
            "user_id": user_id,
        }
        # push to per-user queue
        r.lpush(f"user_queue:{user_id}", json.dumps(job))
        # register user in the active users set
        r.sadd("active_users", user_id)
        queued_count += 1

    return jsonify({
        "message": f"Event accepted, {queued_count} deliveries queued",
        "deliveries_queued": queued_count,
    }), 202


# ============================================
# Delivery Logs Endpoint (for debugging)
# ============================================


@app.route("/api/webhooks/<int:webhook_id>/deliveries", methods=["GET"])
def get_deliveries(webhook_id):
    """Get delivery logs for a specific webhook."""
    user_id = get_user_id()
    if not user_id:
        return jsonify({"error": "X-User-Id header is required"}), 400

    webhook = Webhook.query.filter_by(id=webhook_id, user_id=user_id).first()
    if not webhook:
        return jsonify({"error": "Webhook not found"}), 404

    logs = DeliveryLog.query.filter_by(webhook_id=webhook_id).order_by(
        DeliveryLog.delivered_at.desc()
    ).limit(50).all()

    return jsonify({"deliveries": [log.to_dict() for log in logs]}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


# ============================================
# Rate Limit Configuration (Part B)
# ============================================


@app.route("/api/rate-limit", methods=["GET"])
def get_rate_limit():
    """Get the current global delivery rate limit."""
    limit = r.get("rate_limit")
    if limit is not None:
        current_limit = int(limit)
    else:
        current_limit = 0  # 0 means unlimited
    return jsonify({"rate_limit_per_second": current_limit}), 200


@app.route("/api/rate-limit", methods=["PUT"])
def set_rate_limit():
    """Set the global delivery rate limit (deliveries per second)."""
    data = request.get_json()
    if not data or "rate_limit_per_second" not in data:
        return jsonify({"error": "rate_limit_per_second is required"}), 400

    limit = data["rate_limit_per_second"]
    if not isinstance(limit, int) or limit < 0:
        return jsonify({"error": "rate_limit_per_second must be a non-negative integer"}), 400

    r.set("rate_limit", limit)
    return jsonify({
        "message": f"Rate limit updated to {limit}/second",
        "rate_limit_per_second": limit,
    }), 200


# create tables on startup
with app.app_context():
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()
    if "webhooks" not in existing_tables:
        db.create_all()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
