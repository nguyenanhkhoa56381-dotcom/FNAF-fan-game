import json
import os
import secrets
import socket
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from threading import Lock
from urllib.parse import unquote, urlparse


HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))
ROOM_TTL = 60 * 60
PLAYER_TIMEOUT = 20
ROOMS = {}
ROOMS_LOCK = Lock()
DEFAULT_CORS_ORIGINS = {
    "https://fnafangame.netlify.app",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
}


def cors_origins():
    configured = os.environ.get("CORS_ORIGINS")
    if configured is None:
        return DEFAULT_CORS_ORIGINS
    return {origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()}


class Room:
    def __init__(self, token):
        self.host_token = token
        self.guests = {}
        self.state = None
        self.chat_messages = []
        self.next_chat_id = 1
        self.chat_last_sent = {}
        self.last_activity = time.time()


class GameHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_json(self, status, data):
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Origin")
        origin = self.headers.get("Origin")
        if origin in cors_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin")
        if origin not in cors_origins():
            self.send_json(403, {"error": "Origin chưa được cho phép trong CORS_ORIGINS."})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 100_000:
            raise ValueError("Yêu cầu quá lớn.")
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def clean_rooms(self):
        now = time.time()
        expired = [
            code for code, room in ROOMS.items()
            if now - room.last_activity > ROOM_TTL
        ]
        for code in expired:
            del ROOMS[code]

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(200, {"ok": True})
            return
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/fnaf_game.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        decoded_path = unquote(path)
        is_game_asset = (
            decoded_path in ("/fnaf_game.html", "/fnaf map.jpg", "/It's been so long.mp3")
            or (
                decoded_path.startswith("/assets/sprites/")
                and decoded_path.endswith(".png")
                and ".." not in PurePosixPath(decoded_path).parts
            )
        )
        if not is_game_asset:
            self.send_json(404, {"error": "Không tìm thấy tệp."})
            return
        super().do_GET()

    def do_POST(self):
        try:
            payload = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
            return
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "Nội dung yêu cầu phải là một đối tượng JSON."})
            return

        path = unquote(urlparse(self.path).path)
        if path == "/api/rooms":
            self.create_room()
            return

        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "api" or parts[1] != "rooms":
            self.send_json(404, {"error": "Không tìm thấy API."})
            return

        code = parts[2]
        if len(code) != 6 or not code.isascii() or not code.isdigit():
            self.send_json(400, {"error": "Mã phòng phải gồm đúng 6 chữ số."})
            return
        if len(parts) == 4 and parts[3] == "join":
            self.join_room(code, payload)
        elif len(parts) == 4 and parts[3] == "sync":
            self.sync_room(code, payload)
        elif len(parts) == 4 and parts[3] == "leave":
            self.leave_room(code, payload)
        else:
            self.send_json(404, {"error": "Không tìm thấy API."})

    def create_room(self):
        with ROOMS_LOCK:
            self.clean_rooms()
            while True:
                code = f"{secrets.randbelow(1_000_000):06d}"
                if code not in ROOMS:
                    break
            token = secrets.token_urlsafe(24)
            ROOMS[code] = Room(token)
        self.send_json(201, {"code": code, "token": token})

    @staticmethod
    def clean_profile(value, player_id):
        if not isinstance(value, dict):
            value = {}
        raw_name = value.get("name", "")
        name = "".join(character for character in raw_name if character.isprintable()).strip()[:18] if isinstance(raw_name, str) else ""
        character = value.get("character")
        if character not in {f"guard-{index}" for index in range(1, 6)}:
            character = "guard-1"
        return {"name": name or f"player {player_id}", "character": character}

    def join_room(self, code, payload):
        with ROOMS_LOCK:
            self.clean_rooms()
            room = ROOMS.get(code)
            if room is None:
                self.send_json(404, {"error": "Không tìm thấy phòng. Kiểm tra lại mã phòng."})
                return
            now = time.time()
            connected_ids = {
                player_id for player_id, guest in room.guests.items()
                if now - guest["last_seen"] <= PLAYER_TIMEOUT
            }
            available_ids = [player_id for player_id in range(2, 9) if player_id not in connected_ids]
            if not available_ids:
                self.send_json(409, {"error": "Phòng đã đủ 8 người chơi."})
                return
            player_id = available_ids[0]
            token = secrets.token_urlsafe(24)
            room.guests[player_id] = {
                "token": token,
                "last_seen": now,
                "keys": {},
                "sprint": False,
                "actions": [],
                "profile": self.clean_profile(payload.get("profile"), player_id),
            }
            room.last_activity = now
        self.send_json(200, {"code": code, "token": token, "playerId": player_id})

    def sync_room(self, code, payload):
        role = payload.get("role")
        token = payload.get("token")
        with ROOMS_LOCK:
            room = ROOMS.get(code)
            if room is None:
                self.send_json(404, {"error": "Phòng không còn tồn tại."})
                return
            now = time.time()
            room.last_activity = now

            if role == "host" and secrets.compare_digest(str(token or ""), room.host_token):
                state = payload.get("state")
                if isinstance(state, dict):
                    room.state = state
                host_profile = next(
                    (player for player in state.get("players", []) if player.get("id") == 1),
                    {},
                ) if isinstance(state, dict) else {}
                self.add_chat_messages(room, 1, host_profile.get("name", "player 1"), payload.get("chatMessages"), now)
                connected_guests = {
                    player_id: guest for player_id, guest in room.guests.items()
                    if now - guest["last_seen"] <= PLAYER_TIMEOUT
                }
                guest_inputs = {}
                for player_id, guest in connected_guests.items():
                    guest_inputs[str(player_id)] = {
                        "keys": guest["keys"],
                        "sprint": guest["sprint"],
                        "actions": guest["actions"],
                    }
                    guest["actions"] = []
                response = {
                    "connectedPlayerIds": sorted(connected_guests),
                    "guestProfiles": {
                        str(player_id): guest["profile"]
                        for player_id, guest in connected_guests.items()
                    },
                    "inputs": guest_inputs,
                    "chatMessages": self.chat_messages_after(room, payload.get("chatCursor")),
                }
            elif role == "guest":
                try:
                    player_id = int(payload.get("playerId", 0))
                except (TypeError, ValueError):
                    player_id = 0
                guest = room.guests.get(player_id)
                if not guest or not secrets.compare_digest(str(token or ""), guest["token"]):
                    self.send_json(403, {"error": "Phiên tham gia không hợp lệ."})
                    return
                guest["last_seen"] = now
                self.add_chat_messages(
                    room,
                    player_id,
                    guest["profile"]["name"],
                    payload.get("chatMessages"),
                    now,
                )
                input_state = payload.get("input", {})
                if isinstance(input_state, dict):
                    guest["profile"] = self.clean_profile(input_state.get("profile"), player_id)
                    guest["sprint"] = input_state.get("sprint") is True
                    candidate_keys = input_state.get("keys", {})
                    guest["keys"] = {
                        key: True for key in ("w", "a", "s", "d")
                        if isinstance(candidate_keys, dict) and candidate_keys.get(key) is True
                    }
                    actions = input_state.get("actions", [])
                    if isinstance(actions, list):
                        guest["actions"].extend(
                            action for action in actions
                            if action in ("interact", "light", "camera-close")
                            or (isinstance(action, str) and action in {f"camera-{index}" for index in range(1, 6)})
                            or (isinstance(action, str) and action in {"generator-abort", "puzzle-up", "puzzle-down", "puzzle-left", "puzzle-right"})
                            or (isinstance(action, str) and action.startswith("memory-") and action[7:].isdigit() and 0 <= int(action[7:]) <= 3)
                            or (isinstance(action, str) and action.startswith("puzzle-tile-") and action[12:].isdigit() and 0 <= int(action[12:]) <= 8)
                        )
                response = {
                    "state": room.state,
                    "chatMessages": self.chat_messages_after(room, payload.get("chatCursor")),
                }
            else:
                self.send_json(403, {"error": "Phiên tham gia không hợp lệ."})
                return
        self.send_json(200, response)

    @staticmethod
    def chat_messages_after(room, cursor):
        try:
            cursor = max(0, int(cursor))
        except (TypeError, ValueError):
            cursor = 0
        return [message for message in room.chat_messages if message["id"] > cursor]

    @staticmethod
    def add_chat_messages(room, player_id, player_name, messages, now):
        if not isinstance(messages, list) or now - room.chat_last_sent.get(player_id, 0) < 0.9:
            return
        for raw_message in messages[:1]:
            if not isinstance(raw_message, str):
                continue
            text = "".join(character for character in raw_message if character.isprintable()).strip()[:200]
            if not text:
                continue
            raw_name = player_name if isinstance(player_name, str) else ""
            name = "".join(character for character in raw_name if character.isprintable()).strip()[:18]
            name = name or f"player {player_id}"
            room.chat_messages.append({
                "id": room.next_chat_id,
                "playerId": player_id,
                "name": name,
                "text": text,
            })
            room.next_chat_id += 1
            room.chat_last_sent[player_id] = now
            break
        if len(room.chat_messages) > 50:
            del room.chat_messages[:-50]

    def leave_room(self, code, payload):
        role = payload.get("role")
        token = str(payload.get("token") or "")
        with ROOMS_LOCK:
            room = ROOMS.get(code)
            if room is None:
                self.send_json(200, {"ok": True})
                return
            if role == "host" and secrets.compare_digest(token, room.host_token):
                del ROOMS[code]
            elif role == "guest":
                try:
                    player_id = int(payload.get("playerId", 0))
                except (TypeError, ValueError):
                    player_id = 0
                guest = room.guests.get(player_id)
                if guest and secrets.compare_digest(token, guest["token"]):
                    del room.guests[player_id]
                else:
                    self.send_json(403, {"error": "Phiên tham gia không hợp lệ."})
                    return
            else:
                self.send_json(403, {"error": "Phiên tham gia không hợp lệ."})
                return
        self.send_json(200, {"ok": True})

    def log_message(self, format, *args):
        if "/sync" not in self.path:
            print(f"{self.address_string()} - {format % args}")


class GameHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def get_request(self):
        request, client_address = super().get_request()
        request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return request, client_address


def local_addresses():
    addresses = set()
    for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        address = result[4][0]
        if not address.startswith("127."):
            addresses.add(address)
    return sorted(addresses)


if __name__ == "__main__":
    game_directory = Path(__file__).resolve().parent
    handler = partial(GameHandler, directory=str(game_directory))
    server = GameHTTPServer((HOST, PORT), handler)
    print(f"Game server listening on {HOST}:{PORT}.")
    print(f"Open http://localhost:{PORT}/fnaf_game.html on this computer.")
    addresses = local_addresses()
    for address in addresses:
        print(f"On another computer on the same Wi-Fi/LAN, open http://{address}:{PORT}/fnaf_game.html")
    if not addresses:
        print("No LAN IPv4 address found. Check that this computer is connected to Wi-Fi.")
    print("Use the same Wi-Fi network on all devices and allow Python through Windows Firewall on Private networks.")
    print("Do not forward this port to the public internet.")
    print("Keep this window open while you play. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping game server.")
    finally:
        server.server_close()
