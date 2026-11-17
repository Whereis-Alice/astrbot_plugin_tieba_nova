import asyncio
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tieba_core.config_manager import ConfigManager
from tieba_core.llm_fetcher import TiebaContentFetcher, _run_record_operation
from tieba_core.llm_tool import CoreTiebaFetchContentTool
from tieba_core.parser.platform.tieba import TiebaParser
from tieba_core.storage import ParseRecordManager


def make_config(raw=None):
    with (
        patch("tieba_core.config_manager.check_cache_dir_available", return_value=True),
        patch("tieba_core.config_manager._is_docker_environment", return_value=False),
        patch(
            "tieba_core.config_manager._get_astrbot_plugin_cache_dir",
            return_value=str(Path(tempfile.gettempdir()) / "tieba-llm-tests" / "cache"),
        ),
    ):
        return ConfigManager({} if raw is None else raw)


def thread_response():
    return {
        "error_code": 0,
        "thread": {
            "id": 1,
            "post_id": 101,
            "title": "贴吧主楼标题",
            "reply_num": 25,
            "agree": {"agree_num": 7},
        },
        "forum": {"id": 10, "name": "测试吧"},
        "user_list": [{"id": 7, "name_show": "主楼作者"}],
        "post_list": [
            {
                "id": 101,
                "floor": 1,
                "author_id": 7,
                "time": 1700000000,
                "content": [
                    {"type": 0, "text": "主楼正文不会被卡片设置裁剪。" * 12},
                    {
                        "type": 3,
                        "origin_src": "https://tiebapic.baidu.com/forum/pic/item/main.jpg",
                    },
                ],
            },
            *[
                {
                    "id": 1000 + floor,
                    "floor": floor,
                    "author_id": 7,
                    "time": 1700000000 + floor,
                    "content": [{"type": 0, "text": f"第 {floor} 楼完整回复内容"}],
                }
                for floor in range(2, 27)
            ],
        ],
    }


def event(sender="123", private=False, group="456"):
    return SimpleNamespace(
        get_sender_id=lambda: sender,
        get_group_id=lambda: group,
        is_private_chat=lambda: private,
        get_self_id=lambda: "999",
        get_platform_name=lambda: "test",
        send=Mock(side_effect=AssertionError("Tool must not send chat messages")),
    )


class Parser:
    name = "tieba"

    def extract_links(self, text):
        return re.findall(r"https://tieba\.baidu\.com/p/\d+", text)

    async def parse(self, session, url):
        return {
            "url": url,
            "title": "Thread " + url.rsplit("/", 1)[-1],
            "desc": "贴吧主楼正文",
            "hot_comments": [
                {"message": "楼层回复", "likes": 3, "likes_label": "点赞"}
            ],
            "image_urls": [["https://cdn.example.com/screenshot.png"]],
            "video_headers": {"Cookie": "secret"},
            "proxy_url": "http://user:password@proxy.example.com",
        }


class LLMFetcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = make_config()
        self.parser = Parser()
        self.cfg.create_parsers = Mock(return_value=[self.parser])
        self.records = ParseRecordManager(record_file="")
        self.fetcher = TiebaContentFetcher(self.cfg, self.records)

    async def asyncTearDown(self):
        await self.fetcher.aclose()

    async def test_tool_schema_and_current_event_bridge(self):
        tool = CoreTiebaFetchContentTool(fetcher=self.fetcher)
        self.assertIsNone(tool.handler)
        self.assertEqual(tool.name, "tieba_fetch_content")
        self.assertEqual(tool.parameters["required"], ["url"])
        self.assertEqual(tool.parameters["properties"]["max_comments"]["default"], 5)
        output = await tool.call(
            SimpleNamespace(context=SimpleNamespace(event=event())),
            url="https://tieba.baidu.com/p/1",
        )
        result = json.loads(output)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["results"][0]["desc"], "贴吧主楼正文")
        self.assertNotIn("secret", output)
        self.assertNotIn("password", output)

    async def test_real_parser_preserves_main_floor_and_reply_count_despite_card_settings(self):
        self.cfg = make_config(
            {
                "parsers": {"tieba": "仅富媒体"},
                "message": {
                    "text_metadata": {"show_description": False},
                    "hot_comments": {"count": 1, "tieba": True},
                    "card_render": {
                        "enable": True,
                        "include_hot_comments": False,
                        "hot_comment_max_chars": 60,
                    },
                },
            }
        )
        self.fetcher.config = self.cfg
        self.assertFalse(self.cfg.message.text_metadata.show_description)
        self.assertFalse(self.cfg.message.card_render.include_hot_comments)
        self.assertEqual(self.cfg.message.card_render.hot_comment_max_chars, 60)
        (chat_parser,) = self.cfg.create_parsers()
        self.assertEqual(chat_parser.hot_comment_count, 0)
        ev = event()
        with patch.object(TiebaParser, "_fetch_json", AsyncMock(return_value=thread_response())):
            for arguments, expected in (({}, 5), ({"max_comments": 10}, 10), ({"max_comments": 20}, 20)):
                with self.subTest(arguments=arguments):
                    result = json.loads(
                        await self.fetcher.fetch(ev, url="https://tieba.baidu.com/p/1", **arguments)
                    )
                    item = result["results"][0]
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(item["desc"], "主楼正文不会被卡片设置裁剪。" * 12)
                    self.assertEqual(item["author"], "主楼作者")
                    self.assertEqual(item["comment_section_title"], "楼层回复")
                    self.assertEqual(len(item["hot_comments"]), expected)
                    self.assertEqual(item["hot_comments"][0]["message"], "第 2 楼完整回复内容")
                    self.assertEqual(item["hot_comments"][-1]["message"], f"第 {expected + 1} 楼完整回复内容")
                    self.assertTrue(item["images"])
        ev.send.assert_not_called()

    async def test_real_parser_platform_switches_are_preserved(self):
        self.cfg = make_config({"message": {"hot_comments": {"tieba": False}}})
        self.fetcher.config = self.cfg
        with patch.object(TiebaParser, "_fetch_json", AsyncMock(return_value=thread_response())):
            result = json.loads(
                await self.fetcher.fetch(event(), url="https://tieba.baidu.com/p/1", max_comments=20)
            )
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["results"][0].get("hot_comments"))
        self.assertTrue(result["results"][0]["desc"])
        self.fetcher.config = make_config({"parsers": {"tieba": "关闭"}})
        self.assertEqual(
            json.loads(await self.fetcher.fetch(event(), url="https://tieba.baidu.com/p/1"))["error"]["code"],
            "unsupported_url",
        )

    async def test_llm_decides_comment_count_and_omission_defaults_to_five(self):
        original = self.parser.parse

        async def parse(session, url):
            metadata = await original(session, url)
            metadata["hot_comments"] = [
                {"message": f"评论 {index}"} for index in range(20)
            ]
            return metadata

        self.parser.parse = parse
        for args, expected in (
            ({}, 5),
            ({"max_comments": 0}, 0),
            ({"max_comments": 10}, 10),
            ({"max_comments": 20}, 20),
            ({"max_comments": 20, "include_comments": False}, 0),
        ):
            with self.subTest(args=args):
                self.cfg.create_parsers.reset_mock()
                result = json.loads(
                    await self.fetcher.fetch(
                        event(),
                        url="https://tieba.baidu.com/p/1",
                        **args,
                    )
                )
                self.cfg.create_parsers.assert_called_once_with(
                    for_llm=True,
                    include_media=True,
                    hot_comment_count=expected,
                )
                self.assertEqual(result["comments_requested"], expected)
                self.assertEqual(
                    len(result["results"][0].get("hot_comments", [])), expected
                )

    async def test_permission_checked_before_parsers_and_rate_records(self):
        self.cfg.permission.whitelist_enable = True
        self.records.filter_links = Mock(
            side_effect=AssertionError("No rate mutation before permission")
        )
        output = json.loads(
            await self.fetcher.fetch(
                event(), url="https://tieba.baidu.com/p/1"
            )
        )
        self.assertEqual(output["error"]["code"], "permission_denied")
        self.cfg.create_parsers.assert_not_called()

    async def test_missing_context_self_message_and_disabled_are_rejected(self):
        for ev in (None, event(sender="999"), event(sender="")):
            with self.subTest(ev=ev):
                self.assertEqual(
                    json.loads(
                        await self.fetcher.fetch(
                            ev, url="https://tieba.baidu.com/p/1"
                        )
                    )["error"]["code"],
                    "permission_denied",
                )
        self.cfg.llm_tool.enabled = False
        self.assertEqual(
            json.loads(
                await self.fetcher.fetch(
                    event(), url="https://tieba.baidu.com/p/1"
                )
            )["error"]["code"],
            "disabled",
        )
        self.cfg.create_parsers.assert_not_called()

    async def test_runtime_argument_validation(self):
        for args in (
            {},
            {"url": ""},
            {"url": "x" * 12001},
            {"url": 4},
            {"url": "x", "include_media": "false"},
            {"url": "x", "max_comments": True},
            {"url": "x", "max_comments": 21},
            {"url": "x", "sender_id": "admin"},
        ):
            with self.subTest(args=args):
                result = json.loads(await self.fetcher.fetch(event(), **args))
                self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.cfg.create_parsers.assert_not_called()

    async def test_comment_and_media_controls_passed_to_isolated_factory(self):
        result = json.loads(
            await self.fetcher.fetch(
                event(),
                url="https://tieba.baidu.com/p/1",
                include_comments=False,
                include_media=False,
            )
        )
        self.cfg.create_parsers.assert_called_once_with(
            for_llm=True, include_media=False, hot_comment_count=0
        )
        self.assertFalse(result["media_requested"])
        self.assertFalse(result["results"][0].get("images"))
        self.assertFalse(result["results"][0].get("hot_comments"))

    async def test_link_limit_deduplication_and_order(self):
        self.cfg.llm_tool.max_links = 2
        result = json.loads(
            await self.fetcher.fetch(
                event(),
                url=" ".join(
                    f"https://tieba.baidu.com/p/{n}" for n in (2, 1, 2, 3)
                ),
            )
        )
        self.assertEqual(
            [item["title"] for item in result["results"]], ["Thread 2", "Thread 1"]
        )
        self.assertEqual(result["omitted_link_count"], 1)
        self.assertEqual(result["status"], "partial")

    async def test_rate_limits_shared_with_chat(self):
        self.records = ParseRecordManager(
            record_file="", same_user_max_count=1, same_user_window_seconds=60
        )
        self.fetcher.records = self.records
        self.records.filter_links(
            [("https://tieba.baidu.com/p/2", self.parser)], user_key="test:123"
        )
        result = json.loads(
            await self.fetcher.fetch(
                event(), url="https://tieba.baidu.com/p/1"
            )
        )
        self.assertEqual(result["error"]["code"], "rate_limited")

    async def test_tool_records_final_alias_for_subsequent_chat_limit(self):
        self.records = ParseRecordManager(
            record_file="", same_link_max_count=1, same_link_window_seconds=60
        )
        self.fetcher.records = self.records
        original = self.parser.parse

        async def parse(session, url):
            metadata = await original(session, url)
            metadata["url"] = "https://tieba.baidu.com/p/2"
            return metadata

        self.parser.parse = parse
        result = json.loads(
            await self.fetcher.fetch(event(), url="https://tieba.baidu.com/p/1")
        )
        self.assertEqual(result["status"], "ok")
        allowed, blocked = self.records.filter_links(
            [("https://tieba.baidu.com/mo/q/m?kz=2", self.parser)],
            user_key="test:another-user",
        )
        self.assertEqual(allowed, [])
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].scope, "link")

    async def test_one_failed_link_does_not_hide_other_content_or_exception_secrets(
        self,
    ):
        original = self.parser.parse

        async def parse(session, url):
            if url.endswith("/2"):
                # Return an error as parsers do; never expose it to the model.
                return {"error": "Cookie=secret /root/private.json", "source_url": url}
            return await original(session, url)

        self.parser.parse = parse
        output = await self.fetcher.fetch(
            event(),
            url="https://tieba.baidu.com/p/1 https://tieba.baidu.com/p/2",
        )
        result = json.loads(output)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["results"][1]["status"], "error")
        self.assertNotIn("secret", output)
        self.assertNotIn("/root/private", output)

    async def test_raised_parser_exception_is_redacted_in_result_and_logs(self):
        original = self.parser.parse

        async def parse(session, url):
            if url.endswith("/2"):
                raise ValueError("Cookie=secret /root/private.json")
            return await original(session, url)

        self.parser.parse = parse
        with patch("tieba_core.parser.manager.logger") as logger:
            output = await self.fetcher.fetch(
                event(),
                url="https://tieba.baidu.com/p/1 https://tieba.baidu.com/p/2",
            )
        result = json.loads(output)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["results"][1]["status"], "error")
        self.assertNotIn("secret", output)
        self.assertNotIn("/root/private", output)
        self.assertEqual(logger.mock_calls, [])

    async def test_none_and_invalid_parser_results_are_unavailable(self):
        for metadata in (None, [], "Cookie=secret", {}):
            with self.subTest(metadata=metadata):
                self.parser.parse = AsyncMock(return_value=metadata)
                output = await self.fetcher.fetch(event(), url="https://tieba.baidu.com/p/1")
                result = json.loads(output)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["results"][0]["status"], "error")
                self.assertNotIn("secret", output)

    async def test_timeout_preserves_fast_link(self):
        self.cfg.llm_tool.timeout_seconds = 0.02
        original = self.parser.parse

        async def parse(session, url):
            if url.endswith("/2"):
                await asyncio.Event().wait()
            return await original(session, url)

        self.parser.parse = parse
        result = json.loads(
            await self.fetcher.fetch(
                event(),
                url="https://tieba.baidu.com/p/1 https://tieba.baidu.com/p/2",
            )
        )
        self.assertEqual(result["status"], "partial")
        self.assertIn("超时", result["results"][1]["error"])

    async def test_concurrency_limit_cancel_and_shutdown(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def parse(session, url):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.parser.parse = parse
        self.cfg.llm_tool.max_concurrent_calls = 1
        first = asyncio.create_task(
            self.fetcher.fetch(event(), url="https://tieba.baidu.com/p/1")
        )
        await entered.wait()
        result = json.loads(
            await self.fetcher.fetch(
                event(), url="https://tieba.baidu.com/p/2"
            )
        )
        self.assertEqual(result["error"]["code"], "busy")
        await self.fetcher.aclose()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(self.fetcher._tasks), 0)

    async def test_unsafe_source_and_unsupported_are_not_fetched(self):
        self.parser.extract_links = lambda text: [text]
        for url in (
            "http://127.0.0.1/a",
            "http://10.0.0.1/a",
            "file:///root/data",
            "https://user:pass@example.com/a",
            "http://localhost/a",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    json.loads(await self.fetcher.fetch(event(), url=url))["error"][
                        "code"
                    ],
                    "unsupported_url",
                )

    async def test_shutdown_drains_both_persistent_record_operations(self):
        for operation_name in ("filter_links", "record_metadata_links"):
            with self.subTest(operation_name=operation_name):
                entered = asyncio.Event()
                release = threading.Event()
                finished = threading.Event()
                loop = asyncio.get_running_loop()
                records = ParseRecordManager(
                    record_file="", same_link_max_count=1, same_link_window_seconds=60
                )
                fetcher = TiebaContentFetcher(self.cfg, records)

                def blocking_operation(*args, **kwargs):
                    loop.call_soon_threadsafe(entered.set)
                    try:
                        release.wait(5)
                        return (args[0], []) if operation_name == "filter_links" else None
                    finally:
                        finished.set()

                setattr(records, operation_name, blocking_operation)
                first = asyncio.create_task(
                    fetcher.fetch(event(), url="https://tieba.baidu.com/p/1")
                )
                await asyncio.wait_for(entered.wait(), timeout=2)
                first.cancel()
                closing = asyncio.create_task(fetcher.aclose())
                try:
                    await asyncio.sleep(0.02)
                    self.assertFalse(closing.done())
                    self.assertFalse(finished.is_set())
                finally:
                    release.set()
                await closing
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertTrue(finished.is_set())
                self.assertEqual(fetcher._tasks, set())

    async def test_cancelled_record_failure_does_not_log_raw_worker_exception(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        exceptions = []
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: exceptions.append(context))

        def fail():
            loop.call_soon_threadsafe(entered.set)
            release.wait(5)
            raise ValueError("credential-bearing exception")

        task = asyncio.create_task(_run_record_operation(fail))
        try:
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            self.assertEqual(exceptions, [])
        finally:
            release.set()
            loop.set_exception_handler(original_handler)
