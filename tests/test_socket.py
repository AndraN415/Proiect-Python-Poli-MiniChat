import uuid
from typing import Any
from unittest import mock

from flask.testing import FlaskClient
from flask_socketio import SocketIOTestClient

from app.socket.socket import stored_send
from app.validation import MESSAGE_MAX_LENGTH
from tests import AppTestCase, Message, app, db, socket


def received(sio: SocketIOTestClient, name: str) -> list[Any]:
    """The payloads of the `name` events received since the last call."""
    # the test client unwraps the arguments of an event called "message", and only of that one
    return [event["args"] if name == "message" else event["args"][0]
            for event in sio.get_received() if event["name"] == name]


def received_messages(sio: SocketIOTestClient) -> list[Any]:
    return received(sio, "message")


def new_op_id() -> str:
    return uuid.uuid4().hex


class SocketTestCase(AppTestCase):
    """alice (self.client) created "general" and has a connected socket that hasn't entered a room."""

    def setUp(self) -> None:
        super().setUp()
        self.alice = self.register("alice")
        self.room = self.create_room("general")
        self.alice_socket = self.connect(self.client)

    def connect(self, client: FlaskClient) -> SocketIOTestClient:
        sio = socket.test_client(app, flask_test_client=client)
        self.addCleanup(self.disconnect, sio)
        return sio

    @staticmethod
    def disconnect(sio: SocketIOTestClient) -> None:
        if sio.is_connected():
            sio.disconnect()

    def member(self, display_name: str, room: dict[str, Any] | None = None) -> tuple[FlaskClient, SocketIOTestClient]:
        """A new user who joined `room` (default: general), with a connected socket."""
        client = self.new_client()
        self.register(display_name, client=client)
        client.post(f"/api/rooms/{(room or self.room)['id']}/join")
        return client, self.connect(client)

    def outsider(self, display_name: str) -> SocketIOTestClient:
        """A new user who is in no rooms, with a connected socket."""
        client = self.new_client()
        self.register(display_name, client=client)
        return self.connect(client)

    def enter(self, sio: SocketIOTestClient, room: dict[str, Any] | None = None) -> None:
        reply = sio.emit("enter_room", {"room_id": (room or self.room)["id"]}, callback=True)
        self.assertEqual(reply, {"ok": True})

    def send(self, sio: SocketIOTestClient, content: Any, room: dict[str, Any] | None = None,
             op_id: str | None = None) -> Any:
        """Emit send_message with a fresh operation id, unless one is given (a retry)."""
        data = {"room_id": (room or self.room)["id"], "content": content, "op_id": op_id or new_op_id()}
        return sio.emit("send_message", data, callback=True)

    def stored(self) -> list[str]:
        with app.app_context():
            return list(db.session.scalars(db.select(Message.content).order_by(Message.id)))

    def assertSent(self, reply: Any, duplicate: bool = False) -> dict[str, Any]:
        """Check the acknowledgement of an accepted send and return the stored message it carries."""
        self.assertEqual(set(reply), {"ok", "message", "duplicate"})
        self.assertIs(reply["ok"], True)
        self.assertIs(reply["duplicate"], duplicate)
        return reply["message"]

    def assertRefused(self, reply: Any) -> None:
        self.assertIs(reply["ok"], False)
        self.assertIsInstance(reply["error"], str)


class ConnectTests(AppTestCase):
    def test_refused_when_logged_out(self) -> None:
        self.assertFalse(socket.test_client(app, flask_test_client=self.client).is_connected())

    def test_refused_for_unknown_user(self) -> None:
        with self.client.session_transaction() as session:
            session["user_id"] = "000000000000"
        self.assertFalse(socket.test_client(app, flask_test_client=self.client).is_connected())

    def test_accepted_when_logged_in(self) -> None:
        self.register()
        sio = socket.test_client(app, flask_test_client=self.client)
        self.addCleanup(sio.disconnect)
        self.assertTrue(sio.is_connected())


