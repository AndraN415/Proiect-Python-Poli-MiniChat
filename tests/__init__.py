import os
import unittest
from typing import Any

from flask.testing import FlaskClient
from werkzeug.test import TestResponse

# must happen before `app` is imported: config is read and the engine is built at import time
os.environ["FLASK_SQLALCHEMY_DATABASE_URI"] = "sqlite://"

from app import app  # noqa: E402
from app.db import Message, Room, db  # noqa: E402
from app.socket import presence, socket  # noqa: E402

# Plain `python -m unittest` imports the app package while scanning the project root, before this
# file runs, so the app would be on the real database. setUp drops every table: refuse to run.
if app.config["SQLALCHEMY_DATABASE_URI"] != "sqlite://":
    raise RuntimeError("app is not using the in-memory test database; run: python -m unittest discover tests")

__all__ = ["AppTestCase", "Message", "Room", "app", "db", "presence", "socket"]


class AppTestCase(unittest.TestCase):
    """Every test starts with an empty in-memory database, nobody online and a logged-out client."""

    def setUp(self) -> None:
        app.config["TESTING"] = True
        with app.app_context():
            db.drop_all()
            db.create_all()
        presence.clear()
        self.client = app.test_client()

    def new_client(self) -> FlaskClient:
        return app.test_client()

    def register(self, display_name: str = "alice", client: FlaskClient | None = None) -> dict[str, Any]:
        client = client or self.client
        response = client.post("/api/register", json={"display_name": display_name})
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()

    def create_room(self, display_name: str = "general", client: FlaskClient | None = None) -> dict[str, Any]:
        client = client or self.client
        response = client.post("/api/rooms", json={"display_name": display_name})
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()

    def add_messages(self, room_id: int, user_id: str, *contents: str) -> None:
        with app.app_context():
            db.session.add_all(Message(room_id=room_id, user_id=user_id, content=content) for content in contents)
            db.session.commit()

    def room_member_count(self, room_id: int) -> int:
        with app.app_context():
            room = db.session.get(Room, room_id)
            assert room is not None
            return len(room.users)

    def assertError(self, response: TestResponse, status: int) -> None:
        self.assertEqual(response.status_code, status)
        body = response.get_json()
        self.assertIsInstance(body, dict)
        self.assertIsInstance(body.get("error"), str)
