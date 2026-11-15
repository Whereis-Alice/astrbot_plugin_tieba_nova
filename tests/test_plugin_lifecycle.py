import asyncio
import threading
from unittest.mock import AsyncMock

import pytest
from plugin_test_support import FakeEvent, fake_network, make_plugin


def test_constructor_does_not_start_background_tasks(tmp_path):
    plugin = make_plugin(tmp_path)
    assert plugin._expired_cleanup_task is None
    assert plugin._event_tasks == set()
    assert [parser.name for parser in plugin.parser_manager.parsers] == ["tieba"]


def test_initialize_is_idempotent_and_terminate_closes_clients_once(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.download_manager.shutdown = AsyncMock()
        plugin.metadata_translator.aclose = AsyncMock()
        await plugin.initialize()
        task = plugin._expired_cleanup_task
        await plugin.initialize()
        assert plugin._expired_cleanup_task is task
        await plugin.terminate()
        await plugin.terminate()
        await plugin.initialize()
        assert task.done()
        assert plugin._expired_cleanup_task is None
        plugin.download_manager.shutdown.assert_awaited_once()
        plugin.metadata_translator.aclose.assert_awaited_once()

    asyncio.run(run())


def test_terminate_cancels_active_parse_and_closes_its_http_session(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path)
        parse, connector, sessions = fake_network(monkeypatch, plugin)
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def block(*_):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        parse.side_effect = block
        event = FakeEvent()
        task = asyncio.create_task(plugin.auto_parse(event))
        await asyncio.wait_for(started.wait(), 1)
        await plugin.terminate()
        assert task.cancelled()
        assert stopped.is_set()
        assert connector.closed and sessions[0].closed
        assert not plugin._event_tasks and not plugin._cleanup_tasks
        event.send.assert_not_awaited()
        await plugin.auto_parse(FakeEvent())
        assert len(sessions) == 1

    asyncio.run(run())


def test_terminate_cancels_translation_and_cleans_downloaded_files(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, translation={"enable": True})
        media_path = tmp_path / "downloaded.mp4"
        media_path.write_bytes(b"downloaded media")
        fake_network(monkeypatch, plugin, {
            "url": "https://tieba.baidu.com/p/11180655858", "platform": "tieba",
            "title": "标题", "video_urls": [["https://tb-video.bdstatic.com/test.mp4"]],
            "image_urls": [], "file_paths": [str(media_path)],
        })
        translation_started = asyncio.Event()
        translation_stopped = asyncio.Event()
        download_started = asyncio.Event()

        async def translate(*_, **__):
            translation_started.set()
            try:
                await asyncio.Future()
            finally:
                translation_stopped.set()

        async def download(*_, **__):
            download_started.set()
            await asyncio.Future()

        plugin.metadata_translator.translate_metadata_list = translate
        plugin.download_manager.process_metadata = download
        event = FakeEvent()
        task = asyncio.create_task(plugin.auto_parse(event))
        await asyncio.wait_for(translation_started.wait(), 1)
        await asyncio.wait_for(download_started.wait(), 1)
        await plugin.terminate()
        assert task.cancelled() and translation_stopped.is_set()
        assert plugin._active_media_flows == 0
        assert not media_path.exists()
        event.send.assert_not_awaited()

    asyncio.run(run())


def test_cancelled_cleanup_waits_for_thread_before_returning(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def work():
            started.set()
            release.wait(2)
            finished.set()

        task = asyncio.create_task(plugin._run_blocking_to_completion(work))
        await asyncio.to_thread(started.wait, 1)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        await plugin.terminate()

    asyncio.run(run())


def test_delayed_cleanup_marks_and_preserves_pending_files_on_unload(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        path = tmp_path / "pending-upload.mp4"
        path.write_bytes(b"in flight")
        await plugin._cleanup_delivery_files(
            [str(path)], [{"group_files": [{"path": str(path), "pending": True}]}],
        )
        assert plugin._cleanup_tasks
        await plugin.terminate()
        assert path.exists()
        assert not plugin._cleanup_tasks

    asyncio.run(run())


def test_cancelled_render_cleans_card_completed_before_cancellation(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path)
        fake_network(monkeypatch, plugin)
        started = asyncio.Event()
        card_path = tmp_path / "partly-rendered-card.png"

        async def render(metadata_list, **_):
            card_path.write_bytes(b"rendered card")
            metadata_list[0]["_card_file_path"] = str(card_path)
            started.set()
            await asyncio.Future()

        plugin._render_cards = render
        task = asyncio.create_task(plugin.auto_parse(FakeEvent()))
        await asyncio.wait_for(started.wait(), 1)
        await plugin.terminate()
        assert task.cancelled()
        assert not card_path.exists()

    asyncio.run(run())


def test_translator_closes_even_if_downloader_shutdown_fails(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.download_manager.shutdown = AsyncMock(side_effect=RuntimeError("shutdown"))
        plugin.metadata_translator.aclose = AsyncMock()
        with pytest.raises(RuntimeError, match="shutdown"):
            await plugin.terminate()
        plugin.metadata_translator.aclose.assert_awaited_once()

    asyncio.run(run())