class EnterRoomTests(SocketTestCase):
    def test_member_can_enter(self) -> None:
        self.enter(self.alice_socket)

    def test_non_member_is_refused(self) -> None:
        bob_socket = self.outsider("bob")
        self.assertRefused(bob_socket.emit("enter_room", {"room_id": self.room["id"]}, callback=True))
        self.send(self.alice_socket, "hi")
        self.assertEqual(received_messages(bob_socket), [])

    def test_rejects_bad_payloads(self) -> None:
        room_id = self.room["id"]
        bad = [None, {}, {"room_id": None}, {"room_id": str(room_id)}, {"room_id": 999}, "general", [room_id],
               {"room_id": 0}, {"room_id": -1}, {"room_id": 1.0}, {"room_id": 2**63}, {"room_id": 2**80}]
        for data in bad:
            with self.subTest(data=data):
                self.assertRefused(self.alice_socket.emit("enter_room", data, callback=True))

    def test_true_is_not_room_one(self) -> None:
        self.assertEqual(self.room["id"], 1)
        self.assertRefused(self.alice_socket.emit("enter_room", {"room_id": True}, callback=True))

    def test_missing_payload_still_gets_an_answer(self) -> None:
        self.assertRefused(self.alice_socket.emit("enter_room", callback=True))

    def test_extra_arguments_are_ignored(self) -> None:
        reply = self.alice_socket.emit("enter_room", {"room_id": self.room["id"]}, "extra", callback=True)
        self.assertEqual(reply, {"ok": True})

    def test_messages_arrive_only_after_entering(self) -> None:
        _, bob_socket = self.member("bob")
        self.send(self.alice_socket, "before")
        self.assertEqual(received_messages(bob_socket), [])

        self.enter(bob_socket)
        self.send(self.alice_socket, "after")
        self.assertEqual([m["content"] for m in received_messages(bob_socket)], ["after"])

    def test_entering_a_room_leaves_the_previous_one(self) -> None:
        other = self.create_room("other")
        bob_client, bob_socket = self.member("bob")
        bob_client.post(f"/api/rooms/{other['id']}/join")
        self.enter(bob_socket)
        self.enter(bob_socket, room=other)

        self.send(self.alice_socket, "in general")
        self.send(self.alice_socket, "in other", room=other)
        self.assertEqual([m["content"] for m in received_messages(bob_socket)], ["in other"])


class ExitRoomTests(SocketTestCase):
    def test_stops_messages(self) -> None:
        _, bob_socket = self.member("bob")
        self.enter(bob_socket)
        self.assertEqual(bob_socket.emit("exit_room", {}, callback=True), {"ok": True})
        self.send(self.alice_socket, "hi")
        self.assertEqual(received_messages(bob_socket), [])

    def test_works_without_payload_or_room(self) -> None:
        self.assertEqual(self.alice_socket.emit("exit_room", callback=True), {"ok": True})


