"""独立贴吧插件的默认配置、平台边界和通用功能。"""

import asyncio
import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tieba_core.config_manager import (
    CARD_MODE_ONLY,
    CARD_SKIN_AUTO,
    CARD_SKINS,
    OVERSIZE_DELIVERY_ORIGINAL_FILE,
    PARSER_OUTPUT_KEYS,
    TRANSCODE_MODE_DISABLED,
    ConfigManager,
    DownloadConfig,
    HotCommentConfig,
    ProxyConfig,
)
from tieba_core.constants import Config
from tieba_core.downloader.manager import DownloadManager
from tieba_core.translation import MetadataTranslator, build_card_metadata_list

TASK_DIR = Path(tempfile.gettempdir()) / "tieba-nova-config-tests"


def make_config(raw=None, **cache_options):
    with (
        patch("tieba_core.config_manager.check_cache_dir_available", return_value=True),
        patch(
            "tieba_core.config_manager._is_docker_environment",
            return_value=cache_options.get("docker", False),
        ),
        patch(
            "tieba_core.config_manager._get_astrbot_plugin_cache_dir",
            return_value=str(TASK_DIR / "cache"),
        ),
    ):
        return ConfigManager({} if raw is None else raw)


def schema_defaults(schema):
    return {
        key: schema_defaults(entry["items"])
        if entry["type"] == "object"
        else entry.get("default")
        for key, entry in schema.items()
    }


