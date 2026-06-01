import asyncio
from types import SimpleNamespace

from matrixzulipbridge.control_room import ControlRoom
from matrixzulipbridge.room import Room


class FakeIntent:
    def __init__(self):
        self.events = []

    async def send_message_event(self, room_id, event_type, content, timestamp=None):
        self.events.append(
            {
                "room_id": room_id,
                "event_type": event_type,
                "content": dict(content),
                "timestamp": timestamp,
            }
        )
        return f"${len(self.events)}"


class FakeService:
    user_id = "@zulipbridge:example.org"
    server_name = "example.org"
    config = {"organizations": {}, "allow": {}, "owner": "@owner:example.org"}

    def is_admin(self, user_id):
        return user_id == self.config["owner"]

    def find_rooms(self, *args, **kwargs):
        return []


class MessageContent(dict):
    def __init__(self, body, msgtype="m.text"):
        super().__init__()
        self.body = body
        self.msgtype = msgtype

    def get_edit(self):
        return None


async def make_control_room():
    intent = FakeIntent()
    Room.init_class(SimpleNamespace(intent=intent))
    room = ControlRoom(
        id="!control:example.org",
        user_id="@owner:example.org",
        serv=FakeService(),
        members=["@zulipbridge:example.org", "@owner:example.org"],
        bans=[],
    )
    return room, intent


async def drain(room):
    await asyncio.sleep(0.2)
    await room._queue._chain.join()


def test_control_room_runs_text_commands():
    async def run():
        room, intent = await make_control_room()
        try:
            await room.on_mx_message(
                SimpleNamespace(
                    sender="@owner:example.org",
                    content=MessageContent("VERSION"),
                )
            )
            await drain(room)
        finally:
            room.cleanup()

        assert intent.events[-1]["content"]["msgtype"] == "m.notice"
        assert "zulipbridge v" in intent.events[-1]["content"]["body"]

    asyncio.run(run())


def test_encrypted_matrix_events_get_an_explanatory_notice_once():
    async def run():
        room, intent = await make_control_room()
        encrypted = SimpleNamespace(
            type="m.room.encrypted",
            sender="@owner:example.org",
            content={},
        )
        try:
            await room.on_mx_event(encrypted)
            await room.on_mx_event(encrypted)
            await drain(room)
        finally:
            room.cleanup()

        notices = [
            event["content"]["body"]
            for event in intent.events
            if event["content"]["msgtype"] == "m.notice"
        ]
        assert len(notices) == 1
        assert "cannot read encrypted Matrix messages" in notices[0]

    asyncio.run(run())
