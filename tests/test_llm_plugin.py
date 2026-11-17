"""Real AstrBot tool registration and plugin lifecycle without chat delivery."""

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api import ToolSet
from astrbot.api.star import Context
from plugin_test_support import (
    FakeEvent,
    fake_network,
    main,
    make_plugin,
)


def test_tool_has_plugin_owner_and_distinct_name(tmp_path):
    context = SimpleNamespace(provider_manager=SimpleNamespace(llm_tools=ToolSet()))
    context.add_llm_tools = lambda *tools: Context.add_llm_tools(context, *tools)
    plugin = main.TiebaNovaPlugin(context, {"download": {"cache_dir": str(tmp_path)}})
    tool = context.provider_manager.llm_tools.get_tool("tieba_fetch_content")
    assert tool is not None and tool.handler is None
    assert type(tool).__module__ == main.__name__
    assert tool.handler_module_path == main.__name__
    assert tool.parameters["properties"]["max_comments"]["default"] == 5
    assert context.provider_manager.llm_tools.get_tool("nova_fetch_content") is None
    assert tool.fetcher is plugin.llm_content_fetcher


def test_disabled_tool_is_not_registered(tmp_path):
    plugin = make_plugin(tmp_path, llm_tool={"enabled": False})
    plugin.context.add_llm_tools.assert_not_called()


def test_tool_returns_text_and_comments_without_card_or_translation(
    tmp_path, monkeypatch
):
    async def run():
        plugin = make_plugin(
            tmp_path,
            message={
                "hot_comments": {"count": 1},
                "card_render": {"enable": True, "mode": "仅卡片"},
                "text_metadata": {"desc": False},
            },
            translation={"enable": True},
        )
        _, connector, _ = fake_network(
            monkeypatch,
            plugin,
            {
                "title": "帖子原文",
                "desc": "完整主楼正文",
                "platform": "tieba",
                "hot_comments": [
                    {"message": f"第 {floor} 楼回复", "position_text": f"第 {floor} 楼"}
                    for floor in range(2, 12)
                ],
            },
            all_instances=True,
        )
        fetch_module = importlib.import_module(
            type(plugin.llm_content_fetcher).__module__
        )
        monkeypatch.setattr(
            fetch_module, "create_public_only_connector", lambda **_: connector
        )
        plugin._render_cards = AsyncMock()
        plugin.metadata_translator.translate_metadata_list = AsyncMock()
        plugin.download_manager.process_metadata = AsyncMock()
        tool = plugin.context.add_llm_tools.call_args.args[0]
        event = FakeEvent()
        output = await tool.call(
            SimpleNamespace(context=SimpleNamespace(event=event)),
            url=event.message_str,
            max_comments=10,
        )
        result = json.loads(output)
        assert result["results"][0]["desc"] == "完整主楼正文"
        assert len(result["results"][0]["hot_comments"]) == 10
        assert isinstance(output, str)
        event.send.assert_not_awaited()
        plugin._render_cards.assert_not_awaited()
        plugin.metadata_translator.translate_metadata_list.assert_not_awaited()
        plugin.download_manager.process_metadata.assert_not_awaited()
        await plugin.terminate()

    asyncio.run(run())


def test_plugin_unload_cancels_tool_and_closes_session(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path)
        parse, connector, sessions = fake_network(
            monkeypatch, plugin, all_instances=True
        )
        fetch_module = importlib.import_module(
            type(plugin.llm_content_fetcher).__module__
        )
        monkeypatch.setattr(
            fetch_module, "create_public_only_connector", lambda **_: connector
        )
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def block(*_):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        parse.side_effect = block
        event = FakeEvent()
        tool = plugin.context.add_llm_tools.call_args.args[0]
        task = asyncio.create_task(
            tool.call(
                SimpleNamespace(context=SimpleNamespace(event=event)),
                url=event.message_str,
            )
        )
        await asyncio.wait_for(entered.wait(), 1)
        await plugin.terminate()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set() and connector.closed and sessions[0].closed
        assert plugin.llm_content_fetcher._closed
        event.send.assert_not_awaited()

    asyncio.run(run())
