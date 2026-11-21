import json

import httpx

from ltp.notify import Command, Reply, TelegramBot, parse_text_command

PARENT = 111


class Recorder:
    def __init__(self, updates=None):
        self.calls: list[tuple[str, dict]] = []
        self.updates = updates or []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        return httpx.Response(200, json={"ok": True, "result": {}})


def _bot(rec, offset_box=None):
    box = offset_box if offset_box is not None else {}
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    return TelegramBot("T", frozenset({PARENT}), client,
                       get_offset=lambda: box.get("o"), set_offset=lambda o: box.update(o=o))


def _msg(uid, text, chat=None, update_id=1):
    return {"update_id": update_id,
            "message": {"from": {"id": uid}, "chat": {"id": chat or uid}, "text": text}}


async def test_send_includes_inline_keyboard():
    rec = Recorder()
    await _bot(rec).send("hello", [[("+10", "add:10")]])
    method, body = rec.calls[0]
    assert method == "sendMessage" and body["chat_id"] == PARENT
    assert body["reply_markup"]["inline_keyboard"][0][0] == {"text": "+10", "callback_data": "add:10"}


def test_parse_text_command():
    assert parse_text_command("/add 15") == ("add", "15")
    assert parse_text_command("/Status@my_bot") == ("status", "")
    assert parse_text_command("hi") == ("", "hi")


async def test_text_and_callback_commands_reach_handler_and_offset_advances():
    rec = Recorder([_msg(PARENT, "/add 15", update_id=7),
                    {"update_id": 8, "callback_query": {
                        "id": "cq", "from": {"id": PARENT}, "data": "add:10",
                        "message": {"chat": {"id": PARENT}}}}])
    box = {}
    seen = []

    async def handler(cmd: Command):
        seen.append((cmd.name, cmd.arg))
        return Reply("ok")

    n = await _bot(rec, box).poll_once(handler)
    assert n == 2
    assert seen == [("add", "15"), ("add", "10")]
    assert box["o"] == 9
    assert ("answerCallbackQuery", {"callback_query_id": "cq"}) in rec.calls


async def test_unauthorized_senders_ignored():
    rec = Recorder([_msg(999, "/off", update_id=1),
                    _msg(999, "/off", chat=PARENT, update_id=2),     # stranger in parent's chat id
                    _msg(PARENT, "/off", chat=-500, update_id=3)])   # parent in a group
    called = []

    async def handler(cmd):
        called.append(cmd)
        return None

    await _bot(rec).poll_once(handler)
    assert called == []


async def test_offset_passed_to_get_updates():
    rec = Recorder()
    await _bot(rec, {"o": 42}).poll_once(lambda c: None)
    assert rec.calls[0][1]["offset"] == 42