class SendMessageTests(SocketTestCase):
    def test_broadcasts_to_everyone_in_the_room_including_sender(self) -> None:
        _, bob_socket = self.member("bob")
        self.enter(self.alice_socket)
        self.enter(bob_socket)
        sent = self.assertSent(self.send(self.alice_socket, "hello"))

        [to_alice] = received_messages(self.alice_socket)
        [to_bob] = received_messages(bob_socket)
        self.assertEqual(to_alice, to_bob)
        self.assertEqual(to_bob, sent)
        self.assertEqual(set(to_bob), {"id", "room_id", "display_name", "content", "sent_at"})
        self.assertEqual(
            (to_bob["room_id"], to_bob["display_name"], to_bob["content"]),
            (self.room["id"], "alice", "hello"),
        )

    def test_broadcast_matches_stored_history(self) -> None:
        self.enter(self.alice_socket)
        self.send(self.alice_socket, "hello")
        [message] = received_messages(self.alice_socket)
        history = self.client.get(f"/api/rooms/{self.room['id']}/messages").get_json()
        self.assertEqual(history, [message])

    def test_content_is_trimmed(self) -> None:
        self.send(self.alice_socket, "  hi  ")
        self.assertEqual(self.stored(), ["hi"])

    def test_content_is_sanitised(self) -> None:
        # NUL and a right-to-left override are dropped, line breaks kept, long gaps shortened
        self.send(self.alice_socket, "hello\x00 ‮world  \r\n\r\n\r\n\r\nbye")
        self.assertEqual(self.stored(), ["hello world\n\nbye"])

    def test_html_is_stored_as_typed(self) -> None:
        # escaping happens when it is displayed (textContent), not by mangling the text
        self.send(self.alice_socket, "<b>hi</b> & <3")
        self.assertEqual(self.stored(), ["<b>hi</b> & <3"])

    def test_lone_surrogate_does_not_crash(self) -> None:
        self.assertSent(self.send(self.alice_socket, "a\ud800b"))
        self.assertEqual(self.stored(), ["ab"])

    def test_length_is_counted_after_sanitising(self) -> None:
        padded = "​" * 50 + "x" * MESSAGE_MAX_LENGTH + "\x00" * 50
        self.assertSent(self.send(self.alice_socket, padded))

    def test_unexpected_error_still_gets_an_answer(self) -> None:
        self.enter(self.alice_socket)
        with mock.patch.object(db.session, "commit", side_effect=RuntimeError("disk full")), \
                self.assertLogs(app.logger, "ERROR"):
            reply = self.send(self.alice_socket, "hi")
        self.assertEqual(reply, {"ok": False, "error": "internal server error"})
        self.assertEqual(received_messages(self.alice_socket), [])
        self.assertSent(self.send(self.alice_socket, "again"))
        self.assertEqual(self.stored(), ["again"])

    def test_accepts_max_length(self) -> None:
        self.assertSent(self.send(self.alice_socket, "x" * MESSAGE_MAX_LENGTH))

    def test_rejects_too_long(self) -> None:
        self.assertRefused(self.send(self.alice_socket, "x" * (MESSAGE_MAX_LENGTH + 1)))
        self.assertEqual(self.stored(), [])

    def test_rejects_invalid_content(self) -> None:
        self.enter(self.alice_socket)
        for content in [None, "", "   ", 42, ["hi"], "​⁠", "\n\n\t"]:
            with self.subTest(content=content):
                self.assertRefused(self.send(self.alice_socket, content))
        self.assertRefused(self.alice_socket.emit("send_message", {"room_id": self.room["id"]}, callback=True))
        self.assertEqual(self.stored(), [])
        self.assertEqual(received_messages(self.alice_socket), [])

    def test_rejects_bad_payloads(self) -> None:
        bad = [None, {}, "hi", {"content": "hi"}, {"room_id": 999, "content": "hi"},
               {"room_id": True, "content": "hi"}, {"room_id": 2**80, "content": "hi"}]
        for data in bad:
            with self.subTest(data=data):
                self.assertRefused(self.alice_socket.emit("send_message", data, callback=True))
        self.assertRefused(self.alice_socket.emit("send_message", callback=True))
        self.assertEqual(self.stored(), [])

    def test_non_member_cannot_send(self) -> None:
        self.assertRefused(self.send(self.outsider("bob"), "hi"))
        self.assertEqual(self.stored(), [])

    def test_not_delivered_to_other_rooms(self) -> None:
        other = self.create_room("other")
        _, bob_socket = self.member("bob", room=other)
        self.enter(bob_socket, room=other)
        self.send(self.alice_socket, "hi")
        self.assertEqual(received_messages(bob_socket), [])

    def test_member_can_send_without_entering(self) -> None:
        _, bob_socket = self.member("bob")
        self.enter(self.alice_socket)
        self.assertSent(self.send(bob_socket, "hi"))
        self.assertEqual([m["content"] for m in received_messages(self.alice_socket)], ["hi"])
        self.assertEqual(received_messages(bob_socket), [])

    def test_never_exposes_login_ids(self) -> None:
        self.enter(self.alice_socket)
        self.send(self.alice_socket, "hi")
        [message] = received_messages(self.alice_socket)
        self.assertNotIn(self.alice["id"], str(message))

    def test_leaving_over_http_revokes_sending_at_once(self) -> None:
        bob_client, bob_socket = self.member("bob")
        bob_client.post(f"/api/rooms/{self.room['id']}/leave")
        self.assertRefused(self.send(bob_socket, "hi"))

    def test_socket_that_left_keeps_receiving_until_exit_room(self) -> None:
        # the frontend closes this gap by checking /permission for every received message
        bob_client, bob_socket = self.member("bob")
        self.enter(bob_socket)
        bob_client.post(f"/api/rooms/{self.room['id']}/leave")

        self.send(self.alice_socket, "one")
        self.assertEqual(len(received_messages(bob_socket)), 1)
        permission = bob_client.get(f"/api/rooms/{self.room['id']}/permission").get_json()
        self.assertEqual(permission["permission"], "none")

        bob_socket.emit("exit_room", {}, callback=True)
        self.send(self.alice_socket, "two")
        self.assertEqual(received_messages(bob_socket), [])

    def test_a_failed_send_can_be_retried_with_the_same_operation_id(self) -> None:
        op_id = new_op_id()
        with mock.patch.object(db.session, "commit", side_effect=RuntimeError("disk full")), \
                self.assertLogs(app.logger, "ERROR"):
            self.assertRefused(self.send(self.alice_socket, "hi", op_id=op_id))
        # nothing was stored the first time, so the retry stores it
        self.assertSent(self.send(self.alice_socket, "hi", op_id=op_id))
        self.assertEqual(self.stored(), ["hi"])


