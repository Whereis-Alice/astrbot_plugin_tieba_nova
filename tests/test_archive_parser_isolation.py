"""Archive requests retain source data without changing concurrent chat parsers."""

import asyncio
import copy
import zipfile
from unittest.mock import Mock

import pytest
from astrbot.api.message_components import File, Reply
from plugin_test_support import (
    TIEBA_URL,
    FakeConnector,
    FakeEvent,
    FakeSession,
    main,
    make_plugin,
    sent_components,
)


def official_json():
    return {
        "error_code": 0,
        "thread": {"id": 11180655858, "post_id": 10, "title": "归档帖子"},
        "post_list": [
            {"id": 10, "floor": 1, "author_id": 1, "content": [
                {"type": 0, "text": "归档主楼正文"},
                {"type": 3, "origin_src": "https://tiebapic.baidu.com/archive.png"},
                {"type": 5, "link": "https://tb-video.bdstatic.com/archive.mp4"},
            ]},
            {"id": 11, "floor": 2, "author_id": 2, "content": [
                {"type": 0, "text": "楼层归档回复"},
            ]},
        ],
        "user_list": [{"id": 1, "name": "主楼作者"}, {"id": 2, "name": "回复作者"}],
    }


def fake_sessions(monkeypatch):
    monkeypatch.setattr(main, "create_public_only_connector", lambda **_: FakeConnector())
    monkeypatch.setattr(main.aiohttp, "ClientSession", FakeSession)


def zip_event():
    return FakeEvent("贴吧归档", messages=[Reply(id="source", message_str=TIEBA_URL)])


@pytest.mark.parametrize("mode, chat_options", [
    ("全部发送", (True, 4)),
    ("仅文本", (False, 4)),
    ("仅富媒体", (True, 0)),
    ("关闭", None),
])
def test_archive_factory_retains_enabled_content_and_keeps_normal_parser_options(
    tmp_path, mode, chat_options,
):
    plugin = make_plugin(tmp_path, parsers={"tieba": mode}, message={
        "hot_comments": {"count": 4, "tieba": True},
    })
    cfg = plugin.config_manager
    assert cfg.message.hot_comments.count == 4
    normal = cfg.create_parsers()
    archive = cfg.create_parsers(for_archive=True)
    second_archive = cfg.create_parsers(for_archive=True)
    if chat_options is None:
        assert normal == archive == second_archive == plugin.parser_manager.parsers == []
        return
    shared = plugin.parser_manager.parsers[0]
    assert (shared.include_media, shared.hot_comment_count) == chat_options
    assert (normal[0].include_media, normal[0].hot_comment_count) == chat_options
    assert (archive[0].include_media, archive[0].hot_comment_count) == (True, 4)
    assert shared is not normal[0] and shared is not archive[0]
    assert archive[0] is not second_archive[0]
    assert shared.semaphore is not archive[0].semaphore


@pytest.mark.parametrize("reply_options", [{"count": 0}, {"count": 4, "tieba": False}])
def test_archive_factory_respects_global_reply_switch_and_count(tmp_path, reply_options):
    plugin = make_plugin(tmp_path, parsers={"tieba": "仅富媒体"}, message={
        "hot_comments": reply_options,
    })
    (archive,) = plugin.config_manager.create_parsers(for_archive=True)
    assert archive.include_media
    assert archive.hot_comment_count == 0


