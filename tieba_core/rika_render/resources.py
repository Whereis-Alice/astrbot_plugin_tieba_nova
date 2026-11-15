"""Track only one render's newly downloaded assets and private tasks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..logger import logger

RENDER_RESOURCES_KEY = "_card_render_resources"


async def wait_for_completion(
    task: asyncio.Future[Any],
) -> tuple[Any, asyncio.CancelledError | None]:
    """Observe real completion despite repeated cancellation of the caller."""
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            break
    try:
        result = task.result()
    except BaseException as exc:
        if cancellation is not None:
            raise cancellation from exc
        raise
    return result, cancellation


class RenderResources:
    def __init__(self) -> None:
        self.paths: set[Path] = set()
        self.tasks: set[asyncio.Task[Path]] = set()
        self.outputs: dict[Path, tuple[int, int]] = {}

    def record_output(self, path: Path) -> None:
        stat = path.stat()
        self.outputs[path] = (stat.st_dev, stat.st_ino)

    def cleanup_outputs(self) -> None:
        """A concurrent render may have replaced the same cache filename."""
        for path, identity in self.outputs.items():
            try:
                stat = path.stat()
                if (stat.st_dev, stat.st_ino) == identity:
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(f"清理未交付卡片输出失败: {path}, {exc}")

    async def aclose(self) -> None:
        """Drain eager downloads before deleting their newly created files."""
        cancellation = None
        try:
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            if self.tasks:
                _, cancellation = await wait_for_completion(
                    asyncio.gather(*self.tasks, return_exceptions=True)
                )
        finally:
            for path in self.paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(f"清理本次卡片素材失败: {path}, {exc}")
        if cancellation is not None:
            self.cleanup_outputs()
            raise cancellation