class OperationIdTests(SocketTestCase):
    def test_retry_with_the_same_operation_id_is_stored_and_broadcast_once(self) -> None:
        _, bob_socket = self.member("bob")
        self.enter(bob_socket)
        op_id = new_op_id()
        first = self.assertSent(self.send(self.alice_socket, "hello", op_id=op_id))
        again = self.assertSent(self.send(self.alice_socket, "hello", op_id=op_id), duplicate=True)

        self.assertEqual(again, first)
        self.assertEqual(self.stored(), ["hello"])
        self.assertEqual(received_messages(bob_socket), [first])

    def test_retry_after_reconnecting_is_not_duplicated(self) -> None:
        op_id = new_op_id()
        first = self.assertSent(self.send(self.alice_socket, "hello", op_id=op_id))
        self.alice_socket.disconnect()
        reconnected = self.connect(self.client)
        self.assertEqual(self.assertSent(self.send(reconnected, "hello", op_id=op_id), duplicate=True), first)
        self.assertEqual(self.stored(), ["hello"])

    def test_retry_is_compared_after_sanitising(self) -> None:
        op_id = new_op_id()
        self.assertSent(self.send(self.alice_socket, "hello", op_id=op_id))
        self.assertSent(self.send(self.alice_socket, "  hello\x00 ", op_id=op_id), duplicate=True)
        self.assertEqual(self.stored(), ["hello"])

    def test_a_new_operation_id_is_a_new_message(self) -> None:
        self.assertSent(self.send(self.alice_socket, "hi"))
        self.assertSent(self.send(self.alice_socket, "hi"))
        self.assertEqual(self.stored(), ["hi", "hi"])

    def test_operation_ids_belong_to_their_user(self) -> None:
        _, bob_socket = self.member("bob")
        op_id = new_op_id()
        self.assertSent(self.send(self.alice_socket, "from alice", op_id=op_id))
        self.assertSent(self.send(bob_socket, "from bob", op_id=op_id))
        self.assertEqual(self.stored(), ["from alice", "from bob"])

    def test_reusing_an_operation_id_for_another_message_is_refused(self) -> None:
        other = self.create_room("other")
        op_id = new_op_id()
        self.assertSent(self.send(self.alice_socket, "hello", op_id=op_id))
        self.assertRefused(self.send(self.alice_socket, "something else", op_id=op_id))
        self.assertRefused(self.send(self.alice_socket, "hello", room=other, op_id=op_id))
        self.assertEqual(self.stored(), ["hello"])

    def test_requires_a_valid_operation_id(self) -> None:
        self.enter(self.alice_socket)
        for op_id in ["", "short", "x" * 65, "with space", "ünïcødé-id", "a\nbcdefghi", 12345678, ["abcdefgh"], None]:
            with self.subTest(op_id=op_id):
                data = {"room_id": self.room["id"], "content": "hi", "op_id": op_id}
                self.assertRefused(self.alice_socket.emit("send_message", data, callback=True))
        data = {"room_id": self.room["id"], "content": "hi"}
        self.assertRefused(self.alice_socket.emit("send_message", data, callback=True))
        self.assertEqual(self.stored(), [])
        self.assertEqual(received_messages(self.alice_socket), [])

    def test_accepts_operation_id_shapes(self) -> None:
        for op_id in ["abcdefgh", "x" * 64, str(uuid.uuid4()), "Op_ID-123"]:
            with self.subTest(op_id=op_id):
                self.assertSent(self.send(self.alice_socket, op_id, op_id=op_id))

    def test_a_retry_that_overtakes_the_first_attempt(self) -> None:
        op_id = new_op_id()
        first = self.assertSent(self.send(self.alice_socket, "hello", op_id=op_id))
        lookups: list[str] = []

        def missed_once(user: Any, op_id: str) -> Message | None:
            # the first lookup misses, as if the other attempt committed right after it
            lookups.append(op_id)
            return None if len(lookups) == 1 else stored_send(user, op_id)

        with mock.patch("app.socket.socket.stored_send", side_effect=missed_once):
            again = self.assertSent(self.send(self.alice_socket, "hello", op_id=op_id), duplicate=True)
        self.assertEqual(again, first)
        self.assertEqual(len(lookups), 2)
        self.assertEqual(self.stored(), ["hello"])


