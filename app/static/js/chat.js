// Mirrors app.db.Permission
const Permission = Object.freeze({ NONE: "none", MEMBER: "member" });
// How long a sent message may wait for the server's confirmation before it counts as unconfirmed.
const ACK_TIMEOUT_MS = 5000;

const roomLists = {
  [Permission.MEMBER]: document.getElementById("rooms-member"),
  [Permission.NONE]: document.getElementById("rooms-none"),
};
const roomTitle = document.getElementById("room-title");
const memberList = document.getElementById("members");
const messageList = document.getElementById("messages");
const messageForm = document.getElementById("message-form");
const createRoomForm = document.getElementById("create-room-form");
const errorBox = document.getElementById("error");
const connection = document.getElementById("connection");

const socket = io(ORIGIN);

let activeRoom = null;
const shown = new Set(); // ids of messages already rendered for activeRoom
let lastId = 0; // the highest of them: after a reconnect only the messages after it are fetched
// The message sent but not confirmed yet: {room_id, content, op_id}. Every attempt to send it uses
// the same op_id, so the server stores it once however many of the attempts reach it.
let pending = null;

// Room changes and incoming messages are handled one at a time, in arrival order.
let queue = Promise.resolve();
function enqueue(task) {
  queue = queue.then(task).catch((error) => {
    errorBox.textContent = error.message;
  });
}

function emitAck(event, data) {
  // while disconnected socket.io would hold the event and send it after reconnecting, out of order
  if (!socket.connected) return Promise.reject(new Error("not connected to the server"));
  return new Promise((resolve, reject) => {
    socket.emit(event, data, (reply) => (reply.ok ? resolve(reply) : reject(new Error(reply.error))));
  });
}

function showConnection(state, text) {
  connection.dataset.state = state;
  connection.textContent = text;
  // who is online is only known while we are connected ourselves
  memberList.classList.toggle("stale", state !== "online");
}

function roomItem(room) {
  const item = document.createElement("li");
  item.classList.add(`perm-${room.permission}`);
  item.classList.toggle("active", room.id === activeRoom?.id);

  const isMember = room.permission === Permission.MEMBER;
  const name = document.createElement(isMember ? "button" : "span");
  name.className = "room-name";
  name.textContent = room.display_name;

  const action = document.createElement("button");
  if (isMember) {
    name.onclick = () => enqueue(() => openRoom(room));
    action.textContent = "Leave";
    action.onclick = () => enqueue(() => leaveRoom(room));
  } else {
    action.textContent = "Join";
    action.onclick = () => enqueue(() => joinRoom(room));
  }

  item.append(name, action);
  return item;
}

async function loadRooms() {
  const rooms = await api("/api/rooms");
  for (const list of Object.values(roomLists)) list.replaceChildren();
  for (const room of rooms) roomLists[room.permission]?.append(roomItem(room));
}

function memberItem(member) {
  const item = document.createElement("li");
  item.className = member.online ? "online" : "offline";
  item.title = member.online ? "online" : "offline";
  item.textContent = member.you ? `${member.display_name} (you)` : member.display_name;
  return item;
}

async function loadMembers() {
  const room = activeRoom;
  if (!room) return;
  let members;
  try {
    members = await api(`/api/rooms/${room.id}/members`);
  } catch (error) {
    if (error.status === 403 && room === activeRoom) return lostMembership();
    throw error;
  }
  if (room === activeRoom) memberList.replaceChildren(...members.map(memberItem));
}

