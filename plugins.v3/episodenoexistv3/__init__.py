"""缺失集数订阅 V3 版（EpisodeNoExistV3）

原插件：boeto/MoviePilot-Plugins · plugins/episodenoexist · v0.0.8
本版本针对 MoviePilot V3（app v3.1.2）重做适配，功能与原插件保持一致。

V3 适配要点：
1. 导入路径迁移：app.plugins._PluginBase → app.plugins；app.core.config.settings →
   app.sdk.config；app.log → app.sdk.logging；MediaType 改从 app.schemas 导入。
2. 媒体身份模型重构：V2 的 `item.tmdbid` / `recognize_media(tmdbid=...)` /
   `subscribeoper.exists(tmdbid, ...)` 在 V3 全部改为 `media_source` + `media_id` 成对传入，
   且 subscribeoper.exists 的 media_source / media_id 是位置参数。
3. 订阅去重口径：V3 的 exists() 按媒体身份 + 季号判定，原「只查 tmdbid」的单参写法已失效。
4. 媒体服务器连接信息改用 app.sdk.services.MediaServerHelper 获取实例，
   不再依赖 settings.MEDIASERVER 这个已不再直读的键。
5. API_TOKEN 不再从 settings 直接读取，改用 app.runtime.settings.get_runtime_setting。
6. Pydantic v2 下 `.dict()` 已废弃，统一改为 `.model_dump()`。
7. 缺失集数计算依赖的 TMDB 季集信息改走 app.chain.tmdb.TmdbChain.tmdb_episodes，
   按 air_date 过滤掉尚未播出的集。
"""
from __future__ import annotations

import datetime
from enum import Enum
from threading import Event
from typing import Any, Dict, List, Optional, TypedDict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel

from app import schemas
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.services import MediaServerHelper
from app.schemas import MediaType


def _get_runtime_setting(key: str, default: Any = None) -> Any:
    """读取运行时配置。

    V3 把部署设置挪到了 app.runtime.settings，settings 对象不再保证暴露全部键。
    这里做一次兜底：优先走运行时设置，取不到再回落到 settings 属性。
    """
    try:
        from app.runtime.settings import get_runtime_setting

        value = get_runtime_setting(key, default)
        if value not in (None, ""):
            return value
    except Exception as err:  # pragma: no cover - 宿主版本差异兜底
        logger.debug(f"读取运行时配置 {key} 失败：{err}")
    return getattr(settings, key, default)


class HistoryStatus(Enum):
    UNKNOW = "未知状态"
    ALL_EXIST = "全部存在"
    ADDED_RSS = "已加订阅"
    NO_EXIST = "存在缺失"
    FAILED = "获取失败"


class HistoryDataType(Enum):
    ALL_EXIST = "全部存在"
    ADDED_RSS = "已加订阅"
    NO_EXIST = "存在缺失"
    FAILED = "失败记录"
    ALL = "所有记录"
    LATEST = "最新6条记录"
    NOT_ALL_NO_EXIST = "非全集缺失"


class NoExistAction(Enum):
    ONLY_HISTORY = "仅检查记录"
    ADD_SUBSCRIBE = "添加到订阅"
    SET_ALL_EXIST = "标记为存在"


class Icons(Enum):
    STATISTICS = "icon_statistics"
    WARNING = "icon_warning"
    BUG_REMOVE = "icon_bug_remove"
    GLASSES = "icon_3d_glasses"
    ADD_SCHEDULE = "icon_add_schedule"
    TARGET = "icon_target"


class EpisodeNoExistInfo(BaseModel):
    # 季
    season: Optional[int] = None

    # 缺失剧集列表
    episode_no_exist: Optional[List[int]] = None

    # 总集数
    episode_total: Optional[int] = 0


class TvNoExistInfo(BaseModel):
    """电视剧媒体信息。"""

    title: Optional[str] = "未知"
    year: Optional[str] = "未知"
    path: Optional[str] = "未知"

    # 媒体来源（V3 起不再是单一 tmdbid）
    media_source: Optional[str] = None
    # 媒体来源原生 ID
    media_id: Optional[str] = None

    # 海报地址
    poster_path: Optional[str] = "/assets/no-image-CweBJ8Ee.jpeg"
    # 评分
    vote_average: Optional[float | str] = "未知"
    # 最后发行日期
    last_air_date: Optional[str] = "未知"

    season_episode_no_exist_info: Optional[Dict[int, Dict[str, Any]]] = None


class HistoryDetail(TypedDict):
    exist_status: Optional[str]
    tv_no_exist_info: Optional[TvNoExistInfo]
    last_update: Optional[str]
    last_update_full: Optional[str]


class ExtendedHistoryDetail(HistoryDetail):
    unique: Optional[str]


