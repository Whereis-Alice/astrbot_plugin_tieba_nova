"""独立贴吧内容工具的配置边界、展示独立性与归档隔离。"""

import asyncio
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tieba_core.config_manager import ConfigManager, LLMToolConfig


def make_config(raw=None):
    with (
        patch("tieba_core.config_manager.check_cache_dir_available", return_value=True),
        patch("tieba_core.config_manager._is_docker_environment", return_value=False),
        patch(
            "tieba_core.config_manager._get_astrbot_plugin_cache_dir",
            return_value=str(Path(tempfile.gettempdir()) / "llm-tool-config-cache"),
        ),
    ):
        return ConfigManager({} if raw is None else raw)


class LLMToolConfigTests(unittest.TestCase):
    def test_defaults_and_schema_match_without_comment_ceiling(self):
        defaults = asdict(LLMToolConfig())
        self.assertEqual(asdict(make_config().llm_tool), defaults)
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )["llm_tool"]["items"]
        self.assertEqual({key: value["default"] for key, value in schema.items()}, defaults)
        self.assertNotIn("max_comments", defaults)
        self.assertNotIn("max_comments", schema)

    def test_switch_accepts_explicit_booleans_and_fails_closed(self):
        for value, expected in ((True, True), ("true", True), (1, True),
                                 (False, False), ("false", False), (0, False),
                                 (None, False), ([], False), ({}, False),
                                 ("unknown", False), (2, False), (1.0, False)):
            with self.subTest(value=value):
                self.assertEqual(make_config({"llm_tool": {"enabled": value}}).llm_tool.enabled, expected)
        for malformed in (None, [], "enabled", True):
            with self.subTest(section=malformed):
                self.assertFalse(make_config({"llm_tool": malformed}).llm_tool.enabled)
        self.assertFalse(make_config([]).llm_tool.enabled)

    def test_limits_clamp_and_invalid_types_fall_back(self):
        bounds = {
            "max_links": (3, 1, 5),
            "max_output_chars": (24000, 4000, 60000),
            "timeout_seconds": (45, 5, 120),
            "max_concurrent_calls": (2, 1, 4),
        }
        for field, (default, minimum, maximum) in bounds.items():
            for value, expected in ((-999999, minimum), (999999, maximum),
                                    (str(maximum), maximum), (True, default),
                                    (None, default), ([], default), ({}, default),
                                    (1.5, default), (float("inf"), default),
                                    (float("nan"), default), ("1.5", default)):
                with self.subTest(field=field, value=value):
                    self.assertEqual(getattr(make_config({"llm_tool": {field: value}}).llm_tool, field), expected)


class LLMParserFactoryTests(unittest.TestCase):
    def test_tool_ignores_chat_and_card_display_modes(self):
        for mode in ("全部发送", "仅文本", "仅富媒体"):
            for card_mode in ("仅卡片", "卡片+文本同条发送", "卡片+文本分开发"):
                with self.subTest(mode=mode, card_mode=card_mode):
                    config = make_config({
                        "parsers": {"tieba": mode},
                        "message": {
                            "hot_comments": {"count": 0},
                            "card_render": {"mode": card_mode, "include_hot_comments": False},
                            "text_metadata": {"show_title": False, "show_description": False},
                            "media_display": {"video_cover_only": True},
                        },
                    })
                    (chat,) = config.create_parsers()
                    (tool,) = config.create_parsers(for_llm=True)
                    self.assertTrue(tool.include_media)
                    self.assertEqual(tool.hot_comment_count, 5)
                    self.assertEqual(chat.include_media, mode != "仅文本")
                    self.assertEqual(chat.hot_comment_count, 0)
                    self.assertIsNot(chat, tool)

    def test_disabled_platform_and_tool_do_not_allocate_parsers(self):
        for mode in ("关闭", "unknown", None):
            with self.subTest(mode=mode):
                cfg = make_config({"parsers": {"tieba": mode}})
                with patch("tieba_core.config_manager.TiebaParser") as parser:
                    self.assertEqual(cfg.create_parsers(for_llm=True), [])
                parser.assert_not_called()
        cfg = make_config({"llm_tool": {"enabled": False}})
        self.assertEqual(cfg.create_parsers(for_llm=True), [])
        self.assertEqual([parser.name for parser in cfg.create_parsers()], ["tieba"])
        self.assertEqual([parser.name for parser in cfg.create_parsers(for_archive=True)], ["tieba"])

    def test_comment_parameter_default_range_and_invalid_types(self):
        cfg = make_config()
        for requested, expected in ((None, 5), (0, 0), (2, 2), (10, 10), (20, 20),
                                    (-1, 0), (999, 20), ("3", 3), (True, 5),
                                    ([], 5), ({}, 5), (1.5, 5), (float("inf"), 5),
                                    (float("nan"), 5), ("invalid", 5)):
            with self.subTest(requested=requested):
                (parser,) = cfg.create_parsers(for_llm=True, hot_comment_count=requested)
                self.assertEqual(parser.hot_comment_count, expected)

    def test_legacy_comment_ceiling_is_ignored(self):
        for legacy_limit in (0, 4, 5):
            cfg = make_config({"llm_tool": {"max_comments": legacy_limit}})
            self.assertFalse(hasattr(cfg.llm_tool, "max_comments"))
            for requested, expected in ((None, 5), (10, 10), (20, 20)):
                with self.subTest(legacy_limit=legacy_limit, requested=requested):
                    (parser,) = cfg.create_parsers(for_llm=True, hot_comment_count=requested)
                    self.assertEqual(parser.hot_comment_count, expected)

    def test_reply_switch_is_respected_and_invalid_values_fail_closed(self):
        for value in (False, "false", None, "unknown", [], 2):
            with self.subTest(value=value):
                cfg = make_config({"message": {"hot_comments": {"count": 3, "tieba": value}}})
                self.assertEqual(cfg.create_parsers()[0].hot_comment_count, 0)
                self.assertEqual(cfg.create_parsers(for_llm=True, hot_comment_count=20)[0].hot_comment_count, 0)

    def test_cookie_proxy_and_media_parameter_are_injected(self):
        cfg = make_config({
            "parsers": {"tieba": "仅文本"},
            "tieba": {"cookie": "BDUSS=fixture"},
            "proxy": {"address": "http://127.0.0.1:7890", "tieba": True},
        })
        (tool,) = cfg.create_parsers(for_llm=True)
        self.assertEqual(tool.cookie, "BDUSS=fixture")
        self.assertEqual(tool.proxy_url, "http://127.0.0.1:7890")
        self.assertTrue(tool.include_media)
        self.assertFalse(cfg.create_parsers(for_llm=True, include_media=False)[0].include_media)
        self.assertFalse(cfg.create_parsers()[0].include_media)

    def test_chat_archive_and_tool_instances_keep_independent_options(self):
        for mode in ("仅文本", "仅富媒体"):
            with self.subTest(mode=mode):
                cfg = make_config({"parsers": {"tieba": mode}, "message": {"hot_comments": {"count": 4}}})
                (chat,) = cfg.create_parsers()
                (archive,) = cfg.create_parsers(for_archive=True, include_media=False, hot_comment_count=20)
                (tool,) = cfg.create_parsers(for_llm=True, hot_comment_count=10)
                (second_tool,) = cfg.create_parsers(for_llm=True, hot_comment_count=20)
                self.assertEqual((chat.include_media, chat.hot_comment_count), (mode != "仅文本", 4 if mode == "仅文本" else 0))
                self.assertEqual((archive.include_media, archive.hot_comment_count), (True, 4))
                self.assertEqual((tool.include_media, tool.hot_comment_count), (True, 10))
                self.assertEqual((second_tool.include_media, second_tool.hot_comment_count), (True, 20))
                self.assertEqual(len({id(parser) for parser in (chat, archive, tool, second_tool)}), 4)
                self.assertEqual(len({id(parser.semaphore) for parser in (chat, archive, tool, second_tool)}), 4)
        cfg = make_config({"message": {"hot_comments": {"count": 0}}})
        self.assertEqual(cfg.create_parsers(for_archive=True)[0].hot_comment_count, 0)
        self.assertEqual(cfg.create_parsers(for_llm=True)[0].hot_comment_count, 5)

    def test_tool_parse_retains_body_media_and_replies_under_rich_only_chat(self):
        cfg = make_config({"parsers": {"tieba": "仅富媒体"}, "message": {"hot_comments": {"count": 0}}})
        (tool,) = cfg.create_parsers(for_llm=True)
        response = {
            "error_code": 0,
            "thread": {"id": 11180655858, "post_id": 10, "title": "工具帖子"},
            "post_list": [
                {"id": 10, "floor": 1, "author_id": 1, "content": [
                    {"type": 0, "text": "主楼正文"},
                    {"type": 3, "origin_src": "https://tiebapic.baidu.com/source.png"},
                    {"type": 5, "link": "https://tb-video.bdstatic.com/source.mp4"},
                ]},
                {"id": 11, "floor": 2, "author_id": 2, "content": [{"type": 0, "text": "楼层回复"}]},
            ],
            "user_list": [{"id": 1, "name": "主楼作者"}, {"id": 2, "name": "回复作者"}],
        }
        with patch.object(tool, "_fetch_json", AsyncMock(return_value=response)):
            result = asyncio.run(tool.parse(None, "https://tieba.baidu.com/p/11180655858"))
        self.assertIn("主楼正文", result["desc"])
        self.assertEqual(result["author"], "主楼作者")
        self.assertTrue(result["image_urls"])
        self.assertTrue(result["video_urls"])
        self.assertEqual(result["hot_comments"][0]["message"], "楼层回复")


if __name__ == "__main__":
    unittest.main()
