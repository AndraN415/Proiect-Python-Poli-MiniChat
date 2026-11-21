from collections.abc import Callable, Iterable
from functools import wraps
from typing import Any, Concatenate, TypedDict

from flask import Blueprint, abort, jsonify, request, session
from flask.typing import ResponseReturnValue
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException

from app import validation
from app.db import Message, Permission, Room, User, db
from app.socket import members_changed, presence, socket

api = Blueprint("api", __name__, url_prefix="/api")

HISTORY_LIMIT = 100
# out-of-range ids get a 404 from the router instead of overflowing SQLite's INTEGER
ROOM = f"<int(min=1, max={validation.SQLITE_INTEGER_MAX}):room_id>"


class MemberDict(TypedDict):
    display_name: str
    online: bool  # has at least one connected socket
    you: bool


@api.errorhandler(HTTPException)
def json_error(error: HTTPException) -> ResponseReturnValue:
    return jsonify(error=error.description), error.code or 500


def login_required[**P, R](view: Callable[Concatenate[User, P], R]) -> Callable[P, R]:
    """Pass the logged-in user to the view as its first argument, or answer 401."""

    @wraps(view)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        user = User.from_session()
        if user is None:
            abort(401, "not logged in")
        return view(user, *args, **kwargs)

    return wrapper


def valid[T](check: Callable[[Any], T]) -> T:
    """Run a validator from app.validation on the JSON body; invalid input becomes a 400."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "the request body must be a JSON object")
    try:
        return check(data)
    except validation.ValidationError as error:
        abort(400, str(error))


def start_session(user: User) -> None:
    session.clear()
    session["user_id"] = user.id
    session.permanent = True


def member_room(user: User, room_id: int, refusal: str) -> Room:
    """The room, if the user is one of its members; otherwise 404 or 403."""
    room = db.get_or_404(Room, room_id, description="room not found")
    if user.permission(room) != Permission.MEMBER:
        abort(403, refusal)
    return room


@api.post("/register")
def register() -> ResponseReturnValue:
    user = User(id=User.new_id(), display_name=valid(validation.display_name))
    db.session.add(user)
    db.session.commit()
    start_session(user)
    return jsonify(user.to_dict()), 201


@api.post("/login")
def login() -> ResponseReturnValue:
    user = db.session.get(User, valid(validation.login_id))
    if user is None:
        abort(401, "unknown login id")
    start_session(user)
    return jsonify(user.to_dict())


@api.post("/logout")
def logout() -> ResponseReturnValue:
    session.clear()
    return "", 204


@api.get("/me")
@login_required
def me(user: User) -> ResponseReturnValue:
    return jsonify(user.to_dict())


@api.get("/rooms")
@login_required
def list_rooms(user: User) -> ResponseReturnValue:
    rooms = db.session.scalars(db.select(Room).order_by(Room.display_name))
    return jsonify([room.to_dict(user) for room in rooms])


@api.post("/rooms")
@login_required
def create_room(user: User) -> ResponseReturnValue:
    name = valid(validation.display_name)
    # "General" and "general" would be two rooms nobody can tell apart
    taken = {existing.casefold() for existing in db.session.scalars(db.select(Room.display_name))}
    if name.casefold() in taken:
        abort(409, "a room with that name already exists")
    room = Room(display_name=name)
    user.rooms.append(room)
    try:
        db.session.commit()
    except IntegrityError:  # created by someone else in the meantime
        db.session.rollback()
        abort(409, "a room with that name already exists")
    socket.emit("rooms_changed")
    return jsonify(room.to_dict(user)), 201


@api.post(f"/rooms/{ROOM}/join")
@login_required
def join(user: User, room_id: int) -> ResponseReturnValue:
    room = db.get_or_404(Room, room_id, description="room not found")
    if room not in user.rooms:
        user.rooms.append(room)
        db.session.commit()
        members_changed([room.id])
    return jsonify(room.to_dict(user))


@api.post(f"/rooms/{ROOM}/leave")
@login_required
def leave(user: User, room_id: int) -> ResponseReturnValue:
    room = db.get_or_404(Room, room_id, description="room not found")
    if room in user.rooms:
        user.rooms.remove(room)
        db.session.commit()
        members_changed([room.id])
    return jsonify(room.to_dict(user))


@api.get(f"/rooms/{ROOM}/permission")
@login_required
def permission(user: User, room_id: int) -> ResponseReturnValue:
    room = db.get_or_404(Room, room_id, description="room not found")
    return jsonify(room_id=room.id, permission=user.permission(room))


@api.get(f"/rooms/{ROOM}/members")
@login_required
def members(user: User, room_id: int) -> ResponseReturnValue:
    """The room's members, online ones first; only display names, login ids stay private."""
    room = member_room(user, room_id, "join the room to see its members")
    listed: list[MemberDict] = [
        {"display_name": member.display_name, "online": presence.is_online(member.id), "you": member.id == user.id}
        for member in room.users
    ]
    listed.sort(key=lambda member: (not member["online"], member["display_name"].casefold()))
    return jsonify(listed)


@api.get(f"/rooms/{ROOM}/messages")
@login_required
def messages(user: User, room_id: int) -> ResponseReturnValue:
    """The latest HISTORY_LIMIT messages, or with ?after=<id> the first HISTORY_LIMIT after that id; oldest first.

    A client that reconnects asks for the messages after the last id it has, page by page, until a page is empty.
    """
    room = member_room(user, room_id, "join the room to read its messages")
    try:
        after = validation.after_id(request.args.get("after"))
    except validation.ValidationError as error:
        abort(400, str(error))

    query = db.select(Message).filter_by(room_id=room.id)
    found: Iterable[Message]
    if after is None:
        found = reversed(db.session.scalars(query.order_by(Message.id.desc()).limit(HISTORY_LIMIT)).all())
    else:
        found = db.session.scalars(query.filter(Message.id > after).order_by(Message.id).limit(HISTORY_LIMIT))
    return jsonify([message.to_dict() for message in found])