class EpisodeNoExistV3(_PluginBase):
    # 插件名称
    plugin_name = "缺失集数订阅V3版"
    # 插件描述
    plugin_desc = "订阅媒体库缺失集数的电视剧（适配 MoviePilot V3）"
    # 插件图标
    plugin_icon = "episodenoexist.png"
    # 插件版本
    plugin_version = "1.0.1"
    # 插件作者
    plugin_author = "boeto / liclown2020"
    # 作者主页
    author_url = "https://github.com/liclown2020/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "episodenoexistv3_"
    # 加载顺序
    plugin_order = 6
    # 可使用的用户级别
    auth_level = 2

    # 退出事件
    _event = Event()

    # 私有属性
    subscribechain: SubscribeChain
    _plugin_id = "EpisodeNoExistV3"
    _scheduler = None

    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _clear: bool = False
    _clearflag: bool = False

    _history_type: str = HistoryDataType.LATEST.value
    _no_exist_action: str = NoExistAction.ONLY_HISTORY.value
    _only_exist_season: bool = False
    _save_path_replaces: List[str] = []
    _whitelist_librarys: List[str] = []
    _whitelist_media_servers: List[str] = []

    def init_plugin(self, config: dict[str, Any] | None = None):
        self.subscribechain = SubscribeChain()
        self.mediachain = MediaChain()
        self.tmdb = TmdbChain()

        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = (
                config.get("cron", "").strip() if config.get("cron", "").strip() else ""
            )

            self._clear = config.get("clear", False)

            self._no_exist_action = config.get(
                "no_exist_action", NoExistAction.ONLY_HISTORY.value
            )

            # 仅检查媒体库里已存在的季：库里完全没有的季不参与检查、不自动订阅。
            # 适合「只补漏、不想被尚未入库的季刷屏」的场景。
            self._only_exist_season = bool(config.get("only_exist_season", False))

            self._history_type = config.get(
                "history_type", HistoryDataType.LATEST.value
            )
            _save_path_replaces = config.get("save_path_replaces", "")
            if _save_path_replaces and isinstance(_save_path_replaces, str):
                self._save_path_replaces = _save_path_replaces.split("\n")
            else:
                self._save_path_replaces = []

            _whitelist_librarys = config.get("whitelist_librarys", "")
            if _whitelist_librarys and isinstance(_whitelist_librarys, str):
                self._whitelist_librarys = [
                    item.strip()
                    for item in _whitelist_librarys.split(",")
                    if item.strip()
                ]
            else:
                self._whitelist_librarys = []

            _whitelist_media_servers = config.get("whitelist_media_servers", "")
            if _whitelist_media_servers and isinstance(_whitelist_media_servers, str):
                self._whitelist_media_servers = [
                    item.strip()
                    for item in _whitelist_media_servers.split(",")
                    if item.strip()
                ]
            else:
                self._whitelist_media_servers = []

        # 停止现有任务
        self.stop_service()

        # 启动服务
        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(
                    timezone=_get_runtime_setting("TZ", "Asia/Shanghai")
                )
                logger.info(f"{self.plugin_name}服务启动, 立即运行一次")
                self._scheduler.add_job(
                    func=self.__refresh,
                    trigger="date",
                    run_date=datetime.datetime.now(
                        tz=pytz.timezone(_get_runtime_setting("TZ", "Asia/Shanghai"))
                    )
                    + datetime.timedelta(seconds=3),
                )

                if self._scheduler.get_jobs():
                    # 启动服务
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if self._onlyonce or self._clear:
                # 记录缓存清理标志
                self._clearflag = self._clear

                # 关闭清理缓存
                self._clear = False
                # 关闭一次性开关
                self._onlyonce = False

                # 保存配置
                self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": f"删除 {self.plugin_name} 检查记录",
            },
            {
                "path": "/set_all_exist_history",
                "endpoint": self.set_all_exist_history,
                "methods": ["GET"],
                "summary": f"标记 {self.plugin_name} 存在记录",
            },
            {
                "path": "/add_subscribe_history",
                "endpoint": self.add_subscribe_history,
                "methods": ["GET"],
                "summary": f"订阅 {self.plugin_name} 缺失记录",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [
                {
                    "id": "EpisodeNoExistV3",
                    "name": f"{self.plugin_name}",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.__refresh,
                    "kwargs": {},
                }
            ]
        elif self._enabled:
            return [
                {
                    "id": "EpisodeNoExistV3",
                    "name": f"{self.plugin_name}",
                    "trigger": CronTrigger.from_crontab("0 8 * * *"),
                    "func": self.__refresh,
                    "kwargs": {},
                }
            ]
        return []

    def __refresh(self):
        self.__get_mediaserver_tv_info()

    def __mediaserver_names(self) -> List[str]:
        """
        枚举已配置的媒体服务器名称。

        V3 不再从settings.MEDIASERVER 直读，改由 MediaServerHelper 枚举实例。
        """
        names: List[str] = []
        try:
            services = MediaServerHelper().get_services()
        except Exception as err:
            logger.warning(f"获取媒体服务器列表失败：{err}")
            return names
        for name, service in (services or {}).items():
            instance = getattr(service, "instance", None)
            if instance is None:
                logger.warning(f"媒体服务器 {name} 未连接，跳过")
                continue
            if getattr(instance, "is_inactive", None) and instance.is_inactive():
                logger.warning(f"媒体服务器 {name} 未连接，跳过")
                continue
            names.append(name)
        return names

    def __librarys(self, server: str) -> List[Dict[str, Any]]:
        """读取指定媒体服务器的媒体库列表，归一化成字典。"""
        instance = self.__instance(server)
        if instance is None:
            return []
        try:
            libraries = instance.get_librarys()
        except Exception as err:
            logger.warning(f"读取 {server} 媒体库列表失败：{err}")
            return []

        result: List[Dict[str, Any]] = []
        for library in libraries or []:
            # 兼容 pydantic 模型与 dict 两种返回
            if isinstance(library, dict):
                name = library.get("name")
                lib_id = library.get("id")
            else:
                name = getattr(library, "name", None)
                lib_id = getattr(library, "id", None)
            if not name or not lib_id:
                continue
            result.append({"id": str(lib_id), "name": str(name)})
        return result

    def __season_episodes(
        self, server: str, item_id: str, media_source: str | None, media_id: str | None
    ) -> Tuple[bool, Dict[int, List[int]]]:
        """
        读取媒体库中某个剧集条目实际存在的集号，按季归组。

        走宿主媒体服务器模块的 get_tv_episodes()：它内部按季分组返回
        {季号: [集号]}，并已处理好「缓存 item_id失效时按标题回退搜索」，
        与宿主判断剧集存在性的口径一致。

        返回 (读取成功, {季号: [集号]})。必须区分「读取失败」与「库里一集都没有」：
        前者是接口异常，应跳过本条；后者是真实的全季缺失，要参与统计。
        两者都返回空字典会让失败被当成「整部剧所有季都缺」。
        """
        instance = self.__instance(server)
        if instance is None:
            return False, {}

        try:
            result = instance.get_tv_episodes(
                item_id=item_id,
                media_source=media_source,
                media_id=media_id,
            )
        except TypeError:
            # 兼容不接受媒体身份参数的旧签名
            try:
                result = instance.get_tv_episodes(item_id=item_id)
            except Exception as err:
                logger.warning(f"读取【{item_id}】集号失败：{err}")
                return False, {}
        except Exception as err:
            logger.warning(f"读取【{item_id}】集号失败：{err}")
            return False, {}

        # 返回 (item_id, {季: [集号]})；item_id 为 None 或第二项为 None 均视为读取失败
        if not isinstance(result, tuple) or len(result) < 2:
            logger.warning(f"读取【{item_id}】集号返回格式异常：{type(result)}")
            return False, {}

        resolved_id, seasoninfo = result[0], result[1]
        if resolved_id is None or seasoninfo is None:
            logger.warning(
                f"读取【{item_id}】集号失败：媒体服务器未能定位该剧集"
                f"（可能已被删除或重新入库）"
            )
            return False, {}

        if not seasoninfo:
            # 定位成功但库里没有任何集：真实的全季缺失
            logger.debug(f"【{item_id}】媒体库中没有任何集，按全部缺失处理")
            return True, {}

        return True, {
            int(season): sorted({int(num) for num in numbers or [] if num})
            for season, numbers in seasoninfo.items()
            if season is not None
        }

    def __instance(self, server: str) -> Any:
        """取指定媒体服务器的可用实例，取不到返回 None。"""
        try:
            services = MediaServerHelper().get_services(name_filters=[server])
        except Exception as err:
            logger.warning(f"获取媒体服务器 {server} 实例失败：{err}")
            return None
        service = (services or {}).get(server)
        return getattr(service, "instance", None) if service else None

    @staticmethod
    def __item_attr(item: Any, name: str, default: Any = None) -> Any:
        """兼容 dict 与 pydantic 模型的属性读取。"""
        if isinstance(item, dict):
            return item.get(name, default)
        return getattr(item, name, default)

    def __get_mediaserver_tv_info(self):
        """
        获取媒体库电视剧数据
        """
        logger.info("开始获取媒体库电视剧数据 ...")
        if self._clearflag:
            logger.info("清理检查记录")
            self.save_data("history", "")
            self._clearflag = False
            _history = None
        else:
            _history = self.get_data("history")

        history: Dict[str, Any] = (
            _history if _history else {"item_unique_flags": [], "details": {}}
        )

        # 添加检查记录
        def __append_history(
            item_unique_flag: str,
            exist_status: HistoryStatus,
            tv_no_exist_info: TvNoExistInfo | Dict[str, Any] | None = None,
        ):
            if tv_no_exist_info and isinstance(tv_no_exist_info, TvNoExistInfo):
                tv_no_exist_info = tv_no_exist_info.model_dump()

            current_time = datetime.datetime.now(
                tz=pytz.timezone(_get_runtime_setting("TZ", "Asia/Shanghai"))
            )

            history["item_unique_flags"].append(item_unique_flag)

            history["details"][item_unique_flag] = {
                "exist_status": exist_status.value,
                "tv_no_exist_info": (tv_no_exist_info if tv_no_exist_info else None),
                "last_update": current_time.strftime("%m-%d %H:%M"),
                "last_update_full": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            logger.info(
                f"添加检查记录: {item_unique_flag}: {history['details'][item_unique_flag]}"
            )

            self.save_data("history", history)

        mediaservers = self.__mediaserver_names()
        if not mediaservers:
            logger.warning("未获取到可用的媒体服务器")
            return

        # 白名单, 只获取白名单内指定的媒体库
        logger.info(
            f"媒体服务器白名单: {self._whitelist_media_servers if self._whitelist_media_servers else '全部'}"
        )
        logger.info(f"媒体库白名单: {self._whitelist_librarys}")

        item_unique_flags = history.get("item_unique_flags", [])
        logger.debug(f"item_unique_flags: {item_unique_flags}")

        # 本轮统计，用于结束时汇总
        stat_total = 0# 枚举到的条目总数
        stat_failed = 0      # 处理过程中出错的条目数

        # 遍历媒体服务器
        for mediaserver in mediaservers:
            if (
                self._whitelist_media_servers
                and mediaserver not in self._whitelist_media_servers
            ):
                logger.info(f"【{mediaserver}】不在媒体服务器白名单内, 跳过")
                continue
            logger.info(f"开始获取媒体库 {mediaserver} 的数据 ...")
            for library in self.__librarys(mediaserver):
                library_id = library["id"]
                library_name = library["name"]
                logger.debug(f"媒体库名：{library_name}")
                if library_name not in self._whitelist_librarys:
                    logger.debug(
                        f"媒体库【{library_name}】不在白名单内, 跳过"
                        f"（当前白名单: {self._whitelist_librarys}）"
                    )
                    continue
                logger.info(f"正在获取 {mediaserver} 媒体库 {library_name} ...")

                items = self.__media_items(mediaserver, library_id)
                stat_total += len(items)
                logger.info(
                    f"媒体库【{library_name}】共枚举到 {len(items)} 个条目, 开始逐部检查"
                )

                for item in items:
                    try:
                        self.__process_item(
                            item,
                            mediaserver=mediaserver,
                            library_id=library_id,
                            library_name=library_name,
                            history=history,
                            item_unique_flags=item_unique_flags,
                            append_history=__append_history,
                        )
                    except Exception as err:
                        # 任何单部剧的异常都不允许中断整轮扫描
                        stat_failed += 1
                        title = self.__item_attr(item, "title") if item else None
                        logger.warning(
                            f"处理【{title or '未知条目'}】时出错，已跳过本条：{err}"
                        )
                        continue

                logger.info(f"{mediaserver} 媒体库 {library_name} 获取数据完成")

        logger.info(
            f"媒体库缺失集数据获取完成, 本轮枚举 {stat_total} 个条目, "
            f"处理出错 {stat_failed} 个, 累计已检查 {len(item_unique_flags)} 部"
        )

    def __process_item(
        self,
        item: Any,
        mediaserver: str,
        library_id: str,
        library_name: str,
        history: Dict[str, Any],
        item_unique_flags: List[str],
        append_history: Any,
    ):
        """处理单个媒体库条目。由外层循环兜异常，保证单条失败不影响整轮。"""
        if not item:
            logger.debug("未获取到Item媒体信息, 跳过获取缺失集数")
            return

        item_id = self.__item_attr(item, "item_id")
        if not item_id:
            logger.debug("未获取到Item ID, 跳过获取缺失集数")
            return
        item_id = str(item_id)

        item_title = (
            self.__item_attr(item, "title")
            or self.__item_attr(item, "original_title")
            or f"ItemID: {item_id}"
        )

        item_unique_flag = f"{mediaserver}_{library_id}_{item_id}_{item_title}"

        if item_unique_flag in item_unique_flags:
            logger.info(f"【{item_title}】已处理过, 跳过")
            return

        logger.info(f"正在获取 {item_title} ...")

        seasoninfo: Dict[int, List[int]] = {}

        # 类型：V3 的 item_type 已由宿主归一为 MediaType 值
        raw_item_type = str(self.__item_attr(item, "item_type") or "").lower()
        if raw_item_type in ("series", "show", MediaType.TV.value.lower()):
            item_type = MediaType.TV.value
        elif raw_item_type in ("movie", MediaType.MOVIE.value.lower()):
            item_type = MediaType.MOVIE.value
        else:
            item_type = MediaType.TV.value

        if item_type == MediaType.MOVIE.value:
            logger.debug(f"【{item_title}】为{MediaType.MOVIE.value}, 跳过")
            return

        # V3 媒体身份：media_source + media_id 成对存在
        media_source = self.__item_attr(item, "media_source")
        media_id = self.__item_attr(item, "media_id")
        # 兼容仍按 V2 结构返回 tmdbid 的宿主/适配器
        legacy_tmdbid = self.__item_attr(item, "tmdbid")
        if not media_id and legacy_tmdbid:
            media_source = media_source or "themoviedb"
            media_id = str(legacy_tmdbid)

        if item_type == MediaType.TV.value and media_id:
            read_ok, seasoninfo = self.__season_episodes(
                mediaserver, item_id, media_source, media_id
            )
            if not read_ok:
                # 读不到集号就不能判断缺失，跳过本条，避免把接口故障当成「整部剧全缺」
                logger.warning(
                    f"【{item_title}】无法读取集号，跳过缺失判断"
                    f"（不影响其他条目继续扫描）"
                )
                return
            logger.debug(f"获取到媒体库【{item_title}】季集信息:{seasoninfo}")

        # 插入数据
        item_dict: Dict[str, Any] = {
            "title": self.__item_attr(item, "title"),
            "original_title": self.__item_attr(item, "original_title"),
            "year": self.__item_attr(item, "year"),
            "path": self.__item_attr(item, "path"),
            "media_source": str(media_source) if media_source else None,
            "media_id": str(media_id) if media_id else None,
            "tmdbid": int(media_id)
                if media_id and str(media_source or "themoviedb") == "themoviedb"
                    and str(media_id).isdigit()
                else None,
            "seasoninfo": seasoninfo,
            "item_type": item_type,
        }

        logger.info(f"获到媒体库【{item_title}】数据：{item_dict}")

        is_add_subscribe_success, tv_no_exist_info = (
            self.__get_item_no_exist_info(item_dict)
        )

        if is_add_subscribe_success and tv_no_exist_info:
            if tv_no_exist_info.season_episode_no_exist_info is None:
                logger.info(f"【{item_title}】所有季集均已存在/订阅")
                append_history(
                    item_unique_flag=item_unique_flag,
                    exist_status=HistoryStatus.ALL_EXIST,
                    tv_no_exist_info=tv_no_exist_info,
                )
            else:
                logger.info(f"【{item_title}】缺失集数信息：{tv_no_exist_info}")

                if (
                    self._no_exist_action
                    == NoExistAction.ADD_SUBSCRIBE.value
                ):
                    logger.info("开始订阅缺失集数")
                    is_add_subscribe_success = (
                        self.__add_subscribe_by_tv_no_exist_info(
                            tv_no_exist_info, item_unique_flag
                        )
                    )
                    if is_add_subscribe_success:
                        append_history(
                            item_unique_flag=item_unique_flag,
                            exist_status=HistoryStatus.ADDED_RSS,
                            tv_no_exist_info=tv_no_exist_info,
                        )
                    else:
                        logger.warning(
                            f"订阅【{item_title}】失败, 仅记录缺失集数"
                        )
                        append_history(
                            item_unique_flag=item_unique_flag,
                            exist_status=HistoryStatus.NO_EXIST,
                            tv_no_exist_info=tv_no_exist_info,
                        )
                elif (
                    self._no_exist_action
                    == NoExistAction.SET_ALL_EXIST.value
                ):
                    logger.debug("将缺失季集标记为存在")
                    append_history(
                        item_unique_flag=item_unique_flag,
                        exist_status=HistoryStatus.ALL_EXIST,
                        tv_no_exist_info=tv_no_exist_info,
                    )

                else:
                    logger.debug("仅记录缺失集数")
                    append_history(
                        item_unique_flag=item_unique_flag,
                        exist_status=HistoryStatus.NO_EXIST,
                        tv_no_exist_info=tv_no_exist_info,
                    )
        else:
            logger.warning(f"【{item_title}】获取缺失集数信息失败")
            append_history(
                item_unique_flag=item_unique_flag,
                exist_status=HistoryStatus.FAILED,
                tv_no_exist_info=tv_no_exist_info,
            )

    def __media_items(self, server: str, library_id: str):
        """读取指定媒体库下的全部剧集条目，失败返回空列表。"""
        instance = self.__instance(server)
        if instance is None:
            return []
        try:
            # get_items 是生成器，且内部已递归子目录并只产出 Movie/Series/MusicAlbum
            return list(instance.get_items(library_id) or [])
        except Exception as err:
            logger.warning(f"读取 {server} 媒体库 {library_id} 条目失败：{err}")
            return []

    def __get_item_no_exist_info(
        self, item_dict: dict[str, Any]
    ) -> tuple[bool, TvNoExistInfo]:
        """
        获取缺失集数
        """

        title = item_dict.get("title") or item_dict.get("original_title") or "未知标题"

        tv_no_exist_info = TvNoExistInfo(
            title=title,
            year=item_dict.get("year", ""),
            path=item_dict.get("path", ""),
        )

        media_source: str | None = item_dict.get("media_source")
        media_id: str | None = item_dict.get("media_id")
        if not media_id:
            logger.debug(f"【{title}】未获取到媒体ID, 跳过获取缺失集数")
            return False, tv_no_exist_info
        # V3 身份必须成对，缺失来源时按 TMDB 兜底（TMDB 季集信息最完整）
        media_source = media_source or "themoviedb"
        tv_no_exist_info.media_source = media_source
        tv_no_exist_info.media_id = str(media_id)

        mtype = item_dict.get("item_type")
        if not mtype:
            logger.debug(f"【{title}】未获取到媒体类型, 跳过获取缺失集数")
            return False, tv_no_exist_info

        # 添加不存在的季集信息
        def __append_season_info(
            season: int,
            episode_no_exist: list,
            episode_total: int,
        ):
            logger.debug(f"添加【{title}】第【{season}】季缺失集：{episode_no_exist}")
            __season_info = EpisodeNoExistInfo(
                season=season,
                episode_no_exist=episode_no_exist,
                episode_total=episode_total,
            ).model_dump()
            logger.debug(f"【{title}】第【{season}】季缺失集信息：{__season_info}")

            if not tv_no_exist_info.season_episode_no_exist_info:
                tv_no_exist_info.season_episode_no_exist_info = {season: __season_info}
            else:
                tv_no_exist_info.season_episode_no_exist_info[season] = __season_info
            logger.debug(f"【{title}】缺失季集数的电视剧信息：{tv_no_exist_info}")

        exist_season_info = item_dict.get("seasoninfo") or {}

        logger.debug(f"【{title}】在媒体库已存在季集信息：{exist_season_info}")

        # 获取媒体信息（V3 用 media_source + media_id）
        # recognize_media 内部要走 TMDB 网络，超时/不通会抛异常。
        # 这里必须兜住：扫描循环是逐部剧串行跑的，一部抛错会中断整轮扫描，
        # 表现为「日志里只检查了一部电视剧」。
        try:
            tmdbinfo = self.mediachain.recognize_media(
                mtype=mtype,
                media_source=media_source,
                media_id=str(media_id),
            )
        except Exception as err:
            logger.warning(
                f"【{title}】识别媒体信息异常（{media_source}:{media_id}）：{err}"
            )
            return False, tv_no_exist_info

        if tmdbinfo:
            logger.debug(f"【{title}】获取到媒体信息::: {tmdbinfo}")
            tv_attributes_keys = [
                "poster_path",
                "vote_average",
                "last_air_date",
            ]
            for attr in tv_attributes_keys:
                setattr(
                    tv_no_exist_info,
                    attr,
                    getattr(tmdbinfo, attr, None) or getattr(tv_no_exist_info, attr),
                )

            tmdbinfo_seasons = (tmdbinfo.seasons or {}).items()
            if not tmdbinfo_seasons:
                logger.debug(f"【{title}】未获取到季集信息, 跳过获取缺失集数")
                return False, tv_no_exist_info

            # 季集比对与订阅去重都需要 TMDB ID，只有 TMDB 来源才有季集数据
            tmdbid = self.__to_tmdbid(media_source, media_id)

            if not exist_season_info:
                logger.debug(f"【{title}】全部季不存在, 添加全部季集数")
                # 全部季不存在
                for season, _ in tmdbinfo_seasons:
                    if self._only_exist_season:
                        # 只检查已存在的季，而库里一集都没有 → 本片无需检查
                        logger.debug(
                            f"【{title}】开启了「仅检查已有季」, "
                            f"媒体库中无任何集, 跳过本片"
                        )
                        break
                    filted_episodes = self.__filter_episodes(tmdbid, season)
                    if not filted_episodes:
                        logger.debug(
                            f"【{title}】第【{season}】季未获取到TMDB集数信息, 跳过"
                        )
                        continue
                    # 该季总集数
                    episode_total = len(filted_episodes)

                    # 判断用户是否已经添加订阅
                    if self.__subscribe_exists(media_source, str(media_id), season):
                        logger.info(f"【{title}】第【{season}】季已存在订阅, 跳过")
                        continue
                    __append_season_info(
                        season=season,
                        episode_no_exist=[],
                        episode_total=episode_total,
                    )
            else:
                logger.debug(f"【{title}】检查每季缺失的集")
                # 检查每季缺失的季集
                for season, _ in tmdbinfo_seasons:
                    filted_episodes = self.__filter_episodes(tmdbid, season)
                    logger.debug(
                        f"【{title}】第【{season}】季在TMDB的集数信息: {filted_episodes}"
                    )
                    if not filted_episodes:
                        logger.debug(
                            f"【{title}】第【{season}】季未获取到TMDB集数信息, 跳过"
                        )
                        continue
                    # 该季总集数
                    episode_total = len(filted_episodes)

                    # 该季已存在的集
                    exist_episode = exist_season_info.get(season)
                    logger.debug(
                        f"【{title}】第【{season}】季在媒体库已存在的集数信息: {exist_episode}"
                    )
                    if exist_episode:
                        logger.debug(f"查找【{title}】第【{season}】季缺失集集数")
                        # 按TMDB集数查找缺失集
                        lack_episode = sorted(
                            set(filted_episodes).difference(set(exist_episode))
                        )

                        if not lack_episode:
                            logger.debug(f"【{title}】第【{season}】季全部集存在")
                            # 该季全部集存在, 不添加季集信息
                            continue

                        # 判断用户是否已经添加订阅
                        if self.__subscribe_exists(
                            media_source, str(media_id), season
                        ):
                            logger.info(f"【{title}】第【{season}】季已存在订阅, 跳过")
                            continue
                        # 添加不存在的季集信息
                        __append_season_info(
                            season=season,
                            episode_no_exist=lack_episode,
                            episode_total=episode_total,
                        )
                    else:
                        # 该季在媒体库里一集都没有
                        if self._only_exist_season:
                            # 只补漏：库里没有的季不管，避免把整部未入库的剧
                            # 也算成「缺失」并触发自动订阅
                            logger.debug(
                                f"【{title}】第【{season}】季媒体库无集，"
                                f"已开启「仅检查已有季」, 跳过该季"
                            )
                            continue
                        logger.debug(f"【{title}】第【{season}】季全集不存在")
                        # 判断用户是否已经添加订阅
                        if self.__subscribe_exists(
                            media_source, str(media_id), season
                        ):
                            logger.info(f"【{title}】第【{season}】季已存在订阅, 跳过")
                            continue
                        # 该季全集不存在
                        __append_season_info(
                            season=season,
                            episode_no_exist=[],
                            episode_total=episode_total,
                        )

            logger.debug(f"【{title}】季集信息: {tv_no_exist_info}")

            # 存在不完整的剧集
            if tv_no_exist_info.season_episode_no_exist_info:
                logger.debug("媒体库中已存在部分剧集")
                return True, tv_no_exist_info

            # 全部存在
            logger.debug(f"【{title}】所有季集均已存在/订阅")
            return True, tv_no_exist_info

        else:
            logger.debug(f"【{title}】未获取到媒体信息, 跳过获取缺失集数")
            return False, tv_no_exist_info

    @staticmethod
    def __to_tmdbid(media_source: str | None, media_id: str | None) -> int | None:
        """
        换算成 TMDB ID。

        季集清单只有 TMDB 提供；豆瓣等来源的 media_id 不是数字，无法查季集，
        返回 None 让上层跳过。
        """
        if not media_id or not str(media_id).isdigit():
            return None
        if media_source and str(media_source) != "themoviedb":
            return None
        return int(media_id)

    def __subscribe_exists(
        self, media_source: str | None, media_id: str, season: Optional[int]
    ) -> bool:
        """
        判断某季是否已存在订阅。

        V3 的 SubscribeOper.exists 签名变为 (media_source, media_id, season=...)，媒体
        身份是必填位置参数；这里做一次调用兜底，避免宿主小版本差异导致整轮检查中断。
        """
        try:
            return bool(
                self.subscribechain.subscribeoper.exists(
                    media_source, media_id, season=season
                )
            )
        except TypeError:
            # 兼容仍使用关键字签名的版本
            try:
                return bool(
                    self.subscribechain.subscribeoper.exists(
                        media_source=media_source, media_id=media_id, season=season
                    )
                )
            except Exception as err:
                logger.warning(f"检查订阅是否存在失败：{err}")
                return False
        except Exception as err:
            logger.warning(f"检查订阅是否存在失败：{err}")
            return False

    def __filter_episodes(self, tmdbid, season):
        # 电视剧某季所有集
        if not tmdbid:
            logger.debug(f"无法换算 TMDB ID, 第【{season}】季跳过获取集数信息")
            return []

        try:
            episodes_info = self.tmdb.tmdb_episodes(tmdbid=tmdbid, season=season) or []
        except Exception as err:
            logger.warning(f"获取电视剧【{tmdbid}】第【{season}】季集信息失败：{err}")
            return []

        episodes = []
        # 遍历集，筛选当前日期发布的剧集
        current_time = datetime.datetime.now(
            tz=pytz.timezone(_get_runtime_setting("TZ", "Asia/Shanghai"))
        )
        for episode in episodes_info:
            if episode and episode.air_date:
                # 将 air_date 字符串转换为 datetime 对象
                try:
                    air_date = datetime.datetime.strptime(episode.air_date, "%Y-%m-%d")
                except ValueError:
                    logger.debug(
                        f"【TMDBID: {tmdbid}】第 {season}季 {episode.name} air_date 格式异常, 跳过"
                    )
                    continue
                __episode_name = f"【TMDBID: {tmdbid}】第 {season}季 {episode.name}"
                # 比较两个日期
                if air_date.date() < current_time.date():
                    if episode.episode_number is not None:
                        episodes.append(episode.episode_number)
                else:
                    logger.debug(
                        f"{__episode_name} air_date: {episode.air_date} 发布时间比现在晚, 不添加进集统计"
                    )

        logger.debug(f"筛选后的集数::: {episodes}")

        return episodes

    def __update_config(self):
        """
        更新配置
        """
        __config = {
            "enabled": self._enabled,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "clear": self._clear,
            "history_type": self._history_type,
            "no_exist_action": self._no_exist_action,
            "only_exist_season": self._only_exist_season,
            "save_path_replaces": "\n".join(map(str, self._save_path_replaces)),
            "whitelist_librarys": ",".join(map(str, self._whitelist_librarys)),
            "whitelist_media_servers": ",".join(
                map(str, self._whitelist_media_servers)
            ),
        }
        logger.info(f"更新配置 {__config}")
        self.update_config(__config)

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止服务失败：{e}")

    @staticmethod
    def __remove_history_by_unique(historys, unique: str):

        historys["item_unique_flags"] = [
            item for item in historys["item_unique_flags"] if item != unique
        ]

        if unique in historys["details"]:
            del historys["details"][unique]
            return True, historys
        else:
            logger.warning(f"unique: {unique} 不在历史记录里")
            return False, historys

    def __checke_and_add_subscribe(
        self,
        title: str,
        year: str,
        media_source: str,
        media_id: str,
        season: int,
        save_path: str | None = None,
        total_episode: int | None = None,
    ):
        title_season = f"{title} ({year}) 第 {season} 季"
        logger.info(f"开始检查 {title_season} 是否已添加订阅")

        save_path_replaced = None
        if self._save_path_replaces and save_path:
            for _save_path_replace in self._save_path_replaces:
                replace_list = [
                    part.strip()
                    for part in _save_path_replace.split(":")
                    if part.strip()
                ]
                if len(replace_list) < 2:
                    continue
                _lib_path_str, _save_path_str = replace_list[:2]
                logger.debug(f"替换路径: {_lib_path_str} -> {_save_path_str}")
                if _lib_path_str in save_path:
                    # 媒体服务器路径恒为 POSIX 风格，不能用 pathlib.Path 取父目录：
                    # 插件在 Windows 上开发调试时会被解析成盘符相对路径，把前缀吃掉。
                    normalized = save_path.replace("\\", "/").rstrip("/")
                    head, sep, _tail = normalized.rpartition("/")
                    save_path_parent_str = head if sep else ""
                    if save_path_parent_str:
                        save_path_replaced = save_path_parent_str.replace(
                            _lib_path_str, _save_path_str
                        )
                        logger.info(
                            f"{title_season} 的下载路径替换为: {save_path_replaced}"
                        )
                        break
                    logger.debug(
                        f"替换路径失败: {_lib_path_str} 位于路径根目录, 跳过替换"
                    )

        # 判断用户是否已经添加订阅
        if self.__subscribe_exists(media_source, media_id, season):
            logger.info(f"{title_season} 订阅已存在")
            return True

        logger.info(f"开始添加订阅: {title_season}")

        if not isinstance(season, int):
            try:
                season = int(season)
            except ValueError:
                logger.warning("season 无法转换为整数")

        # 添加订阅（V3 用 media_source + media_id 传媒体身份）
        is_add_success, msg = self.subscribechain.add(
            title=title,
            year=year,
            mtype=MediaType.TV,
            season=season,
            exist_ok=True,
            username=self.plugin_name,
            media_source=media_source,
            media_id=media_id,
            save_path=save_path_replaced,
            total_episode=total_episode,
        )
        logger.debug(f"添加订阅 {title_season} 结果: {is_add_success}, {msg}")
        if not is_add_success:
            logger.warning(f"添加订阅 {title_season} 失败: {msg}")
            return False
        logger.info(f"已添加订阅: {title_season}")
        return True

    @staticmethod
    def __update_exist_status_by_unique(historys, unique: str, new_status: str):
        if unique in historys["details"]:
            historys["details"][unique]["exist_status"] = new_status
            logger.info(f"更新检查记录 {unique} 状态为: {new_status}")
            return True, historys
        else:
            logger.warning(f"unique: {unique} 不在历史记录里")
            return False, historys

    def __add_subscribe_by_tv_no_exist_info(
        self, tv_no_exist_info: TvNoExistInfo | Dict[str, Any], unique: str
    ):
        if tv_no_exist_info and isinstance(tv_no_exist_info, TvNoExistInfo):
            tv_no_exist_info = tv_no_exist_info.model_dump()

        title = tv_no_exist_info.get("title")
        year = tv_no_exist_info.get("year")
        media_source = tv_no_exist_info.get("media_source") or "themoviedb"
        media_id = tv_no_exist_info.get("media_id")
        save_path = tv_no_exist_info.get("path")

        season_episode_no_exist_info = tv_no_exist_info.get(
            "season_episode_no_exist_info"
        )

        if not title or not year or not media_id or not season_episode_no_exist_info:
            logger.warning(f"unique: {unique} 季集信息不完整, 跳过订阅")
            return False

        season_keys = list(season_episode_no_exist_info.keys())

        for season in season_keys:
            total_episode = None
            # 尝试直接获取值
            season_info = season_episode_no_exist_info.get(season)

            if season_info is None:
                # 尝试转换类型后再次获取
                if isinstance(season, int):
                    # season 是数字，尝试转为字符串
                    season_info = season_episode_no_exist_info.get(str(season))
                elif isinstance(season, str):
                    # season 是字符串，尝试转为数字
                    try:
                        season_info = season_episode_no_exist_info.get(int(season))
                    except ValueError:
                        # season 无法转换为数字
                        season_info = None
                        logger.debug("无法获取季集信息")
            if season_info:
                total_episode = season_info.get("episode_total")
                episode_no_exist = season_info.get("episode_no_exist")
                if not episode_no_exist:
                    if self._history_type == HistoryDataType.NOT_ALL_NO_EXIST:
                        logger.info(
                            f"【{title}】第 {season} 季所有集均缺失, 历史数据类型为 {HistoryDataType.NOT_ALL_NO_EXIST}, 跳过订阅。如果需要订阅缺失集数，请将历史数据类型更改为其它类型"
                        )
                        continue
                    else:
                        logger.info(
                            f"【{title}】第 {season} 季所有集均缺失, 历史数据类型为 {self._history_type}, 将添加订阅"
                        )

                else:
                    logger.info(
                        f"【{title}】第 {season} 季缺失集数: {episode_no_exist}, 将添加订阅"
                    )

            if not isinstance(season, int):
                try:
                    season = int(season)
                except ValueError:
                    logger.warning("season 无法转换为整数")
                    return False

            is_add_subscribe_success = self.__checke_and_add_subscribe(
                title=title,
                year=str(year),
                media_source=media_source,
                media_id=str(media_id),
                season=season,
                save_path=save_path,
                total_episode=total_episode,
            )
            if not is_add_subscribe_success:
                return False

        return True

    def __add_subscribe_by_unique(self, historys, unique: str):

        if unique in historys["details"]:
            tv_no_exist_info = historys["details"][unique]["tv_no_exist_info"]
            is_add_subscribe_success = self.__add_subscribe_by_tv_no_exist_info(
                tv_no_exist_info, unique
            )
            if is_add_subscribe_success:
                is_update_exist_status_success, historys = (
                    self.__update_exist_status_by_unique(
                        historys=historys,
                        unique=unique,
                        new_status=HistoryStatus.ADDED_RSS.value,
                    )
                )
                return is_update_exist_status_success, historys
            else:
                return False, historys

        else:
            logger.warning(f"unique: {unique} 不在历史记录里")
            return False, historys

    def __verify_apikey(self, apikey: str) -> Optional[schemas.Response]:
        """校验 API 密钥，错误时返回失败响应。"""
        if apikey != _get_runtime_setting("API_TOKEN"):
            logger.warning("API密钥错误")
            return schemas.Response(success=False, message="API密钥错误")
        return None

    def delete_history(self, key: str, apikey: str):
        """
        删除同步检查记录
        """
        logger.info(f"开始删除检查记录: {key}")
        error = self.__verify_apikey(apikey)
        if error is not None:
            return error
        # 检查记录
        historys = self.get_data("history")
        if not historys:
            logger.warning("未找到检查记录")
            return schemas.Response(success=False, message="未找到检查记录")

        is_success, historys = EpisodeNoExistV3.__remove_history_by_unique(
            historys, key
        )

        if is_success:
            logger.info(f"删除检查记录 {key} 成功")
            self.save_data("history", historys)
            return schemas.Response(success=True, message="删除成功")
        else:
            logger.warning(f"删除检查记录 {key} 失败")
            return schemas.Response(success=False, message="删除失败")

    def add_subscribe_history(self, key: str, apikey: str):
        """
        订阅缺失检查记录
        """
        logger.info(f"开始订阅检查记录: {key}")
        error = self.__verify_apikey(apikey)
        if error is not None:
            return error
        # 检查记录
        historys = self.get_data("history")
        if not historys:
            logger.warning("未找到检查记录")
            return schemas.Response(success=False, message="未找到检查记录")

        is_success, historys = self.__add_subscribe_by_unique(historys, key)
        if is_success:
            logger.info(f"添加 {key} 订阅成功")
            self.save_data("history", historys)
            return schemas.Response(success=True, message="订阅成功")
        else:
            logger.warning(f"添加 {key} 订阅失败")
            return schemas.Response(success=False, message="订阅失败")

    def set_all_exist_history(self, key: str, apikey: str):
        """
        标记存在检查记录
        """
        logger.info(f"开始标记存在检查记录: {key}")
        error = self.__verify_apikey(apikey)
        if error is not None:
            return error
        # 检查记录
        historys = self.get_data("history")
        if not historys:
            logger.warning("未找到检查记录")
            return schemas.Response(success=False, message="未找到检查记录")

        is_success, historys = EpisodeNoExistV3.__update_exist_status_by_unique(
            historys, key, HistoryStatus.ALL_EXIST.value
        )
        if is_success:
            logger.info(f"标记存在 {key} 成功")
            self.save_data("history", historys)
            return schemas.Response(success=True, message="标记存在成功")
        else:
            logger.warning(f"标记存在 {key} 失败")
            return schemas.Response(success=False, message="标记存在失败")

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clear",
                                            "label": "清理检查记录",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "5位cron表达式, 留空自动",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "history_type",
                                            "label": "历史数据类型",
                                            "items": [
                                                {
                                                    "title": f"{HistoryDataType.LATEST.value}",
                                                    "value": f"{HistoryDataType.LATEST.value}",
                                                },
                                                {
                                                    "title": f"{HistoryDataType.NO_EXIST.value}",
                                                    "value": f"{HistoryDataType.NO_EXIST.value}",
                                                },
                                                {
                                                    "title": f"{HistoryDataType.NOT_ALL_NO_EXIST.value}",
                                                    "value": f"{HistoryDataType.NOT_ALL_NO_EXIST.value}",
                                                },
                                                {
                                                    "title": f"{HistoryDataType.ALL_EXIST.value}",
                                                    "value": f"{HistoryDataType.ALL_EXIST.value}",
                                                },
                                                {
                                                    "title": f"{HistoryDataType.ADDED_RSS.value}",
                                                    "value": f"{HistoryDataType.ADDED_RSS.value}",
                                                },
                                                {
                                                    "title": f"{HistoryDataType.FAILED.value}",
                                                    "value": f"{HistoryDataType.FAILED.value}",
                                                },
                                                {
                                                    "title": f"{HistoryDataType.ALL.value}",
                                                    "value": f"{HistoryDataType.ALL.value}",
                                                },
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "no_exist_action",
                                            "label": "缺失处理方式",
                                            "items": [
                                                {
                                                    "title": f"{NoExistAction.ONLY_HISTORY.value}",
                                                    "value": f"{NoExistAction.ONLY_HISTORY.value}",
                                                },
                                                {
                                                    "title": f"{NoExistAction.ADD_SUBSCRIBE.value}",
                                                    "value": f"{NoExistAction.ADD_SUBSCRIBE.value}",
                                                },
                                                {
                                                    "title": f"{NoExistAction.SET_ALL_EXIST.value}",
                                                    "value": f"{NoExistAction.SET_ALL_EXIST.value}",
                                                },
                                            ],
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
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "only_exist_season",
                                            "label": "仅检查已有季（媒体库里完全没有的季不检查、不自动订阅，只补已有季里的漏集）",
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
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "whitelist_media_servers",
                                            "label": "媒体服务器白名单",
                                            "placeholder": "留空默认全部",
                                            "items": self.__mediaserver_options(),
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
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "whitelist_librarys",
                                            "label": "电视剧媒体库白名单",
                                            "placeholder": "*必填, 多个名称用英文逗号分隔",
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
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "save_path_replaces",
                                            "label": "下载路径替换, 一行一个",
                                            "placeholder": "将媒体库电视剧的路径替换为下载路径, 用英文冒号作为分割。不输入则按默认下载路径处理。\n例如将'/media/library/tv/上载新生 (2020)'的下载路径设置为'/downloads/tv', 则输入 /media/library:/downloads",
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
            "cron": "",
            "onlyonce": False,
            "clear": False,
            "history_type": HistoryDataType.LATEST.value,
            "save_path_replaces": "",
            "no_exist_action": NoExistAction.ONLY_HISTORY.value,
            "only_exist_season": False,
            "whitelist_media_servers": [],
            "whitelist_librarys": "",
        }

    def __mediaserver_options(self) -> List[Dict[str, Any]]:
        """给配置表单用的媒体服务器候选项，取不到时返回空列表由用户手填。"""
        options: List[Dict[str, Any]] = []
        try:
            configs = MediaServerHelper().get_configs() or {}
        except Exception as err:
            logger.debug(f"获取媒体服务器配置失败：{err}")
            return options
        for config in configs.values():
            name = getattr(config, "name", None)
            if name:
                options.append({"title": str(name), "value": str(name)})
        return options

    def __get_action_buttons_content(self, unique: str | None, status: str):
        if not unique:
            return []
        apikey = _get_runtime_setting("API_TOKEN")
        action_buttons = {
            "add_subscribe_history": {
                "component": "VBtn",
                "props": {
                    "class": "text-primary flex-grow",
                    "variant": "tonal",
                    "style": "height: 100%",
                },
                "events": {
                    "click": {
                        "api": "plugin/EpisodeNoExistV3/add_subscribe_history",
                        "method": "get",
                        "params": {
                            "key": f"{unique}",
                            "apikey": apikey,
                        },
                    }
                },
                "text": "订阅缺失",
            },
            "set_all_exist_history": {
                "component": "VBtn",
                "props": {
                    "class": "text-success flex-grow",
                    "style": "height: 100%",
                    "variant": "tonal",
                },
                "events": {
                    "click": {
                        "api": "plugin/EpisodeNoExistV3/set_all_exist_history",
                        "method": "get",
                        "params": {
                            "key": f"{unique}",
                            "apikey": apikey,
                        },
                    }
                },
                "text": "标记存在",
            },
            "delete_history": {
                "component": "VBtn",
                "props": {
                    "class": "text-error flex-grow",
                    "variant": "tonal",
                    "style": "height: 100%",
                },
                "events": {
                    "click": {
                        "api": "plugin/EpisodeNoExistV3/delete_history",
                        "method": "get",
                        "params": {
                            "key": f"{unique}",
                            "apikey": apikey,
                        },
                    }
                },
                "text": "删除记录",
            },
        }

        action_names = {
            HistoryStatus.NO_EXIST.value: [
                "delete_history",
                "set_all_exist_history",
                "add_subscribe_history",
            ],
            HistoryStatus.ADDED_RSS.value: [
                "delete_history",
                "set_all_exist_history",
            ],
        }.get(status, ["delete_history"])

        action_buttons = [action_buttons.get(name) for name in action_names]

        return action_buttons

    def __get_history_post_content(
        self, history: ExtendedHistoryDetail | dict[Any, Any]
    ):
        def __count_seasons_episodes(seasons_episodes_info: Dict[int, Dict[str, Any]]):
            seasons_episodes_info = seasons_episodes_info or {}
            seasons_count = len(seasons_episodes_info.keys())
            episodes_count = 0
            for season in seasons_episodes_info.values():
                if season.get("episode_no_exist"):
                    episodes_count += len(season["episode_no_exist"])
                else:
                    episodes_count += season.get("episode_total") or 0
            return seasons_count, episodes_count

        history = history or {}
        time_str = history.get("last_update")
        tv_no_exist_info: TvNoExistInfo = history.get("tv_no_exist_info") or {}

        title = tv_no_exist_info.get("title", "未知").replace(" ", "")
        title = title[:8] + "..." if len(title) > 8 else title
        year = tv_no_exist_info.get("year")
        media_id = tv_no_exist_info.get("media_id")
        media_source = tv_no_exist_info.get("media_source") or "themoviedb"
        poster = tv_no_exist_info.get("poster_path")
        vote = tv_no_exist_info.get("vote_average")
        last_air_date = tv_no_exist_info.get("last_air_date")
        season_episode_no_exist_info = (
            tv_no_exist_info.get("season_episode_no_exist_info") or {}
        )
        season_no_exist_count, episode_no_exist_count = __count_seasons_episodes(
            season_episode_no_exist_info
        )

        _status = history.get("exist_status") or HistoryStatus.UNKNOW.value
        status = _status
        if status == HistoryStatus.NO_EXIST.value:
            status = f"缺失{season_no_exist_count}季, {episode_no_exist_count}集"

        # V3 用媒体身份拼媒体详情页链接
        link = f"#/media?mediaid={media_source}:{media_id}&type={MediaType.TV.value}"
        try:
            mp_domain = settings.MP_DOMAIN()
            if mp_domain:
                link = f"{mp_domain}/{link}"
        except Exception as err:
            logger.debug(f"获取 MP_DOMAIN 失败：{err}")

        unique = history.get("unique")

        if media_id:
            href = f"{link}"
        else:
            href = "#"

        action_buttons_content = self.__get_action_buttons_content(
            unique,
            _status,
        )

        component = {
            "component": "VCard",
            "props": {
                "variant": "tonal",
                "props": {"class": ""},
            },
            "content": [
                {
                    "component": "div",
                    "props": {"class": "flex flex-row"},
                    "content": [
                        {
                            "component": "VImg",
                            "props": {
                                "src": poster,
                                "height": 240,
                                "width": 160,
                                "aspect-ratio": "2/3",
                                "class": "object-cover shadow ring-gray-500 max-w-32",
                                "cover": True,
                                "transition": True,
                                "lazy-src": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAPAAAACgCAQAAACY0inuAAABB0lEQVR42u3RMREAAAjEMF45M65xwcClEppMlx4XwIAFWIAFWIABC7AAC7AAC7AAAxZgARZgARZgARZgwAIswAIswAIswIAFWIAFWIABC7AAC7AAC7AACzBgARZgARZgARZgwAIswAIswAIswAABAxZgARZgARZgAQYswAIswAIswAIMWIAFWIAFWIAFWIABC7AAC7AAC7AAAxZgARZgARZgAQYswAIswAIswAIswAIswAAWIBAP//BwAAqOJUAAAAABJRU5ErkJggg==",
                            },
                        },
                        {
                            "component": "div",
                            "props": {"class": ""},
                            "content": [
                                {
                                    "component": "VCardTitle",
                                    "props": {
                                        "class": "pt-6 pl-4 pr-4 text-lg whitespace-nowrap",
                                        "style": "width: 12rem",
                                    },
                                    "content": [
                                        {
                                            "component": "a",
                                            "props": {
                                                "href": f"{href}",
                                                "target": "_blank",
                                            },
                                            "text": title,
                                        }
                                    ],
                                },
                                {
                                    "component": "VCardText",
                                    "props": {
                                        "class": "pa-0 pl-4 pr-4 pb-1 whitespace-nowrap"
                                    },
                                    "text": f"状态: {status}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {
                                        "class": "pa-0 pl-4 pr-4 py-1 whitespace-nowrap"
                                    },
                                    "text": f"年份: {year}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {
                                        "class": "pa-0 pl-4 pr-4 py-1 whitespace-nowrap"
                                    },
                                    "text": f"评分: {vote}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {
                                        "class": "pa-0 pl-4 pr-4 py-1 whitespace-nowrap"
                                    },
                                    "text": f"检查: {time_str}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {
                                        "class": "pa-0 pl-4 pr-4 py-1 whitespace-nowrap"
                                    },
                                    "text": f"最后: {last_air_date}",
                                },
                            ],
                        },
                    ],
                },
                {
                    "component": "VBtnToggle",
                    "props": {
                        "class": "bg-opacity-80 flex flex-row-reverse justify-between items-center flex-nowrap space-x-reverse space-x-4",
                        "variant": "tonal",
                        "rounded": "0",
                    },
                    "content": action_buttons_content,
                },
            ],
        }

        return component

    def __get_historys_posts_content(
        self, historys: List[ExtendedHistoryDetail] | None
    ):

        posts_content = []
        if not historys:
            posts_content = [
                {
                    "component": "div",
                    "text": "暂无数据",
                    "props": {
                        "class": "text-start",
                    },
                }
            ]
        else:
            for history in historys:
                posts_content.append(self.__get_history_post_content(history))

        component = {
            "component": "div",
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {
                        "class": "pt-8 pb-2 px-0 text-base whitespace-nowrap text-center",
                    },
                    "content": [
                        {
                            "component": "span",
                            "text": f"··· {self._history_type} ···",
                        }
                    ],
                },
                {
                    "component": "div",
                    "props": {
                        "class": "flex flex-row flex-wrap gap-4 items-center justify-center",
                    },
                    "content": posts_content,
                },
            ],
        }

        return component

    @staticmethod
    def __get_svg_content(color: str, ds: List[str]):
        def __get_path_content(fill: str, d: str) -> dict[str, Any]:
            return {
                "component": "path",
                "props": {"fill": fill, "d": d},
            }

        path_content = [__get_path_content(color, d) for d in ds]
        component = {
            "component": "svg",
            "props": {
                "class": "icon",
                "viewBox": "0 0 1024 1024",
                "width": "40",
                "height": "40",
            },
            "content": path_content,
        }
        return component

    @staticmethod
    def __get_icon_content():
        color = "#8a8a8a"
        icon_content = {
            Icons.TARGET: EpisodeNoExistV3.__get_svg_content(
                color,
                [
                    "M512 307.2c-114.688 0-204.8 90.112-204.8 204.8 0 110.592 90.112 204.8 204.8 204.8s204.8-90.112 204.8-204.8-90.112-204.8-204.8-204.8z",
                    "M962.56 471.04H942.08c-20.48-204.8-184.32-372.736-389.12-389.12v-20.48c0-24.576-16.384-40.96-40.96-40.96s-40.96 16.384-40.96 40.96v16.384c-204.8 20.48-372.736 184.32-389.12 393.216h-20.48c-24.576 0-40.96 16.384-40.96 40.96s16.384 40.96 40.96 40.96h16.384c20.48 204.8 184.32 372.736 393.216 393.216v16.384c0 24.576 16.384 40.96 40.96 40.96s40.96-16.384 40.96-40.96V942.08c204.8-20.48 372.736-184.32 393.216-389.12h16.384c24.576 0 40.96-16.384 40.96-40.96s-16.384-40.96-40.96-40.96z m-409.6 389.12v-24.576c0-24.576-16.384-40.96-40.96-40.96s-40.96 16.384-40.96 40.96v24.576c-159.744-20.48-290.816-147.456-307.2-307.2h24.576c24.576 0 40.96-16.384 40.96-40.96s-16.384-40.96-40.96-40.96H163.84c16.384-159.744 147.456-290.816 307.2-307.2v-24.576c0-24.576-16.384-40.96-40.96-40.96s-40.96 16.384-40.96 40.96v24.576c0 24.576 16.384 40.96 40.96 40.96s40.96-16.384 40.96-40.96V163.84c159.744 20.48 290.816 147.456 307.2 307.2h-24.576c-24.576 0-40.96-16.384-40.96-40.96s16.384-40.96 40.96-40.96h24.576c-16.384 159.744-147.456 290.816-307.2 307.2z",
                ],
            ),
            Icons.ADD_SCHEDULE: EpisodeNoExistV3.__get_svg_content(
                color,
                [
                    "M611.157333 583.509333h-63.146666v-63.146666c0-20.138667-16.042667-36.181333-35.84-36.181334-20.138667 0-35.84 16.042667-35.84 35.84v63.146667h-63.146667c-19.797333 0-36.181333 16.384-36.181333 36.181333 0.7168 21.128533 16.759467 35.498667 36.181333 36.181334h63.146667v62.805333c0 20.923733 16.759467 35.84 35.84 35.84 19.797333 0 35.84-16.042667 35.84-35.84v-63.146667h63.146666a35.84 35.84 0 1 0 0-71.68z",
                    "M839.338667 145.749333h-13.653334v86.016c0 56.32-45.738667 102.4-102.4 102.4-56.32 0-102.4-46.08-102.4-102.4V145.749333h-217.770666v86.016c0 56.32-46.08 102.4-102.4 102.4-56.661333 0-102.4-46.08-102.4-102.4V145.749333h-13.653334C120.490667 145.749333 68.266667 197.973333 68.266667 262.144v551.594667c0 64.170667 52.224 116.394667 116.394666 116.394666h654.677334c64.170667 0 116.394667-52.224 116.394666-116.394666V262.144c0-64.170667-52.224-116.394667-116.394666-116.394667z m0 716.117334H184.661333c-26.624 0-48.128-21.504-48.128-48.128V402.773333h750.933334v410.965334c0 26.624-21.504 48.128-48.128 48.128z",
                    "M300.612267 265.796267a34.133333 34.133333 0 0 0 34.133333-34.133334V128a34.133333 34.133333 0 1 0-68.266667 0v103.6288a34.133333 34.133333 0 0 0 34.133334 34.133333zM723.3536 265.796267a34.133333 34.133333 0 0 0 34.133333-34.133334V128a34.133333 34.133333 0 1 0-68.266666 0v103.6288a34.133333 34.133333 0 0 0 34.133333 34.133333z",
                ],
            ),
            Icons.BUG_REMOVE: EpisodeNoExistV3.__get_svg_content(
                color,
                [
                    "M512 640a128 128 0 1 0 0-256 128 128 0 0 0 0 256z m0-192a64 64 0 1 1 0 128 64 64 0 0 1 0-128z",
                    "M512 96a416 416 0 1 0 0 832 416 416 0 0 0 0-832z m0 768a352 352 0 1 1 0-704 352 352 0 0 1 0 704z",
                ],
            ),
            Icons.WARNING: EpisodeNoExistV3.__get_svg_content(
                color,
                [
                    "M965.316923 727.276308l-319.015385-578.953846c-58.171077-106.299077-210.944-106.023385-268.996923 0l-318.621538 579.347692c-56.359385 102.636308 18.116923 227.643077 134.695385 227.643077h637.243076c116.184615 0 191.172923-124.416 134.695385-228.036923z m-453.316923 26.781538c-24.812308 0-44.504615-20.086154-44.504615-44.504615 0-24.812308 19.692308-44.898462 44.504615-44.898462a44.701538 44.701538 0 0 1 0 89.403077z m57.501538-361.156923l-20.873846 170.929231c-1.575385 19.298462-17.329231 33.870769-36.627692 33.870769s-35.446154-14.572308-37.021538-33.870769l-20.48-170.929231c-3.150769-33.870769 23.630769-63.015385 57.501538-63.015385 29.932308 0 57.501538 21.582769 57.501538 63.015385z"
                ],
            ),
            Icons.GLASSES: EpisodeNoExistV3.__get_svg_content(
                color,
                [
                    "M1028.096 503.808L815.104 204.8c-8.192-12.288-20.48-16.384-32.768-16.384h-126.976c-24.576 0-40.96 20.48-40.96 40.96 0 24.576 20.48 40.96 40.96 40.96h102.4l131.072 184.32H143.36l135.168-188.416h102.4c24.576 0 40.96-16.384 40.96-40.96s-16.384-40.96-40.96-40.96H253.952c-16.384 0-24.576 8.192-32.768 16.384L8.192 499.712c0 8.192-8.192 32.768-8.192 53.248v188.416c0 53.248 45.056 94.208 98.304 94.208h266.24c53.248 0 94.208-40.96 94.208-94.208v-188.416-12.288h122.88V741.376c0 53.248 40.96 94.208 98.304 94.208h266.24c53.248 0 94.208-40.96 94.208-94.208v-188.416c0-16.384-8.192-40.96-12.288-49.152zM376.832 716.8c0 20.48-16.384 40.96-40.96 40.96H122.88c-20.48 0-40.96-20.48-40.96-40.96v-135.168c0-24.576 20.48-40.96 40.96-40.96H335.872c24.576 0 40.96 16.384 40.96 40.96v135.168z m581.632 0c0 20.48-16.384 40.96-40.96 40.96H703.552c-24.576 0-40.96-16.384-40.96-40.96v-135.168c0-24.576 16.384-40.96 40.96-40.96h213.888c20.48 0 40.96 16.384 40.96 40.96v135.168z",
                ],
            ),
            Icons.STATISTICS: EpisodeNoExistV3.__get_svg_content(
                color,
                [
                    "M471.04 270.336V20.48c-249.856 20.48-450.56 233.472-450.56 491.52 0 274.432 225.28 491.52 491.52 491.52 118.784 0 229.376-40.96 315.392-114.688L655.36 708.608c-40.96 28.672-94.208 45.056-139.264 45.056-135.168 0-245.76-106.496-245.76-245.76 0-114.688 81.92-217.088 200.704-237.568z",
                    "M552.96 20.48v249.856C655.36 286.72 737.28 368.64 753.664 471.04h249.856C983.04 233.472 790.528 40.96 552.96 20.48zM712.704 651.264l176.128 176.128c65.536-77.824 106.496-172.032 114.688-274.432h-249.856c-8.192 36.864-20.48 69.632-40.96 98.304z",
                ],
            ),
        }
        return icon_content

    @staticmethod
    def __get_historys_statistic_content(
        title: str, value: str, icon_name: Icons
    ) -> dict[str, Any]:
        icon_content = EpisodeNoExistV3.__get_icon_content().get(icon_name, "")
        total_elements = {
            "component": "VCard",
            "props": {
                "variant": "tonal",
                "style": "width: 10rem;",
            },
            "content": [
                {
                    "component": "VCardText",
                    "props": {
                        "class": "d-flex align-center",
                    },
                    "content": [
                        icon_content,
                        {
                            "component": "div",
                            "props": {
                                "class": "ml-2",
                            },
                            "content": [
                                {
                                    "component": "span",
                                    "props": {"class": "text-caption"},
                                    "text": f"{title}",
                                },
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "d-flex align-center flex-wrap"
                                    },
                                    "content": [
                                        {
                                            "component": "span",
                                            "props": {"class": "text-h6"},
                                            "text": f"{value}",
                                        }
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
        }
        return total_elements

    def __get_historys_statistics_content(
        self,
        historys_total,
        historys_no_exist_total,
        historys_fail_total,
        historys_all_exist_total,
        historys_added_rss_total,
        history_not_all_no_exist_total,
    ):

        # 数据统计
        data_statistics = [
            {
                "title": "总处理",
                "value": f"{historys_total}部",
                "icon_name": Icons.STATISTICS,
            },
            {
                "title": "存在缺失",
                "value": f"{historys_no_exist_total}部",
                "icon_name": Icons.WARNING,
            },
            {
                "title": "非全集缺失",
                "value": f"{history_not_all_no_exist_total}部",
                "icon_name": Icons.TARGET,
            },
            {
                "title": "未识别",
                "value": f"{historys_fail_total}部",
                "icon_name": Icons.BUG_REMOVE,
            },
            {
                "title": "全部存在",
                "value": f"{historys_all_exist_total}部",
                "icon_name": Icons.GLASSES,
            },
            {
                "title": "已订阅",
                "value": f"{historys_added_rss_total}部",
                "icon_name": Icons.ADD_SCHEDULE,
            },
        ]

        content = list(
            map(
                lambda s: EpisodeNoExistV3.__get_historys_statistic_content(
                    title=s["title"],
                    value=s["value"],
                    icon_name=s["icon_name"],
                ),
                data_statistics,
            )
        )

        component = {
            "component": "VRow",
            "props": {"class": "flex flex-row justify-center flex-wrap gap-6"},
            "content": content,
        }
        return component

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面, 需要返回页面配置, 同时附带数据
        """

        # 查询检查记录
        historys = self.get_data("history")

        if not historys:
            return [
                {
                    "component": "div",
                    "text": "暂无数据",
                    "props": {
                        "class": "text-center",
                    },
                }
            ]

        details = historys.get("details", {})

        def sort_history(history_list):
            history_list.sort(key=lambda x: x.get("last_update_full") or "", reverse=True)

        (
            history_failed,
            history_all_exist,
            history_added_rss,
            history_no_exist,
            history_all,
        ) = (
            [],
            [],
            [],
            [],
            [],
        )

        # 字典将exist_status映射到相应的列表
        status_to_list = {
            HistoryStatus.FAILED.value: history_failed,
            HistoryStatus.ADDED_RSS.value: history_added_rss,
            HistoryStatus.ALL_EXIST.value: history_all_exist,
            HistoryStatus.NO_EXIST.value: history_no_exist,
        }

        for key, item in details.items():
            item_with_key = item.copy()
            item_with_key["unique"] = key
            history_all.append(item_with_key)

            # 根据exist_status分类项目
            target_list = status_to_list.get(item.get("exist_status"))
            if target_list is not None:
                target_list.append(item_with_key)

        # 对所有列表排序
        sort_history(history_all)
        sort_history(history_failed)
        sort_history(history_all_exist)
        sort_history(history_added_rss)
        sort_history(history_no_exist)

        # 根据_history_type确定使用的列表
        history_type_to_list = {
            HistoryDataType.FAILED.value: history_failed,
            HistoryDataType.ALL_EXIST.value: history_all_exist,
            HistoryDataType.NO_EXIST.value: history_no_exist,
            HistoryDataType.ALL.value: history_all,
        }

        history_not_all_no_exist = [
            history
            for history in history_no_exist
            if any(
                season_info.get("episode_no_exist")
                for season_info in (history.get("tv_no_exist_info") or {})
                .get("season_episode_no_exist_info", {})
                .values()
            )
        ]

        if self._history_type == HistoryDataType.NOT_ALL_NO_EXIST.value:
            historys_in_type = history_not_all_no_exist
        else:
            historys_in_type = history_type_to_list.get(
                self._history_type, history_all[:6]
            )

        historys_posts_content = self.__get_historys_posts_content(historys_in_type)

        # 统计数据
        item_unique_flags = historys.get("item_unique_flags", [])
        historys_total = len(item_unique_flags)
        historys_no_exist_total = len(history_no_exist)
        historys_fail_total = len(history_failed)
        historys_added_rss_total = len(history_added_rss)
        historys_all_exist_total = len(history_all_exist)
        history_not_all_no_exist_total = len(history_not_all_no_exist)
        historys_statistics_content = self.__get_historys_statistics_content(
            historys_total=historys_total,
            historys_no_exist_total=historys_no_exist_total,
            historys_fail_total=historys_fail_total,
            historys_all_exist_total=historys_all_exist_total,
            historys_added_rss_total=historys_added_rss_total,
            history_not_all_no_exist_total=history_not_all_no_exist_total,
        )

        # 拼装页面
        return [
            {
                "component": "div",
                "content": [
                    historys_statistics_content,
                    historys_posts_content,
                ],
            },
        ]