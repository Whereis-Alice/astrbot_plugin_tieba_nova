"""Use the real SDK/components with local fake events and no network sends."""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

Plain = pytest.importorskip("astrbot.api.message_components").Plain

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
main = importlib.import_module("astrbot_plugin_tieba_nova.main")

TIEBA_URL = "https://tieba.baidu.com/p/11180655858"


class FakeEvent:
    def __init__(
        self, text=TIEBA_URL, *, messages=None, private=False, sender_id="222",
        group_id="333", platform="aiocqhttp",
    ):
        self.message_str = text
        self.message_obj = SimpleNamespace(message_id="current-message")
        self.unified_msg_origin = f"{platform}:GroupMessage:{group_id}"
        self._messages = messages or []
        self._private = private
        self._sender_id = sender_id
        self._group_id = "" if private else group_id
        self._platform = platform
        self.send = AsyncMock()
        self.bot = SimpleNamespace(call_action=AsyncMock(return_value={"status": "ok"}))

    def get_messages(self):
        return self._messages

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return "999"

    def get_group_id(self):
        return self._group_id

    def get_platform_name(self):
        return self._platform

    def is_private_chat(self):
        return self._private

    @staticmethod
    def plain_result(text):
        return [Plain(text)]

    @staticmethod
    def chain_result(chain):
        return chain


class FakeConnector:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *, connector, **_):
        self.connector = connector
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True
        await self.connector.close()


def make_plugin(tmp_path, **overrides):
    config = {
        "download": {"cache_dir": str(tmp_path / "cache")},
        "message": {"opening": {"enable": False}, "card_render": {"enable": False}},
    }
    for section, value in overrides.items():
        config.setdefault(section, {}).update(value)
    return main.TiebaNovaPlugin(SimpleNamespace(add_llm_tools=Mock()), config)


def fake_network(monkeypatch, plugin, metadata=None, *, all_instances=False):
    connector = FakeConnector()
    sessions = []

    def create_session(**kwargs):
        session = FakeSession(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(main, "create_public_only_connector", lambda **_: connector)
    monkeypatch.setattr(main.aiohttp, "ClientSession", create_session)
    parsed = metadata or {
        "url": TIEBA_URL, "platform": "tieba", "title": "帖子标题", "desc": "主楼正文",
        "author": "作者", "video_urls": [], "image_urls": [],
    }
    parser = plugin.parser_manager.parsers[0]
    parse = AsyncMock(return_value=parsed)
    monkeypatch.setattr(type(parser) if all_instances else parser, "parse", parse)
    return parse, connector, sessions


def sent_components(event):
    return [component for call in event.send.await_args_list for component in call.args[0]]
