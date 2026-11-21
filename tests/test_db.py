from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import migrate
from tests import AppTestCase, Message, app, db

# the message table as created before operation ids existed (the shape of an existing db.sqlite)
OLD_MESSAGE_TABLE = [
    """CREATE TABLE message (
        id INTEGER NOT NULL,
        room_id INTEGER NOT NULL,
        user_id VARCHAR(12) NOT NULL,
        content TEXT NOT NULL,
        sent_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(room_id) REFERENCES room (id),
        FOREIGN KEY(user_id) REFERENCES user (id)
    )""",
    "CREATE INDEX ix_message_room_id ON message (room_id)",
]


class MigrateTests(AppTestCase):
    """alice's "general" room holds one message stored by the old version."""

    def setUp(self) -> None:
        super().setUp()
        self.alice = self.register("alice")
        self.room = self.create_room("general")
        with app.app_context(), db.engine.begin() as connection:
            connection.execute(text("DROP TABLE message"))
            for statement in OLD_MESSAGE_TABLE:
                connection.execute(text(statement))
            connection.execute(
                text("INSERT INTO message (room_id, user_id, content, sent_at) VALUES (:room, :user, 'old', :at)"),
                {"room": self.room["id"], "user": self.alice["id"], "at": "2026-11-01 10:00:00.000000"},
            )

    def migrate(self) -> None:
        with app.app_context():
            migrate()

    def columns(self) -> set[str]:
        with app.app_context():
            return {column["name"] for column in inspect(db.engine).get_columns("message")}

    def indexes(self) -> dict[str, Any]:
        with app.app_context():
            return {index["name"]: index for index in inspect(db.engine).get_indexes("message")}

    def test_adds_the_operation_id_column_and_its_unique_index(self) -> None:
        self.assertNotIn("op_id", self.columns())
        self.migrate()
        self.assertIn("op_id", self.columns())
        index = self.indexes()["uq_message_user_op"]
        self.assertTrue(index["unique"])
        self.assertEqual(index["column_names"], ["user_id", "op_id"])

    def test_keeps_old_messages(self) -> None:
        self.migrate()
        history = self.client.get(f"/api/rooms/{self.room['id']}/messages").get_json()
        self.assertEqual([message["content"] for message in history], ["old"])
        self.assertEqual(history[0]["sent_at"], "2026-11-01T10:00:00+00:00")

    def test_running_it_again_changes_nothing(self) -> None:
        self.migrate()
        before = (self.columns(), self.indexes())
        self.migrate()
        self.assertEqual((self.columns(), self.indexes()), before)

    def test_up_to_date_database_is_left_alone(self) -> None:
        super().setUp()  # back to a freshly created schema
        before = (self.columns(), self.indexes())
        self.migrate()
        self.assertEqual((self.columns(), self.indexes()), before)

    def test_migrated_database_refuses_a_repeated_operation(self) -> None:
        self.migrate()
        with app.app_context():
            db.session.add(Message(room_id=self.room["id"], user_id=self.alice["id"], content="a", op_id="op-00001"))
            db.session.commit()
            db.session.add(Message(room_id=self.room["id"], user_id=self.alice["id"], content="a", op_id="op-00001"))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()
            # old messages have no operation id, and any number of them may lack one
            db.session.add(Message(room_id=self.room["id"], user_id=self.alice["id"], content="b"))
            db.session.commit()
            self.assertEqual(db.session.scalar(db.select(db.func.count()).select_from(Message)), 3)
