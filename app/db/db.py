import secrets
from datetime import datetime, timezone
from enum import StrEnum
from typing import TypedDict

from flask import session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import ForeignKey, Index, String, Text, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app import app
from app.validation import OP_ID_MAX_LENGTH


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(app, model_class=Base)


class Permission(StrEnum):
    NONE = "none"
    MEMBER = "member"


# the JSON shapes sent to the client


class UserDict(TypedDict):
    id: str
    display_name: str
    registered_at: str


class RoomDict(TypedDict):
    id: int
    display_name: str
    permission: Permission


class MessageDict(TypedDict):
    id: int
    room_id: int
    display_name: str
    content: str
    sent_at: str


user_in_room = db.Table(
    "user_in_room",
    db.Column("user_id", db.ForeignKey("user.id"), primary_key=True),
    db.Column("room_id", db.ForeignKey("room.id"), primary_key=True),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    # sqlite drops tzinfo on the way in; everything stored is UTC
    return dt.replace(tzinfo=timezone.utc).isoformat()


class User(db.Model):
    # the id is also the login credential, so it is only ever sent to its owner
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    registered_at: Mapped[datetime] = mapped_column(default=utcnow)
    display_name: Mapped[str] = mapped_column(String(32))
    rooms: Mapped[list["Room"]] = relationship(secondary=user_in_room, back_populates="users")

    @staticmethod
    def new_id() -> str:
        while True:
            user_id = secrets.token_hex(6)
            if db.session.get(User, user_id) is None:
                return user_id

    @staticmethod
    def from_session() -> "User | None":
        user_id = session.get("user_id")
        return db.session.get(User, user_id) if user_id else None

    def permission(self, room: "Room") -> Permission:
        return Permission.MEMBER if room in self.rooms else Permission.NONE

    def to_dict(self) -> UserDict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "registered_at": iso(self.registered_at),
        }


class Room(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(32), unique=True)
    users: Mapped[list[User]] = relationship(secondary=user_in_room, back_populates="rooms")

    def to_dict(self, user: User) -> RoomDict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "permission": user.permission(self),
        }


class Message(db.Model):
    __table_args__ = (
        # a send retried with the same operation id fails this instead of storing a duplicate;
        # NULLs never collide, so messages stored before operation ids existed are not affected
        Index("uq_message_user_op", "user_id", "op_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("room.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(12), ForeignKey("user.id"))
    content: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(default=utcnow)
    # chosen by the sender's client; NULL only for messages stored by older versions
    op_id: Mapped[str | None] = mapped_column(String(OP_ID_MAX_LENGTH))
    user: Mapped[User] = relationship(lazy="joined")

    def to_dict(self) -> MessageDict:
        return {
            "id": self.id,
            "room_id": self.room_id,
            "display_name": self.user.display_name,
            "content": self.content,
            "sent_at": iso(self.sent_at),
        }


def migrate() -> None:
    """Bring a database made by an older version up to date; create_all() only adds missing tables."""
    columns = {column["name"] for column in inspect(db.engine).get_columns("message")}
    if "op_id" not in columns:
        with db.engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE message ADD COLUMN op_id VARCHAR({OP_ID_MAX_LENGTH})"))
    for index in db.metadata.tables["message"].indexes:
        index.create(db.engine, checkfirst=True)
