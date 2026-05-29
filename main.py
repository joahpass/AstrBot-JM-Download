from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import threading
import time
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import message_components as Comp
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain
from PIL import Image, ImageDraw, ImageFont

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - compatibility fallback
    get_astrbot_data_path = None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "开启", "是"}
    return bool(value)


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[\s,，]+", value.strip())
    elif isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = [value]
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _looks_like_jm_id(value: str) -> bool:
    return bool(re.fullmatch(r"\d{3,}", (value or "").strip()))


class JMComicReaderPlugin(Star):
    """Self-contained AstrBot plugin for JMComicReaderProject core features."""

    RANK_CATEGORIES = [
        ("0", "全部"),
        ("doujin", "同人"),
        ("single", "单本"),
        ("short", "短篇"),
        ("another", "其他"),
        ("hanman", "韩漫"),
        ("meiman", "美漫"),
        ("doujin_cosplay", "Cosplay"),
        ("3D", "3D"),
        ("english_site", "英文"),
    ]
    RANK_PERIODS = [
        ("day", "日排行"),
        ("week", "周排行"),
        ("month", "月排行"),
    ]
    RANK_SESSION_TTL = 180

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self._raw_config = config or {}
        self._config = self._load_config()
        self._download_progress: dict[str, dict[str, Any]] = {}
        self._download_alias: dict[str, str] = {}
        self._rank_sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._services_ready = False
        self._service_error = ""
        self._stop_cleanup = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        self._init_storage()
        self._init_services()
        self._start_cleanup_worker()

    def _load_config(self) -> dict[str, Any]:
        raw_conf: dict[str, Any] = self._raw_config if isinstance(self._raw_config, dict) else {}
        if not raw_conf:
            for attr in ("config", "conf"):
                value = getattr(self, attr, None)
                if isinstance(value, dict):
                    raw_conf = value
                    break

        return {
            "max_search_results": max(1, _as_int(raw_conf.get("max_search_results"), 5)),
            "allow_download": _as_bool(raw_conf.get("allow_download"), False),
            "download_poll_seconds": max(0, _as_int(raw_conf.get("download_poll_seconds"), 10)),
            "download_dir_name": str(raw_conf.get("download_dir_name") or "ComicDownloads").strip() or "ComicDownloads",
            "user_whitelist": _as_str_list(raw_conf.get("user_whitelist")),
            "group_whitelist": _as_str_list(raw_conf.get("group_whitelist")),
            "render_text_as_image": _as_bool(raw_conf.get("render_text_as_image"), True),
            "render_cover_enabled": _as_bool(raw_conf.get("render_cover_enabled"), True),
            "auto_delete_enabled": _as_bool(raw_conf.get("auto_delete_enabled"), False),
            "auto_delete_after_hours": max(1, _as_int(raw_conf.get("auto_delete_after_hours"), 24)),
            "auto_delete_interval_minutes": max(1, _as_int(raw_conf.get("auto_delete_interval_minutes"), 30)),
        }

    def _init_storage(self) -> None:
        if get_astrbot_data_path:
            base = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_jmcomic_reader"
        else:
            base = Path(__file__).resolve().parent / "data"

        self.data_dir = base
        self.download_dir = base / self._safe_download_dir_name(self._config["download_dir_name"])
        self.temp_dir = base / "TempCache"
        self.render_dir = self.temp_dir / "rendered_messages"
        self.backend_dir = base / "backend"

        for path in (self.data_dir, self.download_dir, self.temp_dir, self.render_dir, self.backend_dir):
            path.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_downloads()

        os.environ["BASE_DIR"] = str(self.data_dir)
        os.environ["DOWNLOAD_DIR"] = str(self.download_dir)
        os.environ["TEMP_CACHE_DIR"] = str(self.temp_dir)

    def _safe_download_dir_name(self, value: str) -> str:
        path = Path(value).expanduser()
        if path.is_absolute():
            return str(path)
        safe_name = re.sub(r"[\\/:*?\"<>|]+", "_", value).strip(". ")
        return safe_name or "ComicDownloads"

    def _migrate_legacy_downloads(self) -> None:
        legacy_dir = self.data_dir / "DownloadedComics"
        if legacy_dir == self.download_dir or not legacy_dir.is_dir():
            return
        try:
            for child in legacy_dir.iterdir():
                target = self.download_dir / child.name
                if target.exists():
                    continue
                shutil.move(str(child), str(target))
                logger.info(f"JM migrated legacy download: {child} -> {target}")
        except Exception:
            logger.exception("JM legacy download migration failed")

    def _init_services(self) -> None:
        try:
            from models.database import init_database
            from services.comic_manager import ComicManager
            from services.download_manager import DownloadManager
            from services.jm_crawler import JMCrawler

            init_database()
            self.jm_crawler = JMCrawler()
            self.comic_manager = ComicManager()
            self.download_manager = DownloadManager()
            self._services_ready = True
        except Exception as e:
            logger.exception("JMComicReader internal services init failed")
            self._service_error = str(e)
            self._services_ready = False

    def _start_cleanup_worker(self) -> None:
        if not self._config["auto_delete_enabled"]:
            return
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_worker,
            name="jmcomic-auto-delete",
            daemon=True,
        )
        self._cleanup_thread.start()

    def _cleanup_worker(self) -> None:
        # Run once shortly after startup, then at the configured interval.
        interval_seconds = self._config["auto_delete_interval_minutes"] * 60
        while not self._stop_cleanup.wait(60):
            try:
                self._cleanup_expired_downloads()
            except Exception:
                logger.exception("JM auto delete failed")
            if self._stop_cleanup.wait(interval_seconds):
                break

    def _cleanup_expired_downloads(self) -> int:
        if not self._services_ready:
            return 0

        cutoff = datetime.now() - timedelta(hours=self._config["auto_delete_after_hours"])
        deleted = 0
        for comic_dir in sorted(self.download_dir.iterdir()):
            if not comic_dir.is_dir():
                continue
            if comic_dir.stat().st_mtime > cutoff.timestamp():
                continue

            match = re.match(r"^(\d+)_", comic_dir.name)
            if not match:
                continue
            jm_id = int(match.group(1))
            try:
                if self.comic_manager.delete_comic(jm_id):
                    deleted += 1
                    with self._lock:
                        self._download_alias.pop(str(jm_id), None)
                    logger.info(f"JM auto deleted expired comic JM-{jm_id}: {comic_dir}")
            except Exception:
                logger.exception(f"JM auto delete failed for JM-{jm_id}")
        return deleted

    def _ensure_ready(self) -> str | None:
        if self._services_ready:
            return None
        return (
            "JM 内置服务初始化失败。\n"
            f"错误: {self._service_error or 'unknown'}\n"
            "请检查插件依赖是否安装成功，尤其是 jmcomic、Pillow、img2pdf、PyYAML。"
        )

    def _whitelist_error(self, event: AstrMessageEvent) -> str | None:
        user_whitelist = set(self._config["user_whitelist"])
        group_whitelist = set(self._config["group_whitelist"])
        if not user_whitelist and not group_whitelist:
            return None

        sender_id = str(event.get_sender_id() or "").strip()
        group_id = str(event.get_group_id() or "").strip()
        if sender_id and sender_id in user_whitelist:
            return None
        if group_id and group_id in group_whitelist:
            return None

        if group_id:
            return "你所在的群聊或当前用户不在 JM 插件白名单中。"
        return "当前用户不在 JM 插件个人白名单中。"

    def _help_text(self) -> str:
        return "\n".join(
            [
                "JM 常用命令:",
                "/jm 搜 <关键词> [页码] - 搜索",
                "/jm <JM号> - 查看详情",
                "/jm 下 <JM号> - 下载",
                "/jm 进 <JM号或download_id> - 查进度",
                "/jm 看 <JM号> - 本地阅读信息",
                "/jm 列 - 已下载列表",
                "/jm 榜 - 漫画排行榜，按数字选择分类和时间段",
                "/jm 随机 - 随机推荐漫画",
                "/jm 状态 - 插件状态",
                "/jm 帮助 - 显示帮助",
            ]
        )

    def _format_comic_line(self, comic: dict[str, Any], index: int | None = None) -> str:
        jm_id = comic.get("id") or comic.get("jm_id") or comic.get("album_id") or "-"
        title = comic.get("title") or comic.get("name") or "未命名"
        author = comic.get("author") or comic.get("artist") or ""
        pages = comic.get("pages") or comic.get("page_count") or ""
        prefix = f"{index}. " if index is not None else ""
        suffix = []
        if author:
            suffix.append(f"作者: {author}")
        if pages:
            suffix.append(f"页数: {pages}")
        detail = f" ({' / '.join(suffix)})" if suffix else ""
        return f"{prefix}JM-{jm_id} | {title}{detail}"

    def _font(self, size: int, bold: bool = False) -> ImageFont.ImageFont:
        candidates = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ]
        for font_path in candidates:
            try:
                if font_path and Path(font_path).exists():
                    return ImageFont.truetype(font_path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _wrap_text(self, text: str, width: int = 30) -> list[str]:
        wrapped: list[str] = []
        for line in str(text).splitlines() or [""]:
            if not line:
                wrapped.append("")
                continue
            current = ""
            units = 0
            for ch in line:
                ch_units = 2 if ord(ch) > 127 else 1
                if current and units + ch_units > width:
                    wrapped.append(current)
                    current = ch
                    units = ch_units
                else:
                    current += ch
                    units += ch_units
            wrapped.append(current)
        return wrapped

    def _render_text_image(self, text: str, cover_path: str | None = None, title: str = "JM Comic") -> Path:
        scale = 2
        cover_enabled = self._config["render_cover_enabled"] and cover_path and Path(cover_path).exists()
        card_w = 920
        margin = 36
        cover_w = 250 if cover_enabled else 0
        gap = 28 if cover_enabled else 0
        text_w = card_w - margin * 2 - cover_w - gap
        title_font = self._font(28 * scale, bold=True)
        body_font = self._font(21 * scale)
        meta_font = self._font(16 * scale)
        line_h = 34 * scale
        lines = self._wrap_text(text, max(18, text_w // 22))
        content_h = max(cover_w * 4 // 3 if cover_enabled else 0, 80 + len(lines) * line_h)
        card_h = min(max(260, content_h // scale + margin * 2), 1600)

        img = Image.new("RGB", (card_w * scale, card_h * scale), (245, 241, 232))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            [16 * scale, 16 * scale, (card_w - 16) * scale, (card_h - 16) * scale],
            radius=24 * scale,
            fill=(255, 252, 246),
            outline=(226, 215, 196),
            width=2 * scale,
        )

        x = margin * scale
        y = margin * scale
        if cover_enabled:
            try:
                with Image.open(cover_path) as cover:
                    cover = cover.convert("RGB")
                    cover.thumbnail((cover_w * scale, int((card_h - margin * 2) * scale)))
                    cover_box = Image.new("RGB", (cover_w * scale, int((card_h - margin * 2) * scale)), (232, 224, 210))
                    cx = (cover_box.width - cover.width) // 2
                    cy = (cover_box.height - cover.height) // 2
                    cover_box.paste(cover, (cx, cy))
                    img.paste(cover_box, (x, y))
                x += (cover_w + gap) * scale
            except Exception:
                logger.exception("JM render cover failed")

        draw.text((x, y), title, font=title_font, fill=(47, 42, 35))
        y += 48 * scale
        for line in lines[:36]:
            draw.text((x, y), line, font=body_font, fill=(65, 58, 48))
            y += line_h
            if y > (card_h - margin - 28) * scale:
                draw.text((x, y), "...", font=body_font, fill=(65, 58, 48))
                break
        draw.text((x, (card_h - margin + 4) * scale), "AstrBot JM Download", font=meta_font, fill=(145, 126, 96))

        img = img.resize((card_w, card_h), Image.Resampling.LANCZOS)
        output = self.render_dir / f"jm_render_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.jpg"
        img.save(output, "JPEG", quality=90)
        return output

    def _result_chain(self, text: str, cover_path: str | None = None, title: str = "JM Comic") -> list[Any]:
        if not self._config["render_text_as_image"]:
            return [Comp.Plain(text)]
        try:
            image_path = self._render_text_image(text, cover_path, title)
            return [Comp.Image.fromFileSystem(str(image_path))]
        except Exception:
            logger.exception("JM render message image failed")
            return [Comp.Plain(text)]

    def _cover_path_from_comic(self, comic: dict[str, Any] | None) -> str | None:
        if not isinstance(comic, dict):
            return None
        for key in ("cover_local", "cover_path"):
            value = comic.get(key)
            if value and Path(str(value)).exists():
                return str(value)
        return None

    async def _status_text(self) -> str:
        lines = [
            "JM 插件状态:",
            f"服务初始化: {'正常' if self._services_ready else '失败'}",
            f"数据目录: {self.data_dir}",
            f"下载目录: {self.download_dir}",
            f"允许下载: {self._config['allow_download']}",
            f"个人白名单: {len(self._config['user_whitelist'])} 个",
            f"群聊白名单: {len(self._config['group_whitelist'])} 个",
            f"自动删除: {self._config['auto_delete_enabled']}",
        ]
        if self._config["auto_delete_enabled"]:
            lines.append(f"保留时长: {self._config['auto_delete_after_hours']} 小时")
        if self._service_error:
            lines.append(f"初始化错误: {self._service_error}")
        return "\n".join(lines)

    async def _search_text(self, keyword: str, page: int = 1) -> str:
        if err := self._ensure_ready():
            return err
        keyword = (keyword or "").strip()
        if not keyword:
            return "用法: /jm 搜 <关键词> [页码]"

        page = max(1, int(page))
        try:
            results = await asyncio.to_thread(self.jm_crawler.search_by_keyword, keyword, "desc", page)
        except Exception as e:
            logger.exception("JM search failed")
            return f"搜索失败: {e}"

        if not results:
            return "没有搜索结果"

        limit = self._config["max_search_results"]
        lines = [f"搜索结果: {keyword} (第 {page} 页，显示前 {min(limit, len(results))} 条)"]
        for index, comic in enumerate(results[:limit], start=1):
            if isinstance(comic, dict):
                lines.append(self._format_comic_line(comic, index))
        lines.append("详情: /jm <JM号>")
        lines.append("下载: /jm 下 <JM号>")
        return "\n".join(lines)

    async def _info_payload(self, jm_id: int) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.jm_crawler.get_comic_info, jm_id)

    async def _info_result(self, jm_id: int) -> list[Any] | str:
        if err := self._ensure_ready():
            return err
        if jm_id <= 0:
            return "用法: /jm <JM号>"

        try:
            comic = await self._info_payload(jm_id)
        except Exception as e:
            logger.exception("JM info failed")
            return f"获取详情失败: {e}"

        if not comic:
            return "获取详情失败: 未找到对应漫画"

        tags = comic.get("tags") or []
        tags_text = ", ".join(str(tag) for tag in tags[:12]) if isinstance(tags, list) else str(tags)
        lines = [
            self._format_comic_line(comic),
            f"收藏: {comic.get('favorites', '-')}",
            f"标签: {tags_text or '-'}",
        ]
        if comic.get("description"):
            lines.append(f"简介: {comic.get('description')}")
        lines.append(f"阅读: /jm 看 {jm_id}")
        lines.append(f"下载: /jm 下 {jm_id}")
        return self._result_chain(
            "\n".join(lines),
            self._cover_path_from_comic(comic),
            f"JM-{jm_id} 详情",
        )

    def _update_progress(self, download_id: str, progress: int, status: str, message: str) -> None:
        with self._lock:
            self._download_progress[download_id] = {
                "progress": progress,
                "status": status,
                "message": message,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    def _download_worker(
        self,
        jm_id: int,
        comic_info: dict[str, Any],
        download_id: str,
        session: str | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        title_line: str = "",
    ) -> None:
        try:
            self.download_manager.download_comic(
                jm_id,
                comic_info,
                lambda p, s, m: self._update_progress(download_id, p, s, m),
            )
            with self._lock:
                current = self._download_progress.get(download_id, {})
                if current.get("status") != "error":
                    self._update_progress(download_id, 100, "completed", "下载完成")
            if session and loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._send_download_to_session(session, jm_id, title_line),
                    loop,
                )
        except Exception as e:
            logger.exception("JM download worker failed")
            self._update_progress(download_id, 0, "error", str(e))

    def _find_sendable_comic_file(self, jm_id: int) -> Path | None:
        comic_path = self.comic_manager.get_comic_path(jm_id)
        if not comic_path:
            return None

        path = Path(comic_path)
        if path.is_file():
            return path
        if path.is_dir():
            pdf_files = sorted(path.glob("*.pdf"))
            if pdf_files:
                return pdf_files[0]
        return None

    async def _send_download_to_session(self, session: str, jm_id: int, title_line: str = "") -> None:
        try:
            send_file = await asyncio.to_thread(self._find_sendable_comic_file, jm_id)
            if not send_file or not send_file.exists():
                await self.context.send_message(
                    session,
                    MessageChain(
                        [
                            Comp.Plain(
                                f"JM-{jm_id} 下载完成，但没有找到可上传的 PDF 文件。\n阅读: /jm 看 {jm_id}"
                            )
                        ]
                    ),
                )
                return

            header = title_line or f"JM-{jm_id}"
            await self.context.send_message(
                session,
                MessageChain([Comp.Plain(f"{header}\n下载完成，正在上传文件...")]),
            )
            await self.context.send_message(
                session,
                MessageChain([Comp.File(name=send_file.name, file=str(send_file.resolve()))]),
            )
            logger.info(f"JM-{jm_id} uploaded to session {session}: {send_file}")
        except Exception:
            logger.exception(f"JM-{jm_id} upload to session failed: {session}")

    async def _download_result(self, jm_id: int, event: AstrMessageEvent | None = None) -> list[Any] | str:
        if err := self._ensure_ready():
            return err
        if jm_id <= 0:
            return "用法: /jm 下 <JM号>"
        if not self._config["allow_download"]:
            return "下载命令已禁用。请在插件配置中设置 allow_download=true 后再使用。"

        try:
            already = await asyncio.to_thread(self.comic_manager.is_comic_downloaded, jm_id)
            comic = await self._info_payload(jm_id)
        except Exception as e:
            logger.exception("JM pre-download check failed")
            return f"启动下载失败: {e}"

        title_line = self._format_comic_line(comic) if comic else f"JM-{jm_id}"
        if already:
            if event:
                await self._send_download_to_session(event.unified_msg_origin, jm_id, title_line)
            return self._result_chain(
                f"{title_line}\n已经下载，已尝试上传到当前对话。\n阅读: /jm 看 {jm_id}",
                self._cover_path_from_comic(comic),
                f"JM-{jm_id} 已下载",
            )
        if not comic:
            return "启动下载失败: 未找到对应漫画"

        download_id = f"{jm_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        loop = asyncio.get_running_loop()
        session = event.unified_msg_origin if event else None
        with self._lock:
            self._download_alias[str(jm_id)] = download_id
            self._download_progress[download_id] = {
                "progress": 0,
                "status": "starting",
                "message": "下载任务已创建",
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        thread = threading.Thread(
            target=self._download_worker,
            args=(jm_id, comic, download_id, session, loop, title_line),
            daemon=True,
        )
        thread.start()

        lines = [
            title_line,
            "下载任务已启动",
            f"download_id: {download_id}",
            f"查进度: /jm 进 {download_id}",
        ]
        poll_seconds = self._config["download_poll_seconds"]
        if poll_seconds > 0:
            await asyncio.sleep(min(poll_seconds, 30))
            progress = self._progress_data(download_id)
            if progress:
                lines.append(
                    f"当前进度: {progress.get('progress', 0)}% | "
                    f"{progress.get('status', '-')} | {progress.get('message', '-')}"
                )
        return self._result_chain(
            "\n".join(lines),
            self._cover_path_from_comic(comic),
            f"JM-{jm_id} 下载任务",
        )

    def _progress_data(self, download_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._download_progress.get(download_id)
            return dict(data) if data else None

    async def _downloaded_status_text(self, jm_id_text: str, download_id: str | None = None) -> str | None:
        if not _looks_like_jm_id(jm_id_text) or not self._services_ready:
            return None
        try:
            downloaded = await asyncio.to_thread(self.comic_manager.is_comic_downloaded, int(jm_id_text))
        except Exception:
            downloaded = False
        if not downloaded:
            return None

        if download_id:
            with self._lock:
                self._download_progress[download_id] = {
                    "progress": 100,
                    "status": "completed",
                    "message": "下载完成",
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self._download_alias[jm_id_text] = download_id
            return "\n".join(
                [
                    f"download_id: {download_id}",
                    "进度: 100%",
                    "状态: completed",
                    "消息: 下载完成",
                    f"阅读: /jm 看 {jm_id_text}",
                ]
            )
        return f"JM-{jm_id_text} 已下载完成。\n阅读: /jm 看 {jm_id_text}"

    async def _progress_text(self, identifier: str) -> str:
        identifier = (identifier or "").strip()
        if not identifier:
            return "用法: /jm 进 <JM号或download_id>"

        with self._lock:
            download_id = self._download_alias.get(identifier, identifier)
        progress = self._progress_data(download_id)
        if not progress:
            jm_id_from_download_id = download_id.split("_", 1)[0]
            downloaded_text = await self._downloaded_status_text(jm_id_from_download_id, download_id)
            if downloaded_text:
                return downloaded_text
            if _looks_like_jm_id(identifier):
                downloaded_text = await self._downloaded_status_text(identifier)
                if downloaded_text:
                    return downloaded_text
                return "没有找到这个 JM 号对应的下载任务。请使用 /jm 下 <JM号> 后返回的 download_id 查询。"
            return f"查询进度失败: 下载任务不存在或已过期 ({download_id})"

        if progress.get("status") != "completed":
            jm_id_from_download_id = download_id.split("_", 1)[0]
            downloaded_text = await self._downloaded_status_text(jm_id_from_download_id, download_id)
            if downloaded_text:
                return downloaded_text

        return "\n".join(
            [
                f"download_id: {download_id}",
                f"进度: {progress.get('progress', 0)}%",
                f"状态: {progress.get('status', '-')}",
                f"消息: {progress.get('message', '-')}",
                f"更新时间: {progress.get('updated_at', '-')}",
            ]
        )

    async def _list_text(self) -> str:
        if err := self._ensure_ready():
            return err
        try:
            comics = await asyncio.to_thread(self.comic_manager.get_downloaded_comics)
        except Exception as e:
            logger.exception("JM list failed")
            return f"获取已下载列表失败: {e}"

        if not comics:
            return "当前没有已下载漫画"

        limit = self._config["max_search_results"]
        lines = [f"已下载漫画 (显示前 {min(limit, len(comics))} 条):"]
        for index, comic in enumerate(comics[:limit], start=1):
            if isinstance(comic, dict):
                lines.append(self._format_comic_line(comic, index))
        lines.append("阅读: /jm 看 <JM号>")
        return "\n".join(lines)

    async def _read_text(self, jm_id: int) -> str:
        if err := self._ensure_ready():
            return err
        if jm_id <= 0:
            return "用法: /jm 看 <JM号>"

        try:
            downloaded = await asyncio.to_thread(self.comic_manager.is_comic_downloaded, jm_id)
            if not downloaded:
                return "该漫画尚未下载。请先使用 /jm 下 <JM号>。"
            chapters = await asyncio.to_thread(self.comic_manager.get_comic_chapters, jm_id)
            comic_path = await asyncio.to_thread(self.comic_manager.get_comic_path, jm_id)
        except Exception as e:
            logger.exception("JM read failed")
            return f"获取阅读信息失败: {e}"

        return "\n".join(
            [
                f"JM-{jm_id}",
                f"章节数: {len(chapters or [])}",
                f"本地路径: {comic_path or '-'}",
                "如需网页阅读，请继续使用 JMComicReaderProject Web 版；本插件不启动 Flask 服务。",
            ]
        )

    async def _delete_text(self, jm_id: int) -> str:
        if err := self._ensure_ready():
            return err
        if jm_id <= 0:
            return "用法: /jm_delete <JM号>"

        try:
            ok = await asyncio.to_thread(self.comic_manager.delete_comic, jm_id)
        except Exception as e:
            logger.exception("JM delete failed")
            return f"删除失败: {e}"

        if not ok:
            return "删除失败: 本地记录或文件不存在"
        with self._lock:
            self._download_alias.pop(str(jm_id), None)
        return f"已删除本地漫画: JM-{jm_id}"

    def _rank_session_key(self, event: AstrMessageEvent) -> str:
        session = str(getattr(event, "unified_msg_origin", "") or "")
        sender = str(event.get_sender_id() or "")
        return f"{session}:{sender}"

    def _rank_category_menu(self) -> str:
        lines = ["JM 漫画排行榜", "请选择排行分类，直接回复数字："]
        for index, (_, label) in enumerate(self.RANK_CATEGORIES, start=1):
            lines.append(f"{index}. {label}")
        lines.append("180 秒内有效。")
        return "\n".join(lines)

    def _rank_period_menu(self, category_label: str) -> str:
        lines = [f"已选择分类：{category_label}", "请选择排行时间段，直接回复数字："]
        for index, (_, label) in enumerate(self.RANK_PERIODS, start=1):
            lines.append(f"{index}. {label}")
        lines.append("发送 /jm 榜 可重新选择分类。")
        return "\n".join(lines)

    def _start_rank_flow(self, event: AstrMessageEvent) -> str:
        with self._lock:
            self._rank_sessions[self._rank_session_key(event)] = {
                "step": "category",
                "created_at": time.time(),
            }
        return self._rank_category_menu()

    def _clear_expired_rank_sessions(self) -> None:
        now = time.time()
        with self._lock:
            expired = [
                key
                for key, value in self._rank_sessions.items()
                if now - float(value.get("created_at", 0)) > self.RANK_SESSION_TTL
            ]
            for key in expired:
                self._rank_sessions.pop(key, None)

    async def _ranking_text(self, period: str, category: str, category_label: str, period_label: str) -> str:
        if err := self._ensure_ready():
            return err
        try:
            results = await asyncio.to_thread(self.jm_crawler.get_ranking, period, category, 1)
        except Exception as e:
            logger.exception("JM ranking failed")
            return f"获取排行榜失败: {e}"

        if not results and period == "day":
            results = await asyncio.to_thread(self.jm_crawler.get_ranking, "week", category, 1)
            period_label = f"{period_label}(暂无结果，已切换周排行)"

        if not results:
            return "排行榜暂无结果，请稍后再试或换一个分类。"

        limit = self._config["max_search_results"]
        lines = [f"JM {period_label} / {category_label} (显示前 {min(limit, len(results))} 条)"]
        for index, comic in enumerate(results[:limit], start=1):
            lines.append(self._format_comic_line(comic, index))
        lines.append("详情: /jm <JM号>")
        lines.append("下载: /jm 下 <JM号>")
        return "\n".join(lines)

    async def _handle_rank_choice(self, event: AstrMessageEvent, choice_text: str) -> str | None:
        self._clear_expired_rank_sessions()
        key = self._rank_session_key(event)
        with self._lock:
            session = dict(self._rank_sessions.get(key) or {})

        if not session or not choice_text.isdigit():
            return None

        choice = int(choice_text)
        if session.get("step") == "category":
            if choice < 1 or choice > len(self.RANK_CATEGORIES):
                return self._rank_category_menu()
            category, label = self.RANK_CATEGORIES[choice - 1]
            with self._lock:
                self._rank_sessions[key] = {
                    "step": "period",
                    "category": category,
                    "category_label": label,
                    "created_at": time.time(),
                }
            return self._rank_period_menu(label)

        if session.get("step") == "period":
            if choice < 1 or choice > len(self.RANK_PERIODS):
                return self._rank_period_menu(str(session.get("category_label") or "全部"))
            period, period_label = self.RANK_PERIODS[choice - 1]
            category = str(session.get("category") or "0")
            category_label = str(session.get("category_label") or "全部")
            with self._lock:
                self._rank_sessions.pop(key, None)
            return await self._ranking_text(period, category, category_label, period_label)

        with self._lock:
            self._rank_sessions.pop(key, None)
        return None

    async def _random_text(self, limit: int = 5) -> str:
        if err := self._ensure_ready():
            return err
        try:
            results = await asyncio.to_thread(self.jm_crawler.get_random_recommendations, limit)
        except Exception as e:
            logger.exception("JM random recommendation failed")
            return f"获取随机推荐失败: {e}"

        if not results:
            return "暂时没有获取到随机推荐，请稍后再试。"

        lines = [f"JM 随机推荐 (显示 {len(results)} 条)"]
        for index, comic in enumerate(results, start=1):
            lines.append(self._format_comic_line(comic, index))
        lines.append("详情: /jm <JM号>")
        lines.append("下载: /jm 下 <JM号>")
        return "\n".join(lines)

    def _jm_args(self, event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        if text.startswith("/jm"):
            return text[3:].strip()
        if text.startswith("jm"):
            return text[2:].strip()
        return text

    def _event_result(self, event: AstrMessageEvent, result: list[Any] | str):
        if isinstance(result, list):
            return event.chain_result(result)
        return event.plain_result(str(result))

    @filter.command("jm")
    async def jm(self, event: AstrMessageEvent):
        """日常主命令：/jm"""
        args = self._jm_args(event)
        if not args or args in {"帮助", "help", "h", "?"}:
            yield event.plain_result(self._help_text())
            return
        if err := self._whitelist_error(event):
            yield event.plain_result(err)
            return

        parts = args.split()
        action = parts[0]
        rest = parts[1:]

        if _looks_like_jm_id(action):
            yield self._event_result(event, await self._info_result(int(action)))
            return

        if action in {"搜", "搜索", "s", "search"}:
            if not rest:
                yield event.plain_result("用法: /jm 搜 <关键词> [页码]")
                return
            page = 1
            if len(rest) >= 2 and rest[-1].isdigit():
                page = int(rest[-1])
                keyword = " ".join(rest[:-1])
            else:
                keyword = " ".join(rest)
            yield event.plain_result(await self._search_text(keyword, page))
            return

        if action in {"下", "下载", "d", "download"}:
            if not rest or not _looks_like_jm_id(rest[0]):
                yield event.plain_result("用法: /jm 下 <JM号>")
                return
            yield self._event_result(event, await self._download_result(int(rest[0]), event))
            return

        if action in {"进", "进度", "p", "progress"}:
            if not rest:
                yield event.plain_result("用法: /jm 进 <JM号或download_id>")
                return
            yield event.plain_result(await self._progress_text(rest[0]))
            return

        if action in {"看", "阅读", "r", "read"}:
            if not rest or not _looks_like_jm_id(rest[0]):
                yield event.plain_result("用法: /jm 看 <JM号>")
                return
            yield event.plain_result(await self._read_text(int(rest[0])))
            return

        if action in {"列", "列表", "l", "list"}:
            yield event.plain_result(await self._list_text())
            return

        if action in {"榜", "排行", "排行榜", "rank", "ranking"}:
            yield event.plain_result(self._start_rank_flow(event))
            return

        if action in {"随机", "随", "推荐", "random", "recommend"}:
            limit = 5
            if rest and rest[0].isdigit():
                limit = max(1, min(20, int(rest[0])))
            yield event.plain_result(await self._random_text(limit))
            return

        if action in {"状态", "status"}:
            yield event.plain_result(await self._status_text())
            return

        if action in {"删", "删除", "delete", "del"}:
            yield event.plain_result("删除请使用管理员命令: /jm_delete <JM号>")
            return

        yield event.plain_result(f"未知子命令: {action}\n\n{self._help_text()}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def jm_rank_choice(self, event: AstrMessageEvent):
        """Handle numeric replies during /jm 榜 selection."""
        text = (event.message_str or "").strip()
        result = await self._handle_rank_choice(event, text)
        if result is None:
            return
        if err := self._whitelist_error(event):
            yield event.plain_result(err)
            return
        if hasattr(event, "stop_event"):
            event.stop_event()
        yield event.plain_result(result)

    @filter.command("jm_help")
    async def jm_help(self, event: AstrMessageEvent):
        """显示 JMComicReader 插件帮助"""
        yield event.plain_result(self._help_text())

    @filter.command("jm_status")
    async def jm_status(self, event: AstrMessageEvent):
        """检查插件状态"""
        if err := self._whitelist_error(event):
            yield event.plain_result(err)
            return
        yield event.plain_result(await self._status_text())

    @filter.command("jm_search")
    async def jm_search(self, event: AstrMessageEvent, keyword: str = "", page: int = 1):
        """搜索漫画：/jm_search <关键词> [页码]"""
        if err := self._whitelist_error(event):
            yield event.plain_result(err)
            return
        yield event.plain_result(await self._search_text(keyword, page))

    @filter.command("jm_info")
    async def jm_info(self, event: AstrMessageEvent, jm_id: int = 0):
        """查看漫画详情：/jm_info <JM号>"""
        if err := self._whitelist_error(event):
            yield event.plain_result(err)
            return
        yield self._event_result(event, await self._info_result(jm_id))

    @filter.command("jm_download")
    async def jm_download(self, event: AstrMessageEvent, jm_id: int = 0):
        """启动下载：/jm_download <JM号>"""
        if err := self._whitelist_error(event):
            yield event.plain_result(err)
            return
        yield self._event_result(event, await self._download_result(jm_id, event))

    @filter.command("jm_progress")
    async def jm_progress(self, event: AstrMessageEvent, download_id: str = ""):
        """查询下载进度：/jm_progress <download_id>"""
        if err := self._whitelist_error(event):
            yield event.plain_result(err)
            return
        yield event.plain_result(await self._progress_text(download_id))

    @filter.command("jm_list")
    async def jm_list(self, event: AstrMessageEvent):
        """列出已下载漫画"""
        if err := self._whitelist_error(event):
            yield event.plain_result(err)
            return
        yield event.plain_result(await self._list_text())

    @filter.command("jm_rank")
    async def jm_rank(self, event: AstrMessageEvent):
        """启动 JM 排行榜数字选择流程"""
        if err := self._whitelist_error(event):
            yield event.plain_result(err)
            return
        yield event.plain_result(self._start_rank_flow(event))

    @filter.command("jm_random")
    async def jm_random(self, event: AstrMessageEvent, limit: int = 5):
        """随机推荐漫画"""
        if err := self._whitelist_error(event):
            yield event.plain_result(err)
            return
        yield event.plain_result(await self._random_text(limit))

    @filter.command("jm_read")
    async def jm_read(self, event: AstrMessageEvent, jm_id: int = 0):
        """获取本地阅读信息：/jm_read <JM号>"""
        if err := self._whitelist_error(event):
            yield event.plain_result(err)
            return
        yield event.plain_result(await self._read_text(jm_id))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("jm_delete")
    async def jm_delete(self, event: AstrMessageEvent, jm_id: int = 0):
        """删除本地漫画：/jm_delete <JM号>"""
        if err := self._whitelist_error(event):
            yield event.plain_result(err)
            return
        sender = event.get_sender_name()
        logger.info(f"jm_delete requested by {sender} for JM-{jm_id}")
        yield event.plain_result(await self._delete_text(jm_id))

    async def terminate(self):
        self._stop_cleanup.set()
        logger.info("JMComicReaderPlugin terminated")
