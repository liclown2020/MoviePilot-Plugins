"""豆瓣档案同步：把媒体服务器的播放进度同步到豆瓣「书影音档案」。

设计要点：
1. 不依赖 TMDB 识别，TMDB 不可用时仍能完成同步；
2. 豆瓣 ID 优先取媒体服务器已刮削的标识，取不到才用豆瓣搜索；
3. 网络请求全部放到宿主的延后任务里执行，不阻塞 webhook 事件线程；
4. 同步失败的条目进入待处理队列，由定时服务重试。
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.schemas import WebhookEventInfo
from app.schemas.types import EventType
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.plugin import _PluginBase
from app.sdk import scheduler as scheduler_sdk

try:  # 包内相对导入，兼容宿主重建实例的命名空间
    from .doubanclient import DoubanClient
    from .mediaserver import MediaServerReader
except Exception:  # pragma: no cover - 极端加载路径兜底
    from app.plugins.doubanarchive.doubanclient import DoubanClient
    from app.plugins.doubanarchive.mediaserver import MediaServerReader

# 播放开始事件
_PLAY_START = {"playback.start", "media.play", "PlaybackStart"}
# 标记已播放事件
_PLAYED = {"item.markplayed", "media.scrobble"}

# 存档键，避免字符串散落各处
_KEY_ARCHIVE = "archive"
# 待重试队列键
_KEY_PENDING = "pending"
# 仪表盘 key，宿主用它区分同一插件的多个仪表盘
_KEY_DASHBOARD = "archive"


class DoubanArchive(_PluginBase):
    """把剧集/电影的观看进度写入豆瓣书影音档案。"""

    # 插件基本信息
    plugin_name = "豆瓣档案同步"
    plugin_desc = "将在看、看完状态同步到豆瓣书影音档案，不依赖 TMDB 识别，失败自动重试。"
    plugin_icon = "Douban_A.png"
    plugin_version = "1.1.0"
    plugin_author = "liclown2020"
    author_url = "https://github.com/liclown2020"
    plugin_config_prefix = "doubanarchive_"
    plugin_order = 20
    auth_level = 1

    # 运行期状态
    _enabled = False
    _private = True
    _skip_first = True
    _users = ""
    _exclude = ""
    _cookie = ""
    _use_cookiecloud = True
    _cc_host = ""
    _cc_key = ""
    _cc_password = ""
    _pc_month = 3
    _pc_num = 50
    _mobile_month = 2
    _mobile_num = 15

    # 保护存档读写，避免并发任务互相覆盖
    _lock = threading.Lock()

    # ---------------- 生命周期 ----------------

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置并初始化运行期状态，可重复调用。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._private = bool(config.get("private", True))
        self._skip_first = bool(config.get("skip_first", True))
        self._users = str(config.get("users") or "")
        self._exclude = str(config.get("exclude") or "")
        self._cookie = str(config.get("cookie") or "")
        self._use_cookiecloud = bool(config.get("use_cookiecloud", True))
        self._cc_host = str(config.get("cookiecloud_host") or "")
        self._cc_key = str(config.get("cookiecloud_key") or "")
        self._cc_password = str(config.get("cookiecloud_password") or "")
        self._pc_month = self._to_int(config.get("pc_month"), 3, 2)
        self._pc_num = self._to_int(config.get("pc_num"), 50, 1)
        self._mobile_month = self._to_int(config.get("mobile_month"), 2, 2)
        self._mobile_num = self._to_int(config.get("mobile_num"), 15, 1)

        if config.get("onlyonce"):
            # 勾选立即运行一次：延后执行待处理队列重试
            self._schedule_once("retry_pending_once", self.retry_pending, "重试豆瓣同步失败队列", 3)
            config["onlyonce"] = False
            self.update_config(config)

        if self._enabled:
            logger.info("豆瓣档案同步插件已启用")
        else:
            logger.info("豆瓣档案同步插件未启用")

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册重试失败队列的远程命令。"""
        return [
            {
                "cmd": "/douban_retry",
                "event": EventType.PluginAction,
                "desc": "重试豆瓣同步失败队列",
                "category": "插件命令",
                "data": {"action": "douban_retry"},
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册每 30 分钟重试失败队列的周期任务。"""
        if not self._enabled:
            return []
        return [
            {
                "id": "DoubanArchive.Retry",
                "name": "豆瓣档案同步失败重试",
                "trigger": CronTrigger.from_crontab("*/30 * * * *"),
                "func": self.retry_pending,
                "kwargs": {},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册档案查询接口。"""
        return [
            {
                "path": "/archive",
                "endpoint": self.query_archive,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询豆瓣同步档案",
            }
        ]

    def stop_service(self) -> None:
        """停止时取消未执行的延后任务并关闭状态。"""
        self._remove_once("retry_pending_once")
        self._enabled = False

    # ---------------- 事件处理 ----------------

    @eventmanager.register(EventType.WebhookMessage)
    def handle_webhook(self, event: Event) -> None:
        """接收媒体服务器播放事件，登记需要同步的任务。"""
        info = getattr(event, "event_data", None)
        if not isinstance(info, WebhookEventInfo) or not self._enabled:
            return

        logger.info(
            f"收到事件：{info.event or '-'} | 标题：{info.item_name or '-'} | "
            f"类型：{info.item_type or '-'} | 用户：{info.user_name or '-'} | 服务器：{info.server_name or '-'}")
        if not self._is_target_user(info.user_name):
            logger.debug(f"用户 {info.user_name} 不在配置的媒体库用户名内，忽略")
            return
        if not self._is_sync_event(info):
            logger.debug(f"事件 {info.event} 不是播放或标记已观看事件，忽略")
            return
        if not self._is_allowed_path(info.item_path):
            logger.debug(f"路径 {info.item_path} 命中排除关键词，忽略")
            return

        title, media_type, season, episode = self._parse_media(info)
        if not title or not media_type:
            return
        if media_type == "TV" and self._skip_first and episode < 2:
            logger.info(f"{title} 第 1 集不同步到豆瓣档案")
            return

        payload = {
            "title": title,
            "media_type": media_type,
            "season": season,
            "episode": episode,
            "item_id": info.item_id or "",
            "server_name": info.server_name or "",
            "image": info.image_url or "",
        }
        job_id = f"sync_{media_type}_{re.sub(r'[^0-9A-Za-z]+', '', title)}_{season}_{episode}"
        self._schedule_once(job_id, lambda: self._sync_item(payload), f"同步 {title} 到豆瓣", 3)

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event) -> None:
        """处理属于本插件的远程命令。"""
        data = getattr(event, "event_data", None) or {}
        if data.get("action") != "douban_retry":
            return
        self.retry_pending()

    # ---------------- 同步主流程 ----------------

    def _sync_item(self, payload: Dict[str, Any]) -> None:
        """执行一次同步，失败写入待重试队列。"""
        title = payload.get("title") or ""
        media_type = payload.get("media_type") or ""
        season = self._to_int(payload.get("season"), 0, 0)
        episode = self._to_int(payload.get("episode"), 0, 0)
        key = self._archive_key(title, media_type, season)

        with self._lock:
            archive = self.get_data(_KEY_ARCHIVE) or {}
            record = archive.get(key)
            if isinstance(record, dict) and record.get("status") == "collect":
                logger.info(f"{self._display_title(title, season)} 已标记为看完，跳过")
                return

        reader = MediaServerReader(server_name=payload.get("server_name") or None)
        subject_id = payload.get("subject_id") or reader.get_douban_id(payload.get("item_id") or "")
        subject_name = title

        if not subject_id:
            subject_name, subject_id = self._search_subject(title, season, media_type)
        if not subject_id:
            logger.warn(f"{title} 未找到豆瓣条目，跳过本次同步")
            self._save_pending(key, payload, title, season, media_type, "do")
            return

        status = self._resolve_status(payload, reader, media_type, season, episode)
        client = DoubanClient(cookie=self._load_cookie())
        if not client.has_login():
            logger.error("豆瓣 cookie 为空，无法同步，请配置 cookie 或 CookieCloud")
            self._save_pending(key, payload, subject_name or title, season, media_type, status, subject_id)
            return

        display = self._display_title(subject_name or title, season)
        if client.set_status(subject_id=subject_id, status=status, private=self._private):
            logger.info(f"{display} 同步到档案成功（{self._status_text(status)}）")
            # 海报优先用豆瓣官方竖版，取不到再退回媒体服务器的图
            image = client.get_subject_image(subject_id, media_type) or payload.get("image") or ""
            self._save_archive(key, payload, subject_name or title, subject_id,
                               season, media_type, status, display, image=image)
            self._drop_pending(key)
            return

        logger.error(f"{display} 同步到档案失败，加入待重试队列")
        self._save_pending(key, payload, subject_name or title, season, media_type, status, subject_id)

    def retry_pending(self) -> None:
        """重试待处理队列中的条目，成功后移出队列，并顺带补齐缺失的海报。"""
        if not self._enabled:
            return

        # 仪表盘海报墙依赖封面图，历史缺图条目在这里慢慢补齐
        try:
            self._fill_missing_images()
        except Exception as error:
            logger.debug(f"补齐豆瓣海报失败：{error}")

        pending = dict(self.get_data(_KEY_PENDING) or {})
        if not pending:
            logger.info("豆瓣同步失败队列为空")
            return

        logger.info(f"开始重试豆瓣同步失败队列，共 {len(pending)} 条")
        client = DoubanClient(cookie=self._load_cookie())
        if not client.has_login():
            logger.error("豆瓣 cookie 为空，跳过本次重试")
            return

        for key, record in pending.items():
            if not isinstance(record, dict):
                continue
            subject_id = str(record.get("subject_id") or "")
            status = str(record.get("status") or "do")
            display = str(record.get("display") or key)
            if not subject_id:
                # 之前没搜到条目，重新尝试搜索
                subject_name, found = self._search_subject(
                    str(record.get("title") or key),
                    self._to_int(record.get("season"), 0, 0),
                    str(record.get("type") or "TV"),
                )
                if not found:
                    logger.warn(f"{display} 仍未找到豆瓣条目")
                    continue
                subject_id, record["subject_id"] = found, found
                record["subject_name"] = subject_name
            if client.set_status(subject_id=subject_id, status=status, private=self._private):
                logger.info(f"{display} 重试同步成功")
                self._move_pending_to_archive(key, record)
            else:
                logger.warn(f"{display} 重试仍然失败")

    # ---------------- 数据读写 ----------------

    def _save_archive(self, key: str, payload: Dict[str, Any], subject_name: str, subject_id: str,
                      season: int, media_type: str, status: str, display: str,
                      image: str = "") -> None:
        """写入已同步档案。"""
        with self._lock:
            archive = dict(self.get_data(_KEY_ARCHIVE) or {})
            archive[key] = {
                "title": display,
                "subject_name": subject_name,
                "subject_id": subject_id,
                "season": season,
                "episode": self._to_int(payload.get("episode"), 0, 0),
                "type": "电视剧" if media_type == "TV" else "电影",
                "status": status,
                "image": image or payload.get("image") or "",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.save_data(_KEY_ARCHIVE, archive)

    def _fill_missing_images(self, limit: int = 5) -> None:
        """补齐档案里缺失的海报，单次最多处理 limit 条，失败不影响其它逻辑。"""
        with self._lock:
            snapshot = dict(self.get_data(_KEY_ARCHIVE) or {})
            todo = [key for key, item in snapshot.items()
                    if isinstance(item, dict) and item.get("subject_id") and not item.get("image")][:limit]
            if not todo:
                return

        client = DoubanClient(cookie=self._load_cookie())
        filled: Dict[str, str] = {}
        for key in todo:
            item = snapshot.get(key) or {}
            media_type = "MOV" if str(item.get("type") or "").startswith("电影") else "TV"
            image = client.get_subject_image(str(item.get("subject_id") or ""), media_type)
            if image:
                filled[key] = image

        if not filled:
            return
        with self._lock:
            archive = dict(self.get_data(_KEY_ARCHIVE) or {})
            for key, image in filled.items():
                if key in archive and not archive[key].get("image"):
                    archive[key]["image"] = image
            self.save_data(_KEY_ARCHIVE, archive)
        logger.info(f"豆瓣档案补齐海报 {len(filled)} 条")

    def _save_pending(self, key: str, payload: Dict[str, Any], subject_name: str, season: int,
                      media_type: str, status: str, subject_id: str = "") -> None:
        """写入待重试队列。"""
        with self._lock:
            pending = dict(self.get_data(_KEY_PENDING) or {})
            pending[key] = {
                "title": self._display_title(subject_name, season),
                "display": self._display_title(subject_name, season),
                "subject_name": subject_name,
                "subject_id": subject_id,
                "season": season,
                "episode": self._to_int(payload.get("episode"), 0, 0),
                "type": media_type,
                "status": status,
                "image": payload.get("image") or "",
                "server_name": payload.get("server_name") or "",
                "item_id": payload.get("item_id") or "",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.save_data(_KEY_PENDING, pending)

    def _move_pending_to_archive(self, key: str, record: Dict[str, Any]) -> None:
        """条目重试成功后，从队列移出并写入档案。"""
        with self._lock:
            pending = dict(self.get_data(_KEY_PENDING) or {})
            pending.pop(key, None)
            self.save_data(_KEY_PENDING, pending)

            archive = dict(self.get_data(_KEY_ARCHIVE) or {})
            archive[key] = {
                "title": record.get("display") or key,
                "subject_name": record.get("subject_name") or "",
                "subject_id": record.get("subject_id") or "",
                "season": record.get("season") or 0,
                "episode": record.get("episode") or 0,
                "type": "电视剧" if record.get("type") == "TV" else "电影",
                "status": record.get("status") or "do",
                "image": record.get("image") or "",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.save_data(_KEY_ARCHIVE, archive)

    def _drop_pending(self, key: str) -> None:
        """同步成功后清理同名待处理记录。"""
        with self._lock:
            pending = dict(self.get_data(_KEY_PENDING) or {})
            if key in pending:
                pending.pop(key)
                self.save_data(_KEY_PENDING, pending)

    def query_archive(self) -> Dict[str, Any]:
        """返回当前档案与失败队列，供插件页面和外部调用使用。"""
        return {
            "archive": self.get_data(_KEY_ARCHIVE) or {},
            "pending": self.get_data(_KEY_PENDING) or {},
            "enabled": self._enabled,
        }

    # ---------------- 辅助能力 ----------------

    def _search_subject(self, title: str, season: int, media_type: str) -> Tuple[Optional[str], Optional[str]]:
        """
        按标题搜索豆瓣条目。
        先尝试带季数的标题（部分剧集按季单独建条目），再退回主标题。
        """
        client = DoubanClient()
        candidates = []
        season_title = self._display_title(title, season)
        if season_title != title:
            candidates.append(season_title)
        candidates.append(title)

        for candidate in candidates:
            subject_name, subject_id = client.search(candidate, media_type)
            if subject_id:
                return subject_name, subject_id
        return None, None

    def _resolve_status(self, payload: Dict[str, Any], reader: MediaServerReader,
                        media_type: str, season: int, episode: int) -> str:
        """
        判断应写入的状态：看完→collect，在看→do。
        剧集依据媒体服务器该季已收录集数判断，取不到时按在看处理。
        """
        if media_type != "TV":
            return "collect"
        total = reader.get_season_total(payload.get("item_id") or "", season)
        if total and episode >= total:
            return "collect"
        return "do"

    def _load_cookie(self) -> str:
        """读取豆瓣 cookie：优先插件配置，其次 CookieCloud。"""
        if self._cookie.strip():
            return self._cookie.strip()
        if not self._use_cookiecloud:
            return ""
        host = self._cc_host or str(getattr(settings, "COOKIECLOUD_HOST", "") or "")
        key = self._cc_key or str(getattr(settings, "COOKIECLOUD_KEY", "") or "")
        password = self._cc_password or str(getattr(settings, "COOKIECLOUD_PASSWORD", "") or "")
        if not (host and key and password):
            return ""
        url = f"{host.rstrip('/')}/get/{key}"
        try:
            response = RequestUtils(timeout=15).post_res(url, json={"password": password})
        except Exception as error:
            logger.warn(f"从 CookieCloud 获取 cookie 失败：{error}")
            return ""
        if response is None or response.status_code != 200:
            logger.warn("CookieCloud 返回异常，无法获取豆瓣 cookie")
            return ""
        try:
            data = response.json() or {}
        except Exception:
            logger.warn("CookieCloud 响应解析失败")
            return ""
        cookie_data = data.get("cookie_data") or {}
        cookie = cookie_data.get("douban.com") or cookie_data.get("www.douban.com") or ""
        if cookie:
            logger.info("已从 CookieCloud 获取豆瓣 cookie")
        return str(cookie)

    def _schedule_once(self, job_id: str, func: Any, name: str, delay: int) -> None:
        """把任务交给宿主的延后任务队列，不占用事件线程。"""
        adder = getattr(scheduler_sdk, "add_plugin_once_job", None)
        if not adder:
            threading.Thread(target=func, daemon=True).start()
            return
        try:
            adder(self.__class__.__name__, job_id, func, name, delay_seconds=delay)
        except Exception as error:
            logger.warn(f"登记延后任务失败，改为立即执行：{error}")
            threading.Thread(target=func, daemon=True).start()

    def _remove_once(self, job_id: str) -> None:
        """移除尚未执行的延后任务。"""
        remover = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if not remover:
            return
        try:
            remover(self.__class__.__name__, job_id)
        except Exception as error:
            logger.debug(f"移除延后任务失败：{error}")

    def _is_target_user(self, user_name: Optional[str]) -> bool:
        """判断事件用户是否在配置的媒体库用户名列表中。"""
        allowed = [item.strip() for item in self._users.split(",") if item.strip()]
        if not allowed:
            return False
        return (user_name or "") in allowed

    def _is_sync_event(self, info: WebhookEventInfo) -> bool:
        """判断是否为需要处理的播放或标记已观看事件。"""
        event = info.event or ""
        if event in _PLAY_START or event in _PLAYED:
            return True
        return (info.channel or "").lower() == "jellyfin" \
            and event == "UserDataSaved" and info.save_reason == "TogglePlayed"

    def _is_allowed_path(self, item_path: Optional[str]) -> bool:
        """按路径关键词过滤不需要同步的媒体。"""
        keywords = [item.strip() for item in self._exclude.split(",") if item.strip()]
        if not keywords:
            return True
        path = item_path or ""
        if not path:
            return True
        return not any(keyword in path for keyword in keywords)

    def _parse_media(self, info: WebhookEventInfo) -> Tuple[str, str, int, int]:
        """
        从事件解析标题、类型、季和集。
        :return: (标题, 类型 TV/MOV, 季, 集)，无法识别时类型为空字符串
        """
        raw = (info.item_name or "").strip()
        title = re.split(r"\s*[-–]\s*S\d|\s+S\d", raw)[0].strip() or raw
        season = self._to_int(info.season_id, 0, 0)
        episode = self._to_int(info.episode_id, 0, 0)

        item_type = (info.item_type or "").upper()
        if item_type.startswith("TV") or season or episode:
            if item_type.startswith("MOV") and not season:
                return title, "MOV", season, episode
            return title, "TV", season, episode
        if item_type.startswith("MOV"):
            return title, "MOV", season, episode
        return title, "", season, episode

    def _archive_key(self, title: str, media_type: str, season: int) -> str:
        """生成档案键，剧集按标题加季去重。"""
        if media_type == "TV" and season:
            return f"{title}_S{season}"
        return title

    @staticmethod
    def _display_title(title: str, season: int) -> str:
        """按季生成展示标题。"""
        if season and season > 1:
            return f"{title} 第{season}季"
        return title

    @staticmethod
    def _status_text(status: str) -> str:
        """把状态取值转换为中文说明。"""
        return {"collect": "看过", "do": "在看", "wish": "想看"}.get(status, status)

    @staticmethod
    def _to_int(value: Any, default: int, minimum: int) -> int:
        """安全转换为整数并按最小值收敛。"""
        try:
            result = int(str(value).strip())
        except (TypeError, ValueError):
            return default
        return max(result, minimum)

    # ---------------- 页面 ----------------

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """返回配置页面与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "private", "label": "仅自己可见"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "skip_first", "label": "不标记第一集"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "users",
                                            "label": "媒体库用户名",
                                            "placeholder": "多个用户名以逗号分隔",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exclude",
                                            "label": "媒体路径排除关键词",
                                            "placeholder": "多个关键词以逗号分隔",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cookie",
                                            "label": "豆瓣cookie",
                                            "placeholder": "留空则从 CookieCloud 获取",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "use_cookiecloud", "label": "使用CookieCloud"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即重试失败队列"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "pc_month", "label": "大屏显示月份数", "placeholder": "3"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "pc_num", "label": "大屏每月最多显示", "placeholder": "50"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "mobile_month", "label": "小屏显示月份数", "placeholder": "2"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {"model": "mobile_num", "label": "小屏每月最多显示", "placeholder": "15"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "本插件不依赖 TMDB 识别；豆瓣 ID 优先使用媒体服务器已刮削的标识。"
                                                   "同步失败会进入队列，每 30 分钟自动重试。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "private": True,
            "skip_first": True,
            "users": "",
            "exclude": "",
            "cookie": "",
            "use_cookiecloud": True,
            "cookiecloud_host": "",
            "cookiecloud_key": "",
            "cookiecloud_password": "",
            "onlyonce": False,
            "pc_month": 3,
            "pc_num": 50,
            "mobile_month": 2,
            "mobile_num": 15,
        }

    def get_page(self) -> List[Dict[str, Any]]:
        """返回详情页，展示已同步档案与失败队列概况。"""
        archive = self.get_data(_KEY_ARCHIVE) or {}
        pending = self.get_data(_KEY_PENDING) or {}
        recent = sorted(
            [item for item in archive.values() if isinstance(item, dict) and item.get("timestamp")],
            key=lambda item: item["timestamp"],
            reverse=True,
        )[:20]

        rows = []
        for item in recent:
            rows.append({
                "component": "VListItem",
                "props": {
                    "title": item.get("title", ""),
                    "subtitle": f"{item.get('timestamp', '')} · {self._status_text(item.get('status', ''))}",
                },
            })
        if not rows:
            rows = [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "暂无同步记录"}}]

        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {"component": "VCard", "props": {"variant": "tonal"},
                             "content": [{"component": "VCardText",
                                          "props": {"text": f"已同步：{len(archive)} 条"}}]}
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {"component": "VCard", "props": {"variant": "tonal"},
                             "content": [{"component": "VCardText",
                                          "props": {"text": f"待重试：{len(pending)} 条"}}]}
                        ],
                    },
                ],
            },
            {"component": "VList", "content": rows},
        ]

    @staticmethod
    def get_dashboard_meta() -> Optional[List[Dict[str, str]]]:
        """声明仪表盘 key 与名称，不声明时宿主只能给出空 key，组件标题会显示异常。"""
        return [{"key": _KEY_DASHBOARD, "name": "豆瓣档案"}]

    def get_dashboard(self, key: str, **kwargs: Any) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]]:
        """返回仪表盘：按月分组的豆瓣档案海报墙。"""
        if key and key != _KEY_DASHBOARD:
            return None
        mobile = self._is_mobile(kwargs.get("user_agent"))
        archive = self.get_data(_KEY_ARCHIVE) or {}
        limit_month = max(1, self._mobile_month if mobile else self._pc_month)
        limit_num = max(1, self._mobile_num if mobile else self._pc_num)

        # 显式给出标题，避免宿主用插件名兜底导致标题不一致
        attrs: Dict[str, Any] = {
            "refresh": 600,
            "border": True,
            "title": "豆瓣档案同步",
            "subtitle": f"共 {len(archive)} 部",
        }
        empty = [
            {"component": "VAlert",
             "props": {"type": "info", "variant": "tonal",
                       "text": "还没有同步记录，在 Emby / Jellyfin 里看一集后会自动出现在这里。"}}
        ]

        sorted_items = sorted(
            [item for item in archive.values() if isinstance(item, dict) and item.get("timestamp")],
            key=lambda item: item["timestamp"],
            reverse=True,
        )
        if not sorted_items:
            return {"cols": 12, "md": 6}, attrs, empty

        # 按 (年, 月) 分组，跨年时月份不会串在一起
        groups: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        for record in sorted_items:
            time_object = self._parse_time(record.get("timestamp"))
            if time_object is None:
                continue
            groups.setdefault((time_object.year, time_object.month), []).append(record)

        timeline_items: List[Dict[str, Any]] = []
        for year, month in sorted(groups.keys(), reverse=True)[:limit_month]:
            cards = [card for card in (self._build_card(item, mobile) for item in groups[(year, month)]) if card]
            if not cards:
                continue
            timeline_items.append({
                "component": "VTimelineItem",
                "props": {"size": "x-small", "dot-color": "primary"},
                "content": [
                    {
                        "component": "h1",
                        "props": {"class": "text-base",
                                  "style": "padding:0rem 0rem 0.5rem 0rem;font-weight:bold;"},
                        "html": f"{year}年{month}月 <span class='text-sm font-normal'>共 {len(cards)} 部</span>",
                    },
                    {
                        "component": "VRow",
                        "props": {"class": "pa-0 ma-0", "style": "padding:0rem;"},
                        "content": cards[:limit_num],
                    },
                ],
            })

        if not timeline_items:
            return {"cols": 12, "md": 6}, attrs, empty

        return (
            {"cols": 12, "md": 8},
            attrs,
            [
                {
                    # VTimelineItem 必须放在 VTimeline 里，否则时间线渲染不出来
                    "component": "VTimeline",
                    "props": {"density": "compact"},
                    "content": timeline_items,
                }
            ],
        )

    # ---------------- 仪表盘辅助 ----------------

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        """解析档案里的时间戳，格式不对返回 None。"""
        if not value:
            return None
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    @staticmethod
    def _build_card(record: Dict[str, Any], mobile: bool) -> Optional[Dict[str, Any]]:
        """把一条档案记录渲染成海报卡片；没有豆瓣条目就无处跳转，直接跳过。"""
        subject_id = str(record.get("subject_id") or "")
        if not subject_id:
            return None

        title = str(record.get("title") or record.get("subject_name") or "")
        status = DoubanArchive._status_text(str(record.get("status") or ""))
        width, height = ("44px", "66px") if mobile else ("66px", "99px")
        image = str(record.get("image") or "")

        if image:
            body = [
                {
                    "component": "VImg",
                    "props": {
                        "src": image,
                        "cover": True,
                        "aspect-ratio": "2/3",
                        "style": f"width:{width}; height:{height};",
                    },
                }
            ]
        else:
            # 缺封面时用文字占位，保证这条记录不会从海报墙上凭空消失
            body = [
                {
                    "component": "VCardText",
                    "props": {
                        "class": "text-caption text-center pa-1",
                        "style": f"width:{width}; height:{height};display:flex;"
                                 f"align-items:center;justify-content:center;",
                        "text": title[:4] or "豆瓣",
                    },
                }
            ]

        return {
            "component": "a",
            "props": {
                "href": f"https://movie.douban.com/subject/{subject_id}/",
                "target": "_blank",
                "title": f"{title} · {status}",
                "style": "padding: 0.2rem; text-decoration: none;",
            },
            "content": [
                {"component": "VCard", "props": {"class": "elevation-2"}, "content": body}
            ],
        }

    @staticmethod
    def _is_mobile(user_agent: Optional[str]) -> bool:
        """按 UA 判断是否为移动端。"""
        if not user_agent:
            return False
        return any(keyword.lower() in user_agent.lower()
                   for keyword in ("mobile", "android", "iphone", "ipad", "kindle"))