class StandaloneConfigTests(unittest.TestCase):
    def test_default_is_tieba_cards_with_three_floor_replies(self):
        config = make_config()
        self.assertEqual(PARSER_OUTPUT_KEYS, ("tieba",))
        (parser,) = config.create_parsers()
        self.assertEqual(parser.name, "tieba")
        self.assertTrue(parser.include_media)
        self.assertEqual(parser.hot_comment_count, 3)
        self.assertEqual(parser.cookie, "")
        self.assertIsNone(parser.proxy_url)
        self.assertTrue(config.message.card_render.enabled)
        self.assertTrue(config.message.card_render.include_hot_comments)
        self.assertEqual(config.message.card_render.mode, CARD_MODE_ONLY)
        self.assertFalse(config.message.hot_comments.show_in_text)
        self.assertFalse(config.translation.enabled)
        self.assertEqual(config.trigger.keywords, ["贴吧解析", "解析贴吧"])
        self.assertEqual(config.admin.clean_cache_keyword, "清理贴吧媒体")

    def test_default_uploads_original_and_disables_compression(self):
        config = make_config()
        self.assertEqual(
            config.download.oversize_delivery, OVERSIZE_DELIVERY_ORIGINAL_FILE
        )
        self.assertTrue(config.download.group_file_enabled)
        self.assertFalse(config.download.transcode_oversize_video)
        self.assertEqual(config.download.transcode_mode, TRANSCODE_MODE_DISABLED)
        self.assertEqual(
            DownloadConfig().oversize_delivery, config.download.oversize_delivery
        )
        self.assertEqual(
            DownloadConfig().transcode_mode, config.download.transcode_mode
        )

    def test_each_output_mode_injects_media_and_reply_options(self):
        for mode, expected in {
            "关闭": None,
            "全部发送": (True, 3),
            "仅文本": (False, 3),
            "仅富媒体": (True, 0),
        }.items():
            with self.subTest(mode=mode):
                parsers = make_config({"parsers": {"tieba": mode}}).create_parsers()
                if expected is None:
                    self.assertEqual(parsers, [])
                else:
                    self.assertEqual(
                        (parsers[0].include_media, parsers[0].hot_comment_count),
                        expected,
                    )

    def test_reply_switch_and_zero_are_effective(self):
        for options in ({"count": 0}, {"count": 3, "tieba": False}):
            with self.subTest(options=options):
                (parser,) = make_config(
                    {"message": {"hot_comments": options}}
                ).create_parsers()
                self.assertEqual(parser.hot_comment_count, 0)
        self.assertEqual(
            make_config(
                {"message": {"hot_comments": {"count": 999}}}
            ).message.hot_comments.count,
            50,
        )

    def test_only_tieba_credentials_and_proxy_are_read(self):
        config = make_config(
            {
                "tieba": {"cookie": "BDUSS=local-fixture"},
                "proxy": {
                    "address": "http://127.0.0.1:7890",
                    "tieba": True,
                    "steam": True,
                },
                "steam": {"cookie": "ignored"},
                "bilibili_enhanced": {"use_cookie": True},
                "parsers": {"tieba": "全部发送", "steam": "全部发送"},
            }
        )
        (parser,) = config.create_parsers()
        self.assertEqual(parser.cookie, "BDUSS=local-fixture")
        self.assertEqual(parser.proxy_url, "http://127.0.0.1:7890")
        self.assertNotIn("Cookie", parser.headers)
        self.assertFalse(hasattr(config, "steam"))
        self.assertFalse(hasattr(config, "bilibili"))
        self.assertFalse(hasattr(config, "bilibili_parser"))
        self.assertEqual(
            {field.name for field in fields(ProxyConfig)},
            {"address", "tieba_use_proxy"},
        )
        self.assertEqual(
            {field.name for field in fields(HotCommentConfig)},
            {"count", "tieba", "show_in_text"},
        )
        self.assertEqual(
            config.parser_output.output_for_metadata({"platform": "steam"}),
            (False, False),
        )

    def test_invalid_root_parser_and_permission_config_fail_closed(self):
        config = make_config([])
        self.assertEqual(config.create_parsers(), [])
        self.assertFalse(config.trigger.should_parse("贴吧解析"))
        self.assertFalse(config.permission.check(True, "42", ""))
        for parsers in (None, [], {"tieba": "unknown"}):
            with self.subTest(parsers=parsers):
                self.assertEqual(make_config({"parsers": parsers}).create_parsers(), [])
        config = make_config({"permissions": {"whitelist": {"enable": "invalid"}}})
        self.assertFalse(config.permission.check(True, "42", ""))

    def test_permissions_keywords_rate_limits_and_zip_remain_available(self):
        config = make_config(
            {
                "trigger": {
                    "auto_parse": False,
                    "keywords": ["", "贴吧解析", "贴吧解析", None],
                },
                "permissions": {
                    "admin_id": "1",
                    "whitelist": {"enable": True, "group": ["100"]},
                },
                "parse_rate_limit": {
                    "same_user": {"max_count": 2, "window_seconds": 60}
                },
                "message": {
                    "archive": {"command": "贴吧归档", "max_total_size_mb": 512}
                },
            }
        )
        self.assertTrue(
            config.trigger.should_parse("贴吧解析 https://tieba.baidu.com/p/1")
        )
        self.assertFalse(config.trigger.should_parse("普通消息"))
        self.assertTrue(config.permission.check(False, "2", "100"))
        self.assertTrue(config.permission.check(True, "1", ""))
        self.assertFalse(config.permission.check(True, "2", ""))
        self.assertTrue(config.parse_rate_limit.enabled)
        self.assertEqual(config.parse_rate_limit.retention_seconds, 60)
        self.assertEqual(config.message.archive.command, "贴吧归档")
        self.assertEqual(config.message.archive.max_total_size_mb, 512)
        conflict = make_config({"message": {"archive": {"command": "清理贴吧媒体"}}})
        self.assertEqual(conflict.message.archive.command, "")

    def test_custom_cache_and_runtime_use_independent_locations(self):
        custom = str(TASK_DIR / "configured-cache")
        for docker in (True, False):
            with self.subTest(docker=docker):
                config = make_config({"download": {"cache_dir": custom}}, docker=docker)
                self.assertEqual(config.download.cache_dir, custom)
                self.assertEqual(
                    config.message.card_render.save_dir,
                    Config.build_runtime_dir(custom, "cards"),
                )
                self.assertEqual(
                    config.parse_rate_limit.record_file,
                    str(
                        Path(Config.build_runtime_dir(custom, "parse_records"))
                        / "records.json"
                    ),
                )
        self.assertEqual(Config.PLUGIN_NAME, "astrbot_plugin_tieba_nova")
        self.assertEqual(Config.RUNTIME_DIR_NAME, "tieba_nova_runtime")

    def test_optional_transcode_settings_and_invalid_bounds(self):
        config = make_config(
            {
                "download": {
                    "transcode_oversize_video": True,
                    "transcode_mode": "始终压缩",
                    "transcode_max_height": float("inf"),
                    "transcode_max_attempts": 999,
                }
            }
        )
        self.assertTrue(config.download.transcode_oversize_video)
        self.assertEqual(config.download.transcode_mode, "always")
        self.assertEqual(config.download.transcode_max_height, 0)
        self.assertEqual(config.download.transcode_max_attempts, 3)
        self.assertEqual(
            make_config(
                {"download": {"oversize_delivery": "bad"}}
            ).download.oversize_delivery,
            OVERSIZE_DELIVERY_ORIGINAL_FILE,
        )

    def test_schema_exposes_only_tieba_and_matches_defaults(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            set(schema),
            {
                "parsers",
                "trigger",
                "parse_rate_limit",
                "message",
                "translation",
                "download",
                "permissions",
                "proxy",
                "admin",
                "tieba",
                "media_relay",
            },
        )
        self.assertEqual(set(schema["parsers"]["items"]), {"tieba"})
        self.assertEqual(set(schema["proxy"]["items"]), {"address", "tieba"})
        self.assertEqual(
            set(schema["message"]["items"]["hot_comments"]["items"]),
            {"count", "tieba", "show_in_text"},
        )
        self.assertEqual(
            set(schema["message"]["items"]["media_display"]["items"]),
            {"video_cover_only"},
        )
        self.assertEqual(len(CARD_SKINS) - (CARD_SKIN_AUTO in CARD_SKINS), 7)
        for label in schema["message"]["items"]["card_render"]["items"]["skin"][
            "options"
        ]:
            with self.subTest(label=label):
                self.assertIn(ConfigManager._parse_card_skin(label), CARD_SKINS)
        from_schema = make_config(schema_defaults(schema))
        self.assertEqual(
            from_schema.download.oversize_delivery,
            make_config().download.oversize_delivery,
        )
        self.assertEqual(
            from_schema.message.card_render, make_config().message.card_render
        )
        self.assertEqual(
            from_schema.message.hot_comments, make_config().message.hot_comments
        )
        self.assertEqual(from_schema.trigger.keywords, make_config().trigger.keywords)
        self.assertFalse(from_schema.translation.enabled)
        self.assertEqual(from_schema.relay, make_config().relay)
        exposed = {
            "max_concurrent_downloads" if key == "max_concurrent" else key
            for key in schema["download"]["items"]
        }
        expected = {field.name for field in fields(DownloadConfig)} - {
            "cache_dir_available"
        }
        self.assertEqual(exposed, expected)

    def test_general_legacy_packing_and_translation_values_are_migrated(self):
        config = make_config(
            {
                "message": {"packing": {"mode": "全部打包", "zip_command": "贴吧归档"}},
                "translation": {"output_mode": "仅作用于卡片"},
            }
        )
        self.assertEqual(config.message.aggregation.mode, "全部聚合")
        self.assertEqual(config.message.archive.command, "贴吧归档")
        self.assertEqual(config.translation.apply_scope, "仅卡片使用译文")

    def test_optional_translation_provider_and_media_relay_are_retained(self):
        config = make_config(
            {
                "translation": {
                    "enable": True,
                    "llm": {
                        "provider_source": "插件自定义提供商",
                        "custom_provider": {
                            "provider": "自定义 OpenAI 兼容",
                            "base_url": "https://llm.example/v1/",
                            "api_key": "local-fixture",
                            "model": "fixture-model",
                        },
                    },
                },
                "media_relay": {
                    "enable": True,
                    "callback_url": "https://relay.example/",
                    "ttl": 90,
                },
            }
        )
        self.assertTrue(config.translation.enabled)
        self.assertEqual(config.translation.llm_provider_source, "custom")
        self.assertEqual(config.translation.base_url, "https://llm.example/v1")
        self.assertEqual(config.translation.api_key, "local-fixture")
        self.assertEqual(config.translation.model, "fixture-model")
        self.assertTrue(config.relay.enabled)
        self.assertEqual(config.relay.callback_api_base, "https://relay.example")
        self.assertEqual(config.relay.file_token_ttl, 90)