@pytest.mark.parametrize("mode", ["仅文本", "仅富媒体"])
def test_zip_contains_original_media_and_enabled_floor_replies(tmp_path, monkeypatch, mode):
    async def run():
        plugin = make_plugin(tmp_path, parsers={"tieba": mode}, message={
            "archive": {"command": "贴吧归档"}, "hot_comments": {"count": 4, "tieba": True},
        })
        shared = plugin.parser_manager.parsers[0]
        original_options = (shared.include_media, shared.hot_comment_count)
        fake_sessions(monkeypatch)
        fetched_by = []

        async def fetch(parser, session, thread_id):
            fetched_by.append(parser)
            return copy.deepcopy(official_json())

        async def download(session, metadata, **options):
            assert options["group_file_available"] is False
            assert options["video_cover_only"] is False
            paths = []
            for kind, groups in (("video", metadata["video_urls"]), ("image", metadata["image_urls"])):
                for index, _ in enumerate(groups):
                    suffix = "mp4" if kind == "video" else "png"
                    path = tmp_path / f"original-{kind}-{index}.{suffix}"
                    path.write_bytes(kind.encode())
                    paths.append(str(path))
            metadata["file_paths"] = paths
            return metadata

        monkeypatch.setattr(type(shared), "_fetch_json", fetch)
        monkeypatch.setattr(plugin.download_manager, "process_metadata", download)
        event = zip_event()
        await plugin.auto_parse(event)
        assert fetched_by[0] is not shared
        assert (fetched_by[0].include_media, fetched_by[0].hot_comment_count) == (True, 4)
        assert (shared.include_media, shared.hot_comment_count) == original_options
        files = [item for item in sent_components(event) if isinstance(item, File)]
        assert len(files) == 1
        with zipfile.ZipFile(files[0].file) as archive:
            names = archive.namelist()
            video = next(name for name in names if name.endswith("video_001.mp4"))
            image = next(name for name in names if name.endswith("image_001.png"))
            assert archive.read(video) == b"video"
            assert archive.read(image) == b"image"
            text = "\n".join(archive.read(name).decode("utf-8") for name in names if name.endswith(".txt"))
            assert "归档主楼正文" in text
            assert "楼层归档回复" in text
        event.bot.call_action.assert_not_awaited()
        await plugin.terminate()

    asyncio.run(run())


@pytest.mark.parametrize("mode, normal_options", [("仅文本", (False, 4)), ("仅富媒体", (True, 0))])
def test_concurrent_chat_and_zip_keep_independent_parser_objects(tmp_path, monkeypatch, mode, normal_options):
    async def run():
        plugin = make_plugin(tmp_path, parsers={"tieba": mode}, message={
            "archive": {"command": "贴吧归档"}, "hot_comments": {"count": 4, "tieba": True},
        })
        shared = plugin.parser_manager.parsers[0]
        fake_sessions(monkeypatch)
        normal_started = asyncio.Event()
        release_normal = asyncio.Event()
        fetched_by = []
        processed = {}

        async def fetch(parser, session, thread_id):
            fetched_by.append(parser)
            if parser is shared:
                normal_started.set()
                await release_normal.wait()
            return copy.deepcopy(official_json())

        async def process(**options):
            processed[options["zip_requested"]] = options["metadata_list"][0]

        monkeypatch.setattr(type(shared), "_fetch_json", fetch)
        monkeypatch.setattr(plugin, "_process_and_send_metadata", process)
        normal_task = asyncio.create_task(plugin.auto_parse(FakeEvent()))
        try:
            await asyncio.wait_for(normal_started.wait(), 1)
            await plugin.auto_parse(zip_event())
            assert not normal_task.done()
            assert fetched_by[0] is shared and fetched_by[1] is not shared
            assert (shared.include_media, shared.hot_comment_count) == normal_options
            assert processed[True]["video_urls"] and processed[True]["image_urls"]
            assert processed[True]["hot_comments"][0]["message"] == "楼层归档回复"
        finally:
            release_normal.set()
            await normal_task
            await plugin.terminate()
        normal = processed[False]
        assert bool(normal["video_urls"]) is normal_options[0]
        assert bool(normal.get("hot_comments")) is bool(normal_options[1])
        assert normal["_enable_text_metadata"] is (mode == "仅文本")
        assert normal["_enable_rich_media"] is (mode == "仅富媒体")

    asyncio.run(run())


def test_disabled_zip_does_not_create_archive_parser_or_http_session(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, parsers={"tieba": "关闭"}, message={
            "archive": {"command": "贴吧归档"},
        })
        create = Mock(wraps=plugin.config_manager.create_parsers)
        session = Mock(side_effect=AssertionError("disabled request must not create a session"))
        monkeypatch.setattr(plugin.config_manager, "create_parsers", create)
        monkeypatch.setattr(main.aiohttp, "ClientSession", session)
        event = zip_event()
        await plugin.auto_parse(event)
        create.assert_not_called()
        session.assert_not_called()
        event.send.assert_not_awaited()
        await plugin.terminate()

    asyncio.run(run())


def test_rate_limited_zip_does_not_allocate_archive_parsers(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, message={"archive": {"command": "贴吧归档"}}, parse_rate_limit={
            "same_user": {"max_count": 1},
        })
        create = Mock(wraps=plugin.config_manager.create_parsers)
        monkeypatch.setattr(plugin.config_manager, "create_parsers", create)
        monkeypatch.setattr(plugin.parse_record_manager, "filter_links", Mock(return_value=([], [])))
        await plugin.auto_parse(zip_event())
        create.assert_not_called()
        await plugin.terminate()

    asyncio.run(run())
