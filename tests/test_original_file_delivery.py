"""原文件策略绕过转码，但保留硬上限和当前会话文件投递。"""

import asyncio
import hashlib
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tieba_core.downloader.manager import DownloadManager


class OriginalFileDeliveryTests(unittest.TestCase):
    def _process(self, *, source_mb=80, hard_cap=1000, available=True, stream=True):
        with tempfile.TemporaryDirectory() as cache_dir:
            manager = DownloadManager(
                cache_dir=cache_dir, cache_dir_available=True,
                max_video_size_mb=hard_cap, send_video_max_mb=48,
                oversize_delivery="group_file_original",
                transcode_oversize_video=True, transcode_mode="always",
            )
            source = b"original video bytes" * 32
            source_hash = hashlib.sha256(source).hexdigest()
            compressor = AsyncMock(side_effect=AssertionError("不得压缩原文件"))
            manager._fit_video_to_send_limit = compressor
            plans = []

            async def download(*, session, media_items, cache_dir):
                plans.extend(media_items)
                results = []
                for item in media_items:
                    path = os.path.join(cache_dir, f"{item['position']}.mp4")
                    with open(path, "wb") as handle:
                        handle.write(source)
                    results.append({
                        **item, "success": True, "file_path": path,
                        "size_mb": source_mb,
                    })
                return results

            manager._download_local_items = download
            url = "m3u8:https://cdn.example/video.m3u8" if stream else "https://cdn.example/video.mp4"
            with patch("tieba_core.downloader.manager.get_video_size", AsyncMock(return_value=(source_mb, 200))):
                metadata = asyncio.run(manager.process_metadata(
                    session=None,
                    metadata={"url": "https://example.com/post", "video_urls": [[url]], "image_urls": []},
                    group_file_available=available,
                ))
            compressor.assert_not_awaited()
            path = metadata["file_paths"][0]
            if path:
                with open(path, "rb") as handle:
                    self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), source_hash)
            return metadata, plans

    def test_oversize_stream_is_original_file_even_when_always_compress_enabled(self):
        metadata, plans = self._process()
        self.assertEqual(metadata["video_modes"], ["group_file"])
        self.assertEqual(plans[0]["max_bytes"], 1000 * 1024 * 1024)
        self.assertEqual(metadata["video_transcode_notes"], [None])

    def test_ordinary_mp4_precheck_keeps_file_download_budget(self):
        metadata, plans = self._process(stream=False)
        self.assertEqual(metadata["video_modes"], ["group_file"])
        self.assertTrue(plans)

    def test_hard_download_limit_still_applies(self):
        metadata, _ = self._process(source_mb=1100)
        self.assertEqual(metadata["video_modes"], ["skip"])
        self.assertEqual(metadata["file_paths"], [None])

    def test_unsupported_adapter_falls_back_without_compressing(self):
        metadata, _ = self._process(available=False)
        self.assertEqual(metadata["video_modes"], ["skip"])
        self.assertEqual(metadata["file_paths"], [None])

    def test_under_limit_video_keeps_ordinary_delivery(self):
        metadata, _ = self._process(source_mb=12)
        self.assertEqual(metadata["video_modes"], ["local"])



if __name__ == "__main__":
    unittest.main()