function renderMessage(message) {
  if (shown.has(message.id)) return;
  shown.add(message.id);
  lastId = Math.max(lastId, message.id);

  const item = document.createElement("li");
  item.dataset.id = message.id;
  const time = document.createElement("time");
  time.dateTime = message.sent_at;
  time.textContent = new Date(message.sent_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const author = document.createElement("strong");
  author.textContent = message.display_name;
  const content = document.createElement("span");
  content.textContent = message.content;
  item.append(time, " ", author, " ", content);

  // the server's order is the id order, also for a message that shows up late (e.g. confirmed by a retry)
  let next = null;
  for (let li = messageList.lastElementChild; li && Number(li.dataset.id) > message.id; li = li.previousElementSibling) {
    next = li;
  }
  messageList.insertBefore(item, next);
  if (!next) messageList.scrollTop = messageList.scrollHeight;
}

function showRoom(room) {
  activeRoom = room;
  shown.clear();
  lastId = 0;
  messageList.replaceChildren();
  memberList.replaceChildren();
  memberList.hidden = room === null;
  messageForm.hidden = room === null;
  roomTitle.textContent = room?.display_name ?? "Pick a room";
}

async function stillMember(room) {
  const { permission } = await api(`/api/rooms/${room.id}/permission`);
  return permission === Permission.MEMBER;
}

async function openRoom(room) {
  await emitAck("enter_room", { room_id: room.id });
  showRoom(room);
  const history = await api(`/api/rooms/${room.id}/messages`);
  history.forEach(renderMessage);
  await loadMembers();
  await loadRooms();
}

// After a reconnect the new connection is in no socket room, and messages may have arrived meanwhile:
// keep what is on screen and fetch only the messages after the last one we have, page by page.
async function resumeRoom() {
  const room = activeRoom;
  if (!(await stillMember(room))) return lostMembership(); // dropped while we were away
  await emitAck("enter_room", { room_id: room.id });
  for (;;) {
    const page = await api(`/api/rooms/${room.id}/messages?after=${lastId}`);
    if (room !== activeRoom || page.length === 0) break;
    page.forEach(renderMessage);
  }
  await loadMembers();
}

async function closeRoom() {
  // a disconnected socket is in no room already
  if (socket.connected) await emitAck("exit_room", {});
  showRoom(null);
}

// Membership was dropped elsewhere (another tab, or while we were offline): stop showing the room.
async function lostMembership() {
  await closeRoom();
  await loadRooms();
}

async function joinRoom(room) {
  await openRoom(await api(`/api/rooms/${room.id}/join`, { method: "POST" }));
}

async function leaveRoom(room) {
  await api(`/api/rooms/${room.id}/leave`, { method: "POST" });
  if (room.id === activeRoom?.id) await closeRoom();
  await loadRooms();
}

function newOpId() {
  // crypto.randomUUID only exists on https and localhost; this page is also opened over plain http on the LAN
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

// Send the pending message. Without an answer it stays pending: it is sent again, under the same
// operation id, on the next reconnect or when Send is pressed again.
async function deliver() {
  const op = pending;
  if (!op) return;
  if (!socket.connected) {
    errorBox.textContent = "Not sent yet: it will be sent when the connection is back.";
    return;
  }
  let reply;
  try {
    reply = await socket.timeout(ACK_TIMEOUT_MS).emitWithAck("send_message", op);
  } catch {
    // maybe stored, maybe not: sending it again is safe either way
    if (pending === op) errorBox.textContent = "Not confirmed yet: press Send to retry, it will not be duplicated.";
    return;
  }
  if (pending === op) {
    pending = null;
    errorBox.textContent = reply.ok ? "" : reply.error;
    const input = messageForm.elements.content;
    if (reply.ok && input.value === op.content) input.value = "";
  }
  // the broadcast usually got here first; renderMessage skips ids already shown
  if (reply.ok) enqueue(() => reply.message.room_id === activeRoom?.id && renderMessage(reply.message));
}

socket.on("connect", () => {
  showConnection("online", "Connected");
  errorBox.textContent = "";
  enqueue(async () => {
    if (activeRoom) await resumeRoom();
    await loadRooms(); // rooms may have been created while we were away
  });
  deliver();
});

socket.on("disconnect", () => {
  showConnection("offline", socket.active ? "Disconnected, reconnecting…" : "Disconnected");
});

socket.on("connect_error", () => {
  // inactive means the server refused us (session gone), not a network blip
  if (!socket.active) location.href = "/";
  else showConnection("offline", "Server unreachable, retrying…");
});

socket.on("message", (message) =>
  enqueue(async () => {
    if (message.room_id !== activeRoom?.id) return;
    if (await stillMember(activeRoom)) renderMessage(message);
    else await lostMembership();
  })
);

socket.on("members_changed", ({ room_id }) =>
  enqueue(async () => {
    if (room_id === activeRoom?.id) await loadMembers();
  })
);

socket.on("rooms_changed", () => enqueue(loadRooms));

messageForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const content = messageForm.elements.content.value;
  // pressing Send again on a message that is still unconfirmed retries it rather than sending a copy
  if (pending?.room_id !== activeRoom.id || pending.content !== content) {
    pending = { room_id: activeRoom.id, content, op_id: newOpId() };
  }
  deliver();
});

createRoomForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const input = createRoomForm.elements.display_name;
  enqueue(async () => {
    const room = await api("/api/rooms", { method: "POST", body: { display_name: input.value } });
    input.value = "";
    await openRoom(room);
  });
});

document.getElementById("logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  location.href = "/";
});
