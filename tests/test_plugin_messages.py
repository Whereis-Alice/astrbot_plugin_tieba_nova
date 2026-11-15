import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import File, Image, Json, Nodes, Plain, Reply, Video
from plugin_test_support import (
    TIEBA_URL,
    FakeEvent,
    fake_network,
    main,
    make_plugin,
    sent_components,
)


@pytest.mark.parametrize("text", [
    "https://www.bilibili.com/video/BV1x1", "https://x.com/user/status/1",
    "https://tieba.baidu.com.evil.example/p/11180655858", "https://zhihu.com/question/123",
    "贴吧解析 https://store.steampowered.com/app/123/", "你好", "更新 B站Cookie",
])
def test_unrelated_messages_do_not_parse_or_send(tmp_path, monkeypatch, text):
    async def run():
        plugin = make_plugin(tmp_path)
        parse, _, sessions = fake_network(monkeypatch, plugin)
        event = FakeEvent(text)
        await plugin.auto_parse(event)
        parse.assert_not_awaited()
        event.send.assert_not_awaited()
        assert not sessions
        await plugin.terminate()

    asyncio.run(run())


@pytest.mark.parametrize("private", [False, True])
def test_automatic_text_only_thread_is_sent_to_current_event(tmp_path, monkeypatch, private):
    async def run():
        plugin = make_plugin(tmp_path)
        parse, connector, sessions = fake_network(monkeypatch, plugin)
        event = FakeEvent(private=private)
        await plugin.auto_parse(event)
        parse.assert_awaited_once()
        assert any(isinstance(component, Plain) and "主楼正文" in component.text
                   for component in sent_components(event))
        assert connector.closed and sessions[0].closed
        assert not plugin._event_tasks
        event.bot.call_action.assert_not_awaited()
        await plugin.terminate()

    asyncio.run(run())


def test_manual_trigger_and_json_card_use_tieba_links_only(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, trigger={"auto_parse": False})
        parse, _, _ = fake_network(monkeypatch, plugin)
        await plugin.auto_parse(FakeEvent())
        parse.assert_not_awaited()
        event = FakeEvent("贴吧解析", messages=[Json(data={
            "meta": {"news": {"jumpUrl": TIEBA_URL}},
        })])
        await plugin.auto_parse(event)
        parse.assert_awaited_once()
        assert parse.await_args.args[1] == TIEBA_URL
        assert sent_components(event)
        await plugin.terminate()

    asyncio.run(run())


def test_reply_trigger_parses_embedded_card_and_quotes_source(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, trigger={"auto_parse": False, "reply_trigger": True})
        plugin.config_manager.message.text_metadata.quote_user_message = True
        parse, _, _ = fake_network(monkeypatch, plugin)
        reply = Reply(id="quoted-message", message_str="", chain=[Json(data={
            "meta": {"detail_1": {"qqdocurl": TIEBA_URL}},
        })])
        event = FakeEvent("解析贴吧", messages=[reply])
        await plugin.auto_parse(event)
        parse.assert_awaited_once()
        assert any(isinstance(component, Reply) and component.id == "quoted-message"
                   for component in sent_components(event))
        await plugin.terminate()

    asyncio.run(run())


@pytest.mark.parametrize("sender_id, permissions", [
    ("999", {}),
    ("222", {"whitelist": {"enable": True, "group": ["other-group"]}}),
    ("222", {"blacklist": {"enable": True, "user": ["222"]}}),
])
def test_self_and_permission_blocked_events_are_ignored(tmp_path, monkeypatch, sender_id, permissions):
    async def run():
        plugin = make_plugin(tmp_path, permissions=permissions)
        parse, _, _ = fake_network(monkeypatch, plugin)
        event = FakeEvent(sender_id=sender_id)
        await plugin.auto_parse(event)
        parse.assert_not_awaited()
        event.send.assert_not_awaited()
        await plugin.terminate()

    asyncio.run(run())


def test_rate_limit_prevents_duplicate_parse(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, parse_rate_limit={
            "same_user": {"max_count": 1, "window_seconds": 3600},
        })
        parse, _, _ = fake_network(monkeypatch, plugin)
        await plugin.auto_parse(FakeEvent())
        second = FakeEvent()
        await plugin.auto_parse(second)
        parse.assert_awaited_once()
        second.send.assert_not_awaited()
        await plugin.terminate()

    asyncio.run(run())


def test_aggregation_sends_real_forward_nodes(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.config_manager.message.aggregation.mode = "全部聚合"
        fake_network(monkeypatch, plugin)
        event = FakeEvent()
        await plugin.auto_parse(event)
        assert any(isinstance(component, Nodes) for component in sent_components(event))
        await plugin.terminate()

    asyncio.run(run())


def test_card_translation_and_regular_media_keep_separate_delivery_chains(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, translation={"enable": True})
        plugin.config_manager.message.card_render.enabled = True
        parsed = {
            "url": TIEBA_URL, "platform": "tieba", "title": "原文标题", "desc": "原文正文",
            "video_urls": [["https://tb-video.bdstatic.com/test.mp4"]],
            "image_urls": [["https://tiebapic.baidu.com/test.png"]],
        }
        fake_network(monkeypatch, plugin, parsed)
        video_path = tmp_path / "normal-video.mp4"
        image_path = tmp_path / "normal-image.png"
        card_path = tmp_path / "translated-card.png"
        rendered = []

        async def translate(metadata_list, **_):
            metadata_list[0]["_translated_fields"] = {"title": "翻译标题", "desc": "翻译正文"}

        async def download(session, metadata, **_):
            video_path.write_bytes(b"video")
            image_path.write_bytes(b"image")
            metadata.update({
                "video_modes": ["local"], "video_count": 1, "image_count": 1,
                "image_modes": ["local"], "file_paths": [str(video_path), str(image_path)],
                "video_sizes": [1], "has_valid_media": True,
            })
            return metadata

        async def render(metadata, **_):
            rendered.append(dict(metadata))
            card_path.write_bytes(b"card")
            return card_path

        plugin.metadata_translator.translate_metadata_list = translate
        plugin.download_manager.process_metadata = download
        monkeypatch.setitem(
            sys.modules, main.__package__ + ".tieba_core.render", SimpleNamespace(render_card=render),
        )
        event = FakeEvent()
        await plugin.auto_parse(event)
        assert rendered[0]["title"] == "翻译标题"
        assert parsed["title"] == "原文标题" and parsed["desc"] == "原文正文"
        components = sent_components(event)
        assert len([component for component in components if isinstance(component, Image)]) == 2
        assert len([component for component in components if isinstance(component, Video)]) == 1
        event.bot.call_action.assert_not_awaited()
        assert not any(path.exists() for path in (video_path, image_path, card_path))
        await plugin.terminate()

    asyncio.run(run())


@pytest.mark.parametrize("private, action, target", [
    (False, "upload_group_file", {"group_id": 333}),
    (True, "upload_private_file", {"user_id": "222"}),
])
def test_oversize_original_file_routes_to_current_group_or_private_chat(
    tmp_path, monkeypatch, private, action, target,
):
    async def run():
        plugin = make_plugin(tmp_path)
        fake_network(monkeypatch, plugin, {
            "url": TIEBA_URL, "platform": "tieba", "title": "原文件帖子",
            "video_urls": [["https://tb-video.bdstatic.com/test.mp4"]], "image_urls": [],
        })
        downloader_module = sys.modules[type(plugin.download_manager).__module__]
        monkeypatch.setattr(downloader_module, "get_video_size", AsyncMock(return_value=(80, 200)))
        original = b"original signed video bytes"
        uploaded = []

        async def download(*, session, media_items, cache_dir):
            results = []
            for item in media_items:
                path = Path(cache_dir) / f"{item['position']}.mp4"
                path.write_bytes(original)
                results.append({**item, "success": True, "file_path": str(path), "size_mb": 80})
            return results

        async def upload(upload_action, **payload):
            uploaded.append((upload_action, payload, Path(payload["file"]).read_bytes()))
            return {"status": "ok"}

        plugin.download_manager._download_local_items = download
        plugin.download_manager._fit_video_to_send_limit = AsyncMock(
            side_effect=AssertionError("original file must not be compressed"),
        )
        event = FakeEvent(private=private)
        event.bot.call_action = AsyncMock(side_effect=upload)
        await plugin.auto_parse(event)
        assert len(uploaded) == 1
        assert uploaded[0][0] == action
        assert all(uploaded[0][1][key] == value for key, value in target.items())
        assert uploaded[0][2] == original
        plugin.download_manager._fit_video_to_send_limit.assert_not_awaited()
        assert not any(isinstance(component, Video) for component in sent_components(event))
        await plugin.terminate()

    asyncio.run(run())


def test_zip_reply_exports_real_file_and_preserves_it_until_ttl(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, message={"archive": {"command": "贴吧归档"}})
        parse, _, _ = fake_network(monkeypatch, plugin, all_instances=True)
        event = FakeEvent("贴吧归档", messages=[Reply(id="quoted", message_str=TIEBA_URL)])
        await plugin.auto_parse(event)
        parse.assert_awaited_once()
        files = [component for component in sent_components(event) if isinstance(component, File)]
        assert len(files) == 1
        assert Path(files[0].file).is_file()
        assert plugin._cleanup_tasks
        await plugin.terminate()
        assert Path(files[0].file).is_file()

    asyncio.run(run())


def test_admin_cache_cleanup_refuses_while_media_is_in_flight(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, permissions={"admin_id": "222"})
        plugin._active_media_flows = 1
        cleanup = AsyncMock()
        monkeypatch.setattr(plugin, "_run_blocking_to_completion", cleanup)
        event = FakeEvent(plugin.config_manager.admin.clean_cache_keyword, private=True)
        await plugin.auto_parse(event)
        cleanup.assert_not_awaited()
        assert any("正在处理" in component.text for component in sent_components(event))
        await plugin.terminate()

    asyncio.run(run())
