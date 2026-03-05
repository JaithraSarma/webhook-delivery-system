from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Webhook(db.Model):
    __tablename__ = "webhooks"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.String(100), nullable=False, index=True)
    url = db.Column(db.String(500), nullable=False)
    event_types = db.Column(db.ARRAY(db.String), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "url": self.url,
            "event_types": self.event_types,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DeliveryLog(db.Model):
    __tablename__ = "delivery_logs"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    webhook_id = db.Column(db.Integer, db.ForeignKey("webhooks.id"), nullable=False)
    event_type = db.Column(db.String(50), nullable=False)
    payload = db.Column(db.JSON, nullable=False)
    status_code = db.Column(db.Integer, nullable=True)
    success = db.Column(db.Boolean, nullable=False, default=False)
    error_message = db.Column(db.Text, nullable=True)
    delivered_at = db.Column(db.DateTime, default=datetime.utcnow)

    webhook = db.relationship("Webhook", backref=db.backref("delivery_logs", lazy=True, cascade="all, delete-orphan"))

    def to_dict(self):
        return {
            "id": self.id,
            "webhook_id": self.webhook_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "status_code": self.status_code,
            "success": self.success,
            "error_message": self.error_message,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
        }
