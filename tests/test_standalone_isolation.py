"""Standalone imports, cache ownership, archive privacy and CDN boundaries."""

import ast
import asyncio
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from PIL import Image

from tieba_core.constants import Config
from tieba_core.message_adapter import archive_builder
from tieba_core.parser.platform import TiebaParser
from tieba_core.rika_render import downloader
from tieba_core.rika_render.render import DEFAULT_WATERMARK_TAG, ShareCardRenderer
from tieba_core.storage.cache_marker import (
    EXPIRY_FILE_NAME,
    MARKER_FILE_NAME,
    cleanup_expired_marked_in,
    cleanup_marked_in,
    has_marker,
    mark_subdir_expires_at,
    stamp_subdir,
)
from tieba_core.storage.file_cleaner import cleanup_directory, cleanup_file
from tieba_core.storage.parse_record import ParseRecordManager

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FOREIGN_MARKER = ".astrbot_media_parser_nova"


def foreign_cache(path):
    path.mkdir(parents=True)
    (path / FOREIGN_MARKER).write_text("", encoding="utf-8")
    (path / (FOREIGN_MARKER + ".expire")).write_text("1", encoding="utf-8")
    (path / "keep.txt").write_text("Nova data", encoding="utf-8")
    return path


def test_cold_imports_need_only_standalone_package():
    script = """
import json, sys
from tieba_core.parser.platform import TiebaParser
from tieba_core.storage import cache_marker
from tieba_core.message_adapter import archive_builder
from tieba_core.rika_render import render
print('TIEBA_IMPORT_RESULT=' + json.dumps({
 'foreign_modules': [name for name in sys.modules if name == 'nova_core' or name.startswith('nova_core.')],
 'platform_modules': sorted(name for name in sys.modules if name.startswith('tieba_core.parser.platform.')),
 'paths': [TiebaParser.__module__, cache_marker.__file__, archive_builder.__file__, render.__file__],
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    # A real AstrBot SDK can log platform registration to stdout on import.
    reports = [
        line.removeprefix("TIEBA_IMPORT_RESULT=")
        for line in result.stdout.splitlines()
        if line.startswith("TIEBA_IMPORT_RESULT=")
    ]
    assert len(reports) == 1, result.stdout
    data = json.loads(reports[0])
    assert data["foreign_modules"] == []
    assert data["platform_modules"] == [
        "tieba_core.parser.platform.base",
        "tieba_core.parser.platform.tieba",
    ]
    assert data["paths"][0] == "tieba_core.parser.platform.tieba"
    for path in data["paths"][1:]:
        assert Path(path).resolve().is_relative_to(PROJECT_ROOT)


def test_all_runtime_imports_are_standalone_and_platforms_are_tieba_only():
    for source in (PROJECT_ROOT / "tieba_core").rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(
                name == "nova_core" or name.startswith("nova_core.") for name in names
            ), source
            assert not any(
                name.startswith("astrbot_plugin_media_parser_nova") for name in names
            ), source
    assert {
        p.name for p in (PROJECT_ROOT / "tieba_core/parser/platform").glob("*.py")
    } == {
        "__init__.py",
        "base.py",
        "tieba.py",
    }
    assert not (PROJECT_ROOT / "tieba_core/interaction").exists()
    assert not (PROJECT_ROOT / "tieba_core/runtime_manager").exists()
    for url in (
        "https://www.zhihu.com/answer/123",
        "https://www.bilibili.com/video/BV1test",
        "https://www.tiktok.com/@a/video/123",
    ):
        assert not TiebaParser().can_parse(url)


def test_runtime_paths_remain_separate_even_with_shared_cache(tmp_path):
    assert Config.PLUGIN_NAME == "astrbot_plugin_tieba_nova"
    assert "tieba_nova" in Config.DEFAULT_CACHE_DIR
    own = Path(Config.build_runtime_dir(str(tmp_path), "cards"))
    original = tmp_path / "runtime_manager" / "cards"
    assert own != original
    assert own.parent.name == "tieba_nova_runtime"
    assert own.is_relative_to(tmp_path)


@pytest.mark.parametrize("expired", [False, True])
def test_shared_parent_cleanup_preserves_nova_cache(tmp_path, expired):
    foreign = foreign_cache(tmp_path / "original")
    own = tmp_path / "tieba"
    stamp_subdir(str(own))
    (own / "own.txt").write_text("Tieba data", encoding="utf-8")
    assert mark_subdir_expires_at(str(own), 1)
    if expired:
        result = cleanup_expired_marked_in(str(tmp_path), now=2)
    else:
        result = cleanup_marked_in(str(tmp_path))
    assert result[0] == 1
    assert not own.exists()
    assert (foreign / "keep.txt").read_text(encoding="utf-8") == "Nova data"
    assert (foreign / FOREIGN_MARKER).exists()


def test_foreign_and_dual_markers_cannot_be_adopted_or_deleted(tmp_path):
    foreign = foreign_cache(tmp_path / "foreign")
    stamp_subdir(str(foreign))
    assert not (foreign / MARKER_FILE_NAME).exists()
    (foreign / MARKER_FILE_NAME).write_text("", encoding="utf-8")
    assert not has_marker(str(foreign))
    assert not mark_subdir_expires_at(str(foreign), 1)
    assert not cleanup_file(str(foreign / "keep.txt"))
    assert not cleanup_directory(str(foreign))
    assert cleanup_marked_in(str(tmp_path)) == (0, 0, 0)
    assert (foreign / "keep.txt").exists()


def test_nested_foreign_cache_files_cannot_be_deleted_or_adopted(tmp_path):
    foreign = foreign_cache(tmp_path / "foreign")
    nested = foreign / "nested"
    nested.mkdir()
    file = nested / "keep.txt"
    file.write_text("nested Nova data", encoding="utf-8")
    stamp_subdir(str(nested))
    assert not (nested / MARKER_FILE_NAME).exists()
    assert not cleanup_file(str(file))
    assert not cleanup_directory(str(nested))
    assert file.exists()


def test_same_thread_share_aliases_use_one_rate_limit_key():
    links = (
        "https://tieba.baidu.com/p/11180655858?share=9105&pn=2",
        "http://www.tieba.baidu.com/p/11180655858",
        "https://tieba.baidu.com/mo/q/m?kz=11180655858",
        "https://tieba.baidu.com/mo/q/m?tn=bdPList&z=11180655858",
    )
    assert {ParseRecordManager.build_link_key(url, "tieba") for url in links} == {
        "tieba:https://tieba.baidu.com/p/11180655858"
    }


def test_cleanup_does_not_follow_linked_cache_directory(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    target = tmp_path / "outside"
    stamp_subdir(str(target))
    (target / "keep.txt").write_text("outside", encoding="utf-8")
    link = cache / "alias"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        if result.returncode:
            pytest.skip("Windows could not create a temporary junction")
    else:
        link.symlink_to(target, target_is_directory=True)
    assert not has_marker(str(link))
    assert cleanup_marked_in(str(cache)) == (0, 0, 0)
    assert not cleanup_directory(str(link))
    assert (target / "keep.txt").exists()


def test_nonfinite_expiry_does_not_delete_live_cache(tmp_path):
    own = tmp_path / "live"
    stamp_subdir(str(own))
    assert not mark_subdir_expires_at(str(own), float("nan"))
    (own / EXPIRY_FILE_NAME).write_text("nan", encoding="utf-8")
    assert cleanup_expired_marked_in(str(tmp_path), now=100) == (0, 0, 0)
    assert own.exists()


def test_archive_has_own_names_floor_semantics_and_no_credentials(tmp_path):
    metadata = {
        "platform": "tieba",
        "title": "帖子",
        "desc": "主楼",
        "author": "作者",
        "forum_name": "星之卡比",
        "stats_line": "👍 4 💬 2",
        "comment_section_title": "楼层回复",
        "url": "https://tieba.baidu.com/p/11180655858",
        "image_urls": [],
        "video_urls": [],
        "image_headers": {"Cookie": "BDUSS=secret"},
        "video_headers": {"Authorization": "private"},
        "hot_comments": [
            {
                "username": "用户",
                "message": "回帖",
                "likes": 0,
                "position_text": "第 2 楼",
            },
            {
                "username": "用户2",
                "message": "回帖2",
                "likes": None,
                "position_text": "第 3 楼",
            },
        ],
    }
    archive_path = Path(
        archive_builder.build_zip_archive([metadata], output_dir=str(tmp_path))
    )
    assert archive_path.name == "tieba_nova.zip"
    assert archive_path.parent.name.startswith("tieba_nova_zip_")
    assert has_marker(str(archive_path.parent))
    with zipfile.ZipFile(archive_path) as archive:
        assert all(name.startswith("tieba_nova/") for name in archive.namelist())
        text = archive.read(
            next(n for n in archive.namelist() if n.endswith("metadata.txt"))
        ).decode()
        details = archive.read(
            next(n for n in archive.namelist() if n.endswith("details.json"))
        ).decode()
        assert "楼层回复：" in text and "热评：" not in text
        assert "点赞=0" in text and "点赞=未知" in text
        assert "第 2 楼" in text and "贴吧：星之卡比" in text
        assert "BDUSS=secret" not in details and "private" not in details
        data = json.loads(details)
        assert data["hot_comments"][1]["likes"] is None
        assert data["forum_name"] == "星之卡比"
    archive_builder.cleanup_zip_archive(str(archive_path))
    assert not archive_path.parent.exists()


def test_zip_cleanup_requires_own_marker_even_with_own_looking_name(
    tmp_path, monkeypatch
):
    foreign = foreign_cache(tmp_path / "tieba_nova_zip_foreign")
    path = foreign / "tieba_nova.zip"
    path.write_bytes(b"foreign")
    archive_builder.cleanup_zip_archive(str(path))
    assert path.exists()
    monkeypatch.setattr(archive_builder.tempfile, "gettempdir", lambda: str(tmp_path))
    assert archive_builder.cleanup_expired_zip_workspaces(now=100) == (0, 0)
    assert path.exists()


def test_default_watermark_and_cache_filename_belong_to_tieba(tmp_path):
    assert DEFAULT_WATERMARK_TAG == "贴吧解析"
    assert (PROJECT_ROOT / "tieba_core/rika_render/assets/fallback_tieba.png").is_file()
    renderer = ShareCardRenderer(tmp_path)
    result = SimpleNamespace(
        extra={},
        platform=SimpleNamespace(name="tieba"),
        title="帖子",
        timestamp=None,
        url="https://tieba.baidu.com/p/123",
    )
    assert renderer._output_path(None, result).name.startswith("tieba_card_")


class _Content:
    def __init__(self, payload):
        self.payload = payload

    async def iter_chunked(self, _):
        yield self.payload


class _Response:
    def __init__(self, payload, status=200):
        self.status = status
        self.content_length = None
        self.content = _Content(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _Session:
    def __init__(self, response, calls):
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def image_payload():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(output, format="JPEG")
    return output.getvalue()


def test_card_image_uses_official_cdn_without_cookies_or_redirects(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        lambda **_: _Session(_Response(image_payload()), calls),
    )
    metadata = {
        "image_headers": {
            "Cookie": "BDUSS=secret",
            "Authorization": "private",
            "Referer": "https://evil.test/",
            "User-Agent": "Test",
        }
    }
    path = asyncio.run(
        downloader._download_one(
            "http://tb.himg.baidu.com/sys/portrait/item/token?t=1", tmp_path, metadata
        )
    )
    assert path and path.name.startswith("tieba_rika_")
    url, request = calls[0]
    assert url == "https://himg.bdimg.com/sys/portrait/item/token?t=1"
    assert request["headers"]["Referer"] == "https://tieba.baidu.com/"
    assert (
        "Cookie" not in request["headers"] and "Authorization" not in request["headers"]
    )
    assert not request["allow_redirects"]


def test_card_image_blocks_foreign_hosts_and_oversized_bodies(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        lambda **_: _Session(_Response(image_payload()), calls),
    )
    assert (
        asyncio.run(downloader._download_one("https://evil.test/a.jpg", tmp_path, {}))
        is None
    )
    assert calls == []
    monkeypatch.setattr(downloader, "MAX_CARD_IMAGE_BYTES", 8)
    assert (
        asyncio.run(
            downloader._download_one("https://tiebapic.baidu.com/a.jpg", tmp_path, {})
        )
        is None
    )
    assert list(tmp_path.iterdir()) == []


def test_card_image_redirect_is_rejected_without_second_request(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        lambda **_: _Session(_Response(b"", status=302), calls),
    )
    assert (
        asyncio.run(
            downloader._download_one("https://tiebapic.baidu.com/a.jpg", tmp_path, {})
        )
        is None
    )
    assert len(calls) == 1
    assert not calls[0][1]["allow_redirects"]