class StandaloneBehaviorTests(unittest.TestCase):
    def test_default_file_policy_keeps_original_bytes_even_with_optional_compression(
        self,
    ):
        config = make_config()
        manager = DownloadManager(
            cache_dir=str(TASK_DIR),
            cache_dir_available=True,
            oversize_delivery=config.download.oversize_delivery,
            max_video_size_mb=config.download.max_video_size_mb,
            send_video_max_mb=config.download.send_video_max_mb,
            transcode_oversize_video=True,
            transcode_mode="always",
        )
        original = b"tieba-original-fixture" * 30
        TASK_DIR.mkdir(parents=True, exist_ok=True)
        path = TASK_DIR / "original-fixture.mp4"
        compressor = AsyncMock(side_effect=AssertionError("原文件策略不得转码"))
        manager._fit_video_to_send_limit = compressor

        async def download(*, session, media_items, cache_dir):
            path.write_bytes(original)
            return [
                {**item, "success": True, "file_path": str(path), "size_mb": 80}
                for item in media_items
            ]

        manager._download_local_items = download
        with patch(
            "tieba_core.downloader.manager.get_video_size",
            AsyncMock(return_value=(80, 200)),
        ):
            metadata = asyncio.run(
                manager.process_metadata(
                    session=None,
                    metadata={
                        "url": "https://tieba.baidu.com/p/1",
                        "video_urls": [["https://tb-video.bdstatic.com/fixture.mp4"]],
                    },
                    group_file_available=True,
                )
            )
        self.assertEqual(metadata["video_modes"], ["group_file"])
        self.assertEqual(path.read_bytes(), original)
        compressor.assert_not_awaited()

    def test_title_body_and_replies_use_one_translation_chain(self):
        source = [
            {
                "title": "An English title",
                "desc": "An English body",
                "hot_comments": [
                    {
                        "username": "贴吧用户",
                        "message": "An English reply",
                        "position_text": "第 2 楼",
                        "likes": None,
                    }
                ],
            }
        ]
        translator = MetadataTranslator.__new__(MetadataTranslator)
        translator.config = SimpleNamespace(
            content_scope="正文和标题",
            max_text_chars_per_request=4000,
            target_language="简体中文",
        )
        groups = translator._collect_item_groups(source, "简体中文")
        self.assertEqual(
            [item["id"] for group in groups for item in group],
            ["0:title", "0:desc", "0:comment:0"],
        )
        translator._apply_translations(
            source,
            {"0:title": "翻译标题", "0:desc": "翻译正文", "0:comment:0": "翻译回复"},
        )
        card = build_card_metadata_list(source, source)[0]
        self.assertEqual(card["title"], "翻译标题")
        self.assertEqual(card["desc"], "翻译正文")
        self.assertEqual(card["hot_comments"][0]["message"], "翻译回复")
        self.assertEqual(card["hot_comments"][0]["position_text"], "第 2 楼")
        self.assertIsNone(card["hot_comments"][0]["likes"])


if __name__ == "__main__":
    unittest.main()