class PresenceTests(SocketTestCase):
    """alice is online in general; members() is alice's view of general's member list."""

    def members(self, client: FlaskClient | None = None) -> list[Any]:
        response = (client or self.client).get(f"/api/rooms/{self.room['id']}/members")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def offline_member(self, display_name: str) -> FlaskClient:
        """A new user who joined general, without a socket."""
        client = self.new_client()
        self.register(display_name, client=client)
        client.post(f"/api/rooms/{self.room['id']}/join")
        return client

    def members_changed(self, sio: SocketIOTestClient) -> list[Any]:
        return received(sio, "members_changed")

    def test_lists_members_online_first(self) -> None:
        self.offline_member("bob")
        self.member("carol")
        self.assertEqual(self.members(), [
            {"display_name": "alice", "online": True, "you": True},
            {"display_name": "carol", "online": True, "you": False},
            {"display_name": "bob", "online": False, "you": False},
        ])

    def test_never_exposes_login_ids(self) -> None:
        bob = self.offline_member("bob")
        bob_id = bob.get("/api/me").get_json()["id"]
        response = self.client.get(f"/api/rooms/{self.room['id']}/members")
        self.assertNotIn(self.alice["id"], response.get_data(as_text=True))
        self.assertNotIn(bob_id, response.get_data(as_text=True))

    def test_online_until_the_last_socket_disconnects(self) -> None:
        second_tab = self.connect(self.client)
        self.alice_socket.disconnect()
        self.assertTrue(self.members()[0]["online"])
        second_tab.disconnect()
        self.assertFalse(self.members()[0]["online"])

    def test_room_is_told_when_a_member_comes_online_and_goes_offline(self) -> None:
        self.enter(self.alice_socket)
        bob = self.offline_member("bob")
        self.alice_socket.get_received()

        bob_socket = self.connect(bob)
        self.assertEqual(self.members_changed(self.alice_socket), [{"room_id": self.room["id"]}])
        bob_socket.disconnect()
        self.assertEqual(self.members_changed(self.alice_socket), [{"room_id": self.room["id"]}])

    def test_another_tab_of_an_online_user_changes_nothing(self) -> None:
        self.enter(self.alice_socket)
        bob_client, _ = self.member("bob")
        self.alice_socket.get_received()

        bob_second_tab = self.connect(bob_client)
        bob_second_tab.disconnect()
        self.assertEqual(self.members_changed(self.alice_socket), [])

    def test_room_is_told_when_someone_joins_or_leaves(self) -> None:
        self.enter(self.alice_socket)
        bob = self.offline_member("bob")
        self.assertEqual(self.members_changed(self.alice_socket), [{"room_id": self.room["id"]}])
        bob.post(f"/api/rooms/{self.room['id']}/join")  # already a member: nothing changes
        self.assertEqual(self.members_changed(self.alice_socket), [])
        bob.post(f"/api/rooms/{self.room['id']}/leave")
        self.assertEqual(self.members_changed(self.alice_socket), [{"room_id": self.room["id"]}])

    def test_only_sockets_with_the_room_open_are_told(self) -> None:
        other = self.create_room("other")
        _, bob_socket = self.member("bob", room=other)
        self.enter(bob_socket, room=other)
        outsider = self.outsider("carol")
        for sio in (self.alice_socket, bob_socket, outsider):
            sio.get_received()

        self.offline_member("dave")
        for sio in (self.alice_socket, bob_socket, outsider):
            with self.subTest(sio=sio):
                self.assertEqual(self.members_changed(sio), [])
