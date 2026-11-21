"""Who is online: the users with at least one connected socket (any tab, any browser).

Kept in memory, not in the database: after a restart nobody is connected, and every client that
reconnects counts again.
"""

from threading import Lock


class Presence:
    def __init__(self) -> None:
        # events run in parallel threads
        self._lock = Lock()
        self._user_of: dict[str, str] = {}  # socket id -> user id
        self._sockets: dict[str, set[str]] = {}  # user id -> socket ids

    def connect(self, sid: str, user_id: str) -> bool:
        """Record a socket; True if it is the user's first one, i.e. they just came online."""
        with self._lock:
            self._user_of[sid] = user_id
            sockets = self._sockets.setdefault(user_id, set())
            sockets.add(sid)
            return len(sockets) == 1

    def disconnect(self, sid: str) -> str | None:
        """Forget a socket; return its user's id if it was their last one, i.e. they just went offline."""
        with self._lock:
            user_id = self._user_of.pop(sid, None)
            if user_id is None:
                return None
            sockets = self._sockets[user_id]
            sockets.discard(sid)
            if sockets:
                return None
            del self._sockets[user_id]
            return user_id

    def is_online(self, user_id: str) -> bool:
        with self._lock:
            return user_id in self._sockets

    def clear(self) -> None:
        with self._lock:
            self._user_of.clear()
            self._sockets.clear()
