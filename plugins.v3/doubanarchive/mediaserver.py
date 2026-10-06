"""媒体服务器读取：只读取查看进度和已有标识，不修改媒体库数据。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.sdk.logging import logger
from app.sdk.services import MediaServerHelper

# 支持的媒体服务器类型
_SUPPORTED_TYPES = ("emby", "jellyfin")


class MediaServerReader:
    """
    按 webhook 事件定位媒体服务器实例，读取条目元信息。

    读取能力包括：条目的 ProviderIds（取豆瓣 ID）、所属剧集 ID、某季已收录
    集数、以及可用于前端展示的图片地址。读取失败统一返回空值，由调用方降级。
    """

    def __init__(self, server_name: Optional[str] = None, timeout: int = 15) -> None:
        """
        :param server_name: 媒体服务器名称，为空时取第一个可用实例
        :param timeout: 预留的超时设置，实例自身已有默认超时
        """
        self._server_name = server_name
        self._timeout = timeout
        self._cache: Dict[Tuple[str, str], Any] = {}

    # ---------------- 对外能力 ----------------

    def available(self) -> bool:
        """判断是否存在可用的媒体服务器实例。"""
        return self._locate()[0] is not None

    def get_item(self, item_id: str) -> Dict[str, Any]:
        """读取条目详情字典，失败返回空字典。"""
        if not item_id:
            return {}
        instance, server_type = self._locate()
        if not instance:
            return {}
        cache_key = (server_type, item_id)
        if cache_key in self._cache:
            return self._cache[cache_key]

        response = self._get(instance, f"[HOST]emby/Users/[USER]/Items/{item_id}?fields=ProviderIds&api_key=[APIKEY]")
        data = response if isinstance(response, dict) else {}
        self._cache[cache_key] = data
        return data

    def get_douban_id(self, item_id: str) -> Optional[str]:
        """
        读取条目刮削的豆瓣 ID。
        单集通常没有豆瓣 ID，此时回退到所属剧集（Series）级再取一次。
        """
        item = self.get_item(item_id)
        douban_id = self._pick_douban(item.get("ProviderIds"))
        if douban_id:
            return douban_id

        series_id = item.get("SeriesId")
        if series_id:
            series_item = self.get_item(str(series_id))
            douban_id = self._pick_douban(series_item.get("ProviderIds"))
            if douban_id:
                return douban_id
        return None

    def get_season_total(self, item_id: str, season_no: int) -> Optional[int]:
        """
        读取某一季已收录的集数，用于判断是否看到本季最后一集。
        取不到返回 None，由调用方决定降级行为。
        """
        item = self.get_item(item_id)
        series_id = str(item.get("SeriesId") or "")
        if not series_id and item.get("Type") == "Series":
            series_id = str(item.get("Id") or item_id)
        if not series_id:
            return None

        instance, server_type = self._locate()
        if not instance:
            return None

        cache_key = (server_type, f"{series_id}-season")
        seasons = self._cache.get(cache_key)
        if seasons is None:
            response = self._get(instance, f"[HOST]emby/Shows/{series_id}/Seasons?api_key=[APIKEY]")
            seasons = response.get("Items", []) if isinstance(response, dict) else []
            self._cache[cache_key] = seasons

        for season in seasons:
            if not isinstance(season, dict):
                continue
            if self._to_int(season.get("IndexNumber")) == season_no:
                total = self._to_int(season.get("ChildCount"))
                if total:
                    return total
        return None

    def get_image_url(self, item_id: str) -> Optional[str]:
        """
        拼接条目的主视觉图片地址，供仪表盘展示。
        地址来自媒体服务器自身，取不到时返回 None。
        """
        if not item_id:
            return None
        instance, _ = self._locate()
        if not instance:
            return None
        host = None
        for attr in ("_server_url", "_play_or_host", "_host", "_url"):
            value = getattr(instance, attr, None)
            if isinstance(value, str) and value.startswith("http"):
                host = value.rstrip("/")
                break
        api_key = getattr(instance, "_apikey", None)
        if not host or not api_key:
            return None
        return f"{host}/emby/Items/{item_id}/Images/Primary?api_key={api_key}"

    # ---------------- 内部实现 ----------------

    def _locate(self) -> Tuple[Optional[Any], str]:
        """定位可用的媒体服务器实例，返回 (实例, 类型)。"""
        if getattr(self, "_located", None) is not None:
            return (self._located, getattr(self, "_located_type", ""))
        result: Tuple[Optional[Any], str] = (None, "")
        fallback: Tuple[Optional[Any], str] = (None, "")
        try:
            for service in MediaServerHelper().iterate_module_instances():
                server_type = (service.type or "").lower()
                if server_type not in _SUPPORTED_TYPES or not service.instance:
                    continue
                if self._server_name and service.name == self._server_name:
                    result = (service.instance, server_type)
                    break
                if not fallback[0]:
                    fallback = (service.instance, server_type)
        except Exception as error:
            logger.debug(f"枚举媒体服务器实例失败：{error}")
            return (None, "")

        self._located = result[0] if result[0] else fallback[0]
        self._located_type = result[1] if result[0] else fallback[1]
        return (self._located, self._located_type)

    def _get(self, instance: Any, url: str) -> Optional[Dict[str, Any]]:
        """调用媒体服务器接口并解析 JSON，异常统一降级。"""
        try:
            response = instance.get_data(url)
        except Exception as error:
            logger.warn(f"访问媒体服务器接口失败：{error}")
            return None
        if response is None or getattr(response, "status_code", None) != 200:
            logger.debug(f"媒体服务器接口返回异常状态：{getattr(response, 'status_code', None)}")
            return None
        try:
            data = response.json()
        except Exception:
            logger.warn("媒体服务器响应解析失败")
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _pick_douban(provider_ids: Optional[Dict[str, Any]]) -> Optional[str]:
        """从 ProviderIds 中提取豆瓣 ID。"""
        if not isinstance(provider_ids, dict):
            return None
        for key, value in provider_ids.items():
            if key.lower() == "douban" and value:
                return str(value).strip()
        return None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        """把接口返回值安全转换为整数。"""
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None
