"""Real asset writes with fake HTTP and controlled rendering threads."""

import asyncio
import io
import threading
from types import SimpleNamespace

import aiohttp
import pytest
from PIL import Image

from tieba_core.rika_render import render_card_rika
from tieba_core.rika_render.render import _FALLBACK_BG_PATH, ShareCardRenderer
from tieba_core.rika_render.resources import RENDER_RESOURCES_KEY


def jpeg_bytes():
    output = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(output, format="JPEG")
    return output.getvalue()


class Content:
    def __init__(self, response):
        self.response = response

    async def iter_chunked(self, _):
        state = self.response.state
        if "slow" in self.response.url:
            state["started"].set()
            try:
                await asyncio.Future()
            finally:
                state["cancelled"].set()
                if state.get("release_cleanup") is not None:
                    await state["release_cleanup"].wait()
        yield jpeg_bytes()


class Response:
    status = 200
    content_length = None

    def __init__(self, url, state):
        self.url = url
        self.state = state
        self.content = Content(self)
        if state.get("fail_cover") and "cover" in url:
            self.status = 404

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.state["closed"].append(self.url)
        return False


class Session:
    def __init__(self, state):
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def get(self, url, **_):
        self.state["calls"].append(url)
        return Response(url, self.state)


def network(monkeypatch, **extra):
    state = {
        "calls": [],
        "closed": [],
        "started": asyncio.Event(),
        "cancelled": asyncio.Event(),
        **extra,
    }
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **_: Session(state))
    return state


def metadata(tmp_path, *, video=True):
    borrowed = tmp_path / "borrowed-main.jpg"
    borrowed.write_bytes(jpeg_bytes())
    return {
        "platform": "tieba",
        "url": "https://tieba.baidu.com/p/11180655858",
        "title": "帖子",
        "desc": "主楼正文",
        "author": "作者",
        "avatar_url": "https://himg.bdimg.com/sys/portrait/item/author",
        "video_urls": [["https://tb-video.bdstatic.com/video.mp4"]] if video else [],
        "video_cover_urls": [["https://tiebapic.baidu.com/cover.jpg"]],
        "image_urls": [["https://tiebapic.baidu.com/main.jpg"]],
        "file_paths": [None, str(borrowed)] if video else [str(borrowed)],
        "_card_include_hot_comments": True,
        "hot_comments": [
            {
                "username": "回帖用户",
                "message": "回帖正文",
                "likes": None,
                "avatar_url": "https://himg.bdimg.com/sys/portrait/item/comment",
            }
        ],
    }, borrowed


def paths(images):
    return [
        path
        for path in [
            images["avatar"],
            images["hero"],
            *images["grid"],
            *images["comment_avatars"].values(),
        ]
        if path
    ]


def save_card(_, result, images, output):
    assert all(path.is_file() for path in paths(images))
    Image.new("RGB", (4, 4), "red").save(output)
    return output


def test_success_deletes_only_new_assets_and_does_not_mutate_metadata(
    tmp_path, monkeypatch
):
    async def run():
        state = network(monkeypatch)
        data, borrowed = metadata(tmp_path)
        original_fallback = _FALLBACK_BG_PATH.read_bytes()
        monkeypatch.setattr(ShareCardRenderer, "_render_sync", save_card)
        output = await render_card_rika(data, save_dir=str(tmp_path))
        assert output and output.is_file()
        assert len(state["calls"]) == 3
        assert list(tmp_path.glob("tieba_rika_*.jpg")) == []
        assert list(tmp_path.glob("*.tmp.png")) == []
        assert borrowed.read_bytes() == jpeg_bytes()
        assert _FALLBACK_BG_PATH.read_bytes() == original_fallback
        assert RENDER_RESOURCES_KEY not in data and "_render_save_dir" not in data

    asyncio.run(run())


def test_render_failure_removes_partial_output_and_new_assets(tmp_path, monkeypatch):
    async def run():
        network(monkeypatch)
        data, borrowed = metadata(tmp_path)

        def fail(_, result, images, output):
            assert all(path.is_file() for path in paths(images))
            output.write_bytes(b"partial PNG")
            raise RuntimeError("renderer failed after creating output")

        monkeypatch.setattr(ShareCardRenderer, "_render_sync", fail)
        assert await render_card_rika(data, save_dir=str(tmp_path)) is None
        assert list(tmp_path.glob("tieba_rika_*.jpg")) == []
        assert list(tmp_path.glob("*.png")) == []
        assert borrowed.is_file()

    asyncio.run(run())


def test_real_renderer_publishes_valid_png_and_reclaims_downloads(
    tmp_path, monkeypatch
):
    async def run():
        network(monkeypatch)
        data, borrowed = metadata(tmp_path)
        output = await render_card_rika(data, save_dir=str(tmp_path), width=520)
        assert output and output.is_file()
        with Image.open(output) as image:
            assert image.format == "PNG"
            image.verify()
        assert list(tmp_path.glob("tieba_rika_*.jpg")) == []
        assert list(tmp_path.glob("*.tmp.png")) == []
        assert borrowed.is_file()

    asyncio.run(run())


def test_fallback_used_by_renderer_is_never_deleted(tmp_path, monkeypatch):
    async def run():
        network(monkeypatch, fail_cover=True)
        data, borrowed = metadata(tmp_path)
        original = _FALLBACK_BG_PATH.read_bytes()

        def render_with_fallback(_, result, images, output):
            assert images["hero"] == _FALLBACK_BG_PATH
            return save_card(_, result, images, output)

        monkeypatch.setattr(ShareCardRenderer, "_render_sync", render_with_fallback)
        assert await render_card_rika(data, save_dir=str(tmp_path))
        assert _FALLBACK_BG_PATH.read_bytes() == original
        assert borrowed.is_file()
        assert list(tmp_path.glob("tieba_rika_*.jpg")) == []

    asyncio.run(run())


