from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# in-memory store for received payloads
received_payloads = []


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    """Receive and log incoming webhook deliveries."""
    data = request.get_json()
    entry = {
        "received_at": datetime.utcnow().isoformat(),
        "data": data,
        "headers": dict(request.headers),
    }
    received_payloads.append(entry)
    print(f"[RECEIVER] Got webhook: {data}")
    return jsonify({"status": "received"}), 200


@app.route("/logs", methods=["GET"])
def get_logs():
    """Return all received webhook payloads."""
    return jsonify({"total": len(received_payloads), "payloads": received_payloads}), 200


@app.route("/logs/clear", methods=["POST"])
def clear_logs():
    """Clear all received payloads."""
    received_payloads.clear()
    return jsonify({"message": "Logs cleared"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000, debug=True)
