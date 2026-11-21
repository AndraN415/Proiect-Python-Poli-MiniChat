from collections.abc import Iterable
from typing import Literal, TypedDict

from flask import request
from flask_socketio import ConnectionRefusedError, SocketIO, emit, join_room, leave_room, rooms
from sqlalchemy.exc import IntegrityError

from app import app, validation
from app.db import Message, MessageDict, Permission, Room, User, db
from app.socket.presence import Presence
from app.validation import ValidationError

# a message is at most a few KB of JSON; the default would accept 1 MB per packet
socket = SocketIO(app, max_http_buffer_size=64 * 1024)
presence = Presence()


# acknowledgements: what an event handler returns is what the client's callback receives


class Ack(TypedDict):
    ok: Literal[True]


class SentAck(Ack):
    message: MessageDict
    # True when this operation id was stored before: nothing new was saved or broadcast
    duplicate: bool


class ErrorAck(TypedDict):
    ok: Literal[False]
    error: str


def current_sid() -> str:
    # Flask-SocketIO adds the socket id to the request object, which Flask's type does not declare
    sid: str = getattr(request, "sid")
    return sid


def channel(room_id: int) -> str:
    return f"room-{room_id}"


def members_changed(room_ids: Iterable[int]) -> None:
    """Tell the sockets that have one of these rooms open to reload its member list."""
    for room_id in room_ids:
        socket.emit("members_changed", {"room_id": room_id}, to=channel(room_id))


def joined_room(data: object) -> tuple[User, Room]:
    """Return (user, room) for data["room_id"], or raise ValidationError unless the user is a member."""
    user = User.from_session()
    if user is None:
        raise ValidationError("not logged in")
    room = db.session.get(Room, validation.room_id(data))
    if room is None or user.permission(room) != Permission.MEMBER:
        raise ValidationError("you are not a member of this room")
    return user, room


def exit_channels() -> None:
    for name in list(rooms()):
        if name != current_sid():
            leave_room(name)


def stored_send(user: User, op_id: str) -> Message | None:
    return db.session.scalar(db.select(Message).filter_by(user_id=user.id, op_id=op_id))


@socket.on_error_default
def on_error(error: Exception) -> ErrorAck:
    # whatever is returned here becomes the acknowledgement, so the client is never left waiting
    if isinstance(error, ValidationError):
        return {"ok": False, "error": str(error)}
    db.session.rollback()
    app.logger.exception("socket event %s failed", getattr(request, "event")["message"])
    return {"ok": False, "error": "internal server error"}


@socket.on("connect")
def on_connect(_auth: object = None) -> None:
    user = User.from_session()
    if user is None:
        raise ConnectionRefusedError("not logged in")
    if presence.connect(current_sid(), user.id):
        members_changed(room.id for room in user.rooms)


@socket.on("disconnect")
def on_disconnect(_reason: object = None) -> None:
    user_id = presence.disconnect(current_sid())
    user = db.session.get(User, user_id) if user_id else None
    if user is not None:
        members_changed(room.id for room in user.rooms)


@socket.on("enter_room")
def on_enter_room(data: object = None, *_extra: object) -> Ack:
    _, room = joined_room(data)
    exit_channels()
    join_room(channel(room.id))
    return {"ok": True}


@socket.on("exit_room")
def on_exit_room(_data: object = None, *_extra: object) -> Ack:
    exit_channels()
    return {"ok": True}


@socket.on("send_message")
def on_send_message(data: object = None, *_extra: object) -> SentAck:
    """Store and broadcast a message, once per operation id: a client that got no answer sends it again."""
    user, room = joined_room(data)
    content = validation.message_content(data)
    op_id = validation.op_id(data)

    message = stored_send(user, op_id)
    if message is None:
        message = Message(room_id=room.id, user_id=user.id, content=content, op_id=op_id)
        db.session.add(message)
        try:
            db.session.commit()
        except IntegrityError:  # a retry of the same operation was stored in the meantime
            db.session.rollback()
            message = stored_send(user, op_id)
            if message is None:
                raise
        else:
            emit("message", message.to_dict(), to=channel(room.id))
            return {"ok": True, "message": message.to_dict(), "duplicate": False}

    if (message.room_id, message.content) != (room.id, content):
        raise ValidationError("this operation id was already used for a different message")
    return {"ok": True, "message": message.to_dict(), "duplicate": True}
