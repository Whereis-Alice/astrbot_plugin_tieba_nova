"""惰性下载：把 URL 列表下载到缓存目录，返回第一个成功文件的路径。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logger import logger
from ..parser.platform.tieba import TiebaParser
from .resources import RENDER_RESOURCES_KEY, RenderResources

MAX_CARD_IMAGE_BYTES = 32 * 1024 * 1024


async def _download_one(
    url: str, cache_dir: Path, metadata: Dict[str, Any]
) -> Optional[Path]:
    import uuid
    from io import BytesIO

    import aiohttp
    from PIL import Image

    url = TiebaParser._media_url(url)
    if not url:
        return None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": "https://tieba.baidu.com/",
    }
    image_headers = metadata.get("image_headers") or {}
    if isinstance(image_headers, dict):
        # Account cookies belong to the API, never to the image CDN.
        allowed = {"user-agent": "User-Agent", "accept": "Accept"}
        headers.update(
            {
                allowed[k.lower()]: str(v)
                for k, v in image_headers.items()
                if isinstance(k, str) and k.lower() in allowed
            }
        )

    proxy = None
    if metadata.get("use_image_proxy") and metadata.get("proxy_url"):
        proxy = metadata["proxy_url"]

    try:
        timeout = aiohttp.ClientTimeout(total=20, connect=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers=headers, proxy=proxy, allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"rika 下载失败: HTTP {resp.status}: {url}")
                    return None
                if (
                    resp.content_length is not None
                    and resp.content_length > MAX_CARD_IMAGE_BYTES
                ):
                    return None
                chunks = []
                size = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > MAX_CARD_IMAGE_BYTES:
                        return None
                    chunks.append(chunk)
                raw = b"".join(chunks)
    except Exception as e:
        logger.debug(f"贴吧卡片图片下载失败（{type(e).__name__}）")
        return None

    if not raw:
        return None
    try:
        with Image.open(BytesIO(raw)) as image:
            image.verify()
    except Exception as e:
        logger.debug(f"rika 响应非图片: {url}, {e}")
        return None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        out = cache_dir / f"tieba_rika_{uuid.uuid4().hex}.jpg"
        with out.open("xb") as file:
            resources = metadata.get(RENDER_RESOURCES_KEY)
            if isinstance(resources, RenderResources):
                resources.paths.add(out)
            file.write(raw)
        return out
    except Exception as e:
        logger.debug(f"rika 保存失败: {e}")
        return None


async def download_first_to_cache(
    urls: List[str],
    *,
    cache_dir: Path,
    metadata: Dict[str, Any],
    kind: str = "image",
) -> Path:
    """依次尝试下载，返回第一个成功的本地路径；全部失败抛异常。"""
    for url in urls:
        path = await _download_one(url, cache_dir, metadata)
        if path is not None:
            logger.debug(f"rika {kind} 下载成功: {url}")
            return path
    raise RuntimeError(f"rika {kind} 下载失败: {urls[:2]}")