def test_borrowed_file_with_temporary_prefix_is_not_adopted(tmp_path, monkeypatch):
    async def run():
        network(monkeypatch)
        data, borrowed = metadata(tmp_path)
        borrowed = borrowed.rename(tmp_path / "tieba_rika_borrowed.jpg")
        data["file_paths"][1] = str(borrowed)
        monkeypatch.setattr(ShareCardRenderer, "_render_sync", save_card)
        assert await render_card_rika(data, save_dir=str(tmp_path))
        assert borrowed.read_bytes() == jpeg_bytes()
        assert list(tmp_path.glob("tieba_rika_*.jpg")) == [borrowed]

    asyncio.run(run())


def test_repeated_cancellation_waits_for_real_thread_before_cleanup(
    tmp_path, monkeypatch
):
    async def run():
        network(monkeypatch)
        data, borrowed = metadata(tmp_path)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        seen = []

        def block(_, result, images, output):
            seen.extend(paths(images))
            started.set()
            try:
                assert release.wait(3)
                assert all(path.is_file() for path in seen)
                output.write_bytes(b"worker completed after cancellation")
                return output
            finally:
                finished.set()

        monkeypatch.setattr(ShareCardRenderer, "_render_sync", block)
        task = asyncio.create_task(render_card_rika(data, save_dir=str(tmp_path)))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert all(path.is_file() for path in seen)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert list(tmp_path.glob("tieba_rika_*.jpg")) == []
        assert list(tmp_path.glob("*.png")) == []
        assert borrowed.is_file()

    asyncio.run(run())


def test_download_cancellation_drains_all_tasks_and_finished_assets(
    tmp_path, monkeypatch
):
    async def run():
        state = network(monkeypatch)
        data, borrowed = metadata(tmp_path)
        data["hot_comments"][0]["avatar_url"] += "-slow"
        task = asyncio.create_task(render_card_rika(data, save_dir=str(tmp_path)))
        await asyncio.wait_for(state["started"].wait(), 1)
        assert list(tmp_path.glob("tieba_rika_*.jpg"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state["cancelled"].is_set()
        assert set(state["closed"]) == set(state["calls"])
        assert list(tmp_path.glob("tieba_rika_*.jpg")) == []
        assert list(tmp_path.glob("*.png")) == []
        assert borrowed.is_file()

    asyncio.run(run())


def test_cache_hit_stops_eager_downloads_without_removing_cached_card(
    tmp_path, monkeypatch
):
    async def run():
        state = network(monkeypatch)
        data, borrowed = metadata(tmp_path)
        renderer = ShareCardRenderer(tmp_path)
        cached = renderer._output_path("stable", SimpleNamespace(extra={}))
        cached.write_bytes(b"cached PNG")
        output = await render_card_rika(
            data, save_dir=str(tmp_path), cache_key="stable"
        )
        assert output == cached
        assert cached.read_bytes() == b"cached PNG"
        assert state["calls"] == []
        assert borrowed.is_file()
        assert list(tmp_path.glob("tieba_rika_*.jpg")) == []

    asyncio.run(run())


def test_cancellation_during_download_shutdown_removes_unreturned_card(
    tmp_path, monkeypatch
):
    async def run():
        release = asyncio.Event()
        state = network(monkeypatch, release_cleanup=release)
        data, borrowed = metadata(tmp_path, video=False)
        # A non-video post does not consume this eagerly created cover task.
        data["video_cover_urls"] = [["https://tiebapic.baidu.com/unused-slow.jpg"]]
        monkeypatch.setattr(ShareCardRenderer, "_render_sync", save_card)
        task = asyncio.create_task(render_card_rika(data, save_dir=str(tmp_path)))
        await asyncio.wait_for(state["cancelled"].wait(), 1)
        assert list(tmp_path.glob("tieba_card_*.png"))
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(tmp_path.glob("*.png")) == []
        assert list(tmp_path.glob("tieba_rika_*.jpg")) == []
        assert borrowed.is_file()

    asyncio.run(run())


def test_cancelling_one_same_key_render_preserves_other_render_output(
    tmp_path, monkeypatch
):
    async def run():
        network(monkeypatch)
        data, borrowed = metadata(tmp_path)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        seen = []

        def block_or_save(_, result, images, output):
            if not started.is_set():
                seen.extend(paths(images))
                started.set()
                try:
                    assert release.wait(3)
                    assert all(path.is_file() for path in seen)
                    output.write_bytes(b"cancelled render")
                    return output
                finally:
                    finished.set()
            return save_card(_, result, images, output)

        monkeypatch.setattr(ShareCardRenderer, "_render_sync", block_or_save)
        first = asyncio.create_task(
            render_card_rika(data, save_dir=str(tmp_path), cache_key="same")
        )
        try:
            assert await asyncio.to_thread(started.wait, 1)
            output = await render_card_rika(
                data, save_dir=str(tmp_path), cache_key="same"
            )
            assert output and output.is_file()
            first.cancel()
            await asyncio.sleep(0)
            assert all(path.is_file() for path in seen)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert finished.is_set()
        assert output.is_file()
        assert list(tmp_path.glob("tieba_rika_*.jpg")) == []
        assert list(tmp_path.glob("*.tmp.png")) == []
        assert borrowed.is_file()
        assert RENDER_RESOURCES_KEY not in data

    asyncio.run(run())
