"""媒体服务器读取：只读取查看进度和已有标识，不修改媒体库数据。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

from app.sdk.logging import logger
from app.sdk.services import MediaServerHelper

# 支持的媒体服务器类型
_SUPPORTED_TYPES = ("emby", "jellyfin")


class MediaServerReader:
    """
    按 webhook 事件定位媒体服务器实例，读取条目元信息。

    读取能力包括：条目的 ProviderIds（取豆瓣 ID）、真实集号列表（判定是否末集）、
    单集已播放状态、所属媒体库，以及可用于前端展示的图片地址。
    读取失败统一返回空值，由调用方降级。
    """

    def __init__(self, server_name: Optional[str] = None, timeout: int = 15,
                 username: Optional[str] = None) -> None:
        """
        :param server_name: 媒体服务器名称，为空时取第一个可用实例
        :param timeout: 预留的超时设置，实例自身已有默认超时
        :param username: 用于查询播放状态的用户名。
            播放状态按用户隔离，必须指定实际看剧的账号，
            否则取到的是管理员（宿主默认用 SUPERUSER）的播放记录。
        """
        self._server_name = server_name
        self._timeout = timeout
        self._username = (username or "").strip()
        self._cache: Dict[Tuple[str, str], Any] = {}
        # 运行时解析出的 userId，解析失败时回退到宿主默认（管理员）
        self._user_id: Optional[str] = None
        self._user_resolved = False

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

        response = self._get(
            instance,
            f"[HOST]emby/Users/[USER]/Items/{item_id}"
            f"?fields=ProviderIds,Path,ParentId&api_key=[APIKEY]",
        )
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

    def get_series_id(self, item_id: str) -> Optional[str]:
        """
        解析条目所属剧集的 ID。
        宿主的 webhook 对剧集事件已经把 item_id 置为 SeriesId，
        这里仍做一次兜底判断，避免不同版本字段缺失时取不到。
        """
        if not item_id:
            return None
        item = self.get_item(item_id)
        if not item:
            # 条目读不到时，按宿主约定直接认为传入的就是剧集 ID
            return str(item_id)

        item_type = str(item.get("Type") or "")
        if item_type == "Series":
            return str(item.get("Id") or item_id)

        series_id = item.get("SeriesId")
        if series_id:
            return str(series_id)

        season_id = item.get("SeasonId") or item.get("ParentId")
        if season_id and item_type == "Season":
            season_item = self.get_item(str(season_id))
            if season_item:
                return str(season_item.get("SeriesId") or season_id)
        return None

    def get_season_episodes(self, series_id: str, season_no: int) -> List[int]:
        """
        读取某一季实际存在的集号列表（升序）。
        走 Shows/Id/Episodes 逐集遍历，与宿主判断剧集存在性的口径一致，
        不依赖 Season 元数据里的 ChildCount。
        """
        if not series_id:
            return []
        instance, server_type = self._locate()
        if not instance:
            return []

        cache_key = (server_type, f"{series_id}-episodes-{season_no}")
        cached = self._cache.get(cache_key)
        if cached is not None:
            return list(cached)

        response = self._get(
            instance,
            f"[HOST]emby/Shows/{series_id}/Episodes?Season={season_no}"
            f"&IsMissing=false&api_key=[APIKEY]",
        )
        episodes: Set[int] = set()
        items = response.get("Items") if isinstance(response, dict) else None
        for entry in items or []:
            if not isinstance(entry, dict):
                continue
            index = self._to_int(entry.get("IndexNumber"))
            parent = self._to_int(entry.get("ParentIndexNumber"))
            if index is None or index <= 0:
                continue
            # 只认属于目标季的集，Season 参数失效时靠这个兜底
            if parent is not None and season_no and parent != season_no:
                continue
            episodes.add(index)

        result = sorted(episodes)
        self._cache[cache_key] = result
        return list(result)

    def is_episode_played(self, series_id: str, season_no: int, episode_no: int) -> Optional[bool]:
        """
        读取指定单集在媒体服务器里的已播放标记。

        这是「是否看完」最可靠的依据：Emby / Jellyfin 在播放结束或用户手动
        标记为已播放时都会把 Played 置为 True，不依赖任何刮削元数据。
        取不到返回 None，由调用方降级到按末集判定。
        """
        if not series_id or not episode_no:
            return None
        instance, server_type = self._locate()
        if not instance:
            return None

        cache_key = (server_type, f"{series_id}-played-{season_no}-{episode_no}")
        if cache_key in self._cache:
            return self._cache[cache_key]

        response = self._get(
            instance,
            self._user_scope_url(
                instance,
                f"/emby/Shows/{series_id}/Episodes?Season={season_no}&IsMissing=false",
            ),
        )
        items = response.get("Items") if isinstance(response, dict) else None
        played: Optional[bool] = None
        for entry in items or []:
            if not isinstance(entry, dict):
                continue
            if self._to_int(entry.get("IndexNumber")) != episode_no:
                continue
            user_data = entry.get("UserData")
            if isinstance(user_data, dict) and "Played" in user_data:
                played = bool(user_data.get("Played"))
            break

        self._cache[cache_key] = played
        return played

    def get_librarys(self, server_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        读取媒体服务器的全部媒体库，归一化成字典列表。
        读取失败返回空列表，调用方据此关闭库过滤。
        """
        instance, _ = self._locate(server_name)
        if not instance or not hasattr(instance, "get_librarys"):
            return []

        try:
            libraries = instance.get_librarys()
        except Exception as error:
            logger.warn(f"读取媒体库列表失败：{error}")
            return []

        result: List[Dict[str, Any]] = []
        for library in libraries or []:
            name = self._pick(library, "name")
            if not name:
                continue
            path = self._pick(library, "path")
            result.append({
                "id": str(self._pick(library, "id") or ""),
                "name": str(name),
                "type": str(self._pick(library, "type") or ""),
                "paths": [str(item) for item in (path if isinstance(path, list) else [path]) if item],
            })
        return result

    @staticmethod
    def build_title_candidates(title: str, season: int = 0) -> List[str]:
        """
        生成标题候选，按「越可能命中 Emby 原名」的顺序排列。

        档案里的标题可能来自事件拼接，与媒体服务器里的剧集原名不一致，
        例如档案存「小猪佩奇 迷你剧 第二季 第二季」，
        而 Emby 里实际叫「小猪佩奇迷你剧」。逐级放宽直到能匹配上。
        """
        base = re.sub(r"\s+", " ", str(title or "")).strip()
        if not base:
            return []

        # 数字需覆盖阿拉伯数字与中文数字（第二季 / 第2季）。
        # 必须用非捕获分组包裹，否则 "|" 的优先级会让后半段脱离整体，
        # 导致拼接出的正则分支错乱、剥离失败。
        number = r"(?:\d+|[一二三四五六七八九十百零〇两])"
        # 去掉各种季数后缀：第N季 / 第N部 / Season N / S N
        pattern = (r"\s*第\s*" + number + r"\s*[季部集]\s*"
                   r"|\s*Season\s*" + number + r"\s*"
                   r"|\s*S" + number + r"\s*")
        stripped = re.sub(pattern, " ", base, flags=re.IGNORECASE)
        stripped = re.sub(r"\s+", " ", stripped).strip()

        # 去掉末尾孤立的数字（如「xxx 5」）
        no_tail = re.sub(r"\s+" + number + r"\s*$", "", stripped).strip()

        # 中文剧名里的空格在 Emby 里常被去掉（如「小猪佩奇 迷你剧」→「小猪佩奇迷你剧」）
        compact = re.sub(r"\s+", "", base)
        compact_stripped = re.sub(r"\s+", "", stripped)

        candidates: List[str] = [base]
        for extra in (stripped, compact, no_tail, compact_stripped):
            if extra and extra not in candidates:
                candidates.append(extra)
        return candidates

    def search_series(self, title: str, season: int = 0) -> Optional[str]:
        """
        按标题搜索剧集条目，返回剧集 ID。
        用于重扫历史档案：档案里只有标题和豆瓣 ID，需要重新定位媒体服务器条目。

        先用原始标题精确匹配；匹配不到时依次用标题候选重试，
        以适应档案标题与 Emby 原名不一致的情况。
        """
        if not title:
            return None
        instance, server_type = self._locate()
        if not instance:
            return None

        result = self._search_by_title(instance, title, exact_only=True)
        if result:
            return result

        # 候选里可能只有原标题自身（如「征途」无季数后缀），
        # 此时也要再试一次放宽匹配，否则精确匹配失败就没有任何回退机会。
        for candidate in self.build_title_candidates(title, season):
            if candidate == title:
                continue
            result = self._search_by_title(instance, candidate, exact_only=False)
            if result:
                logger.info(f"标题「{title}」按候选「{candidate}」匹配到剧集条目")
                return result

        # 放宽同一标题再试一次，覆盖「结果集非空但名称不完全相同」的情况
        return self._search_by_title(instance, title, exact_only=False)

    def _search_by_title(self, instance: Any, title: str,
                         exact_only: bool) -> Optional[str]:
        """
        用单个标题查询剧集条目。
        exact_only 为真时只接受名称完全相同的结果，否则接受首条非空结果。
        """
        cache_key = ("search", f"{title}-{int(exact_only)}")
        if cache_key in self._cache:
            return self._cache[cache_key]

        response = self._get(
            instance,
            f"[HOST]emby/Users/[USER]/Items?SearchTerm={quote(str(title))}"
            f"&IncludeItemTypes=Series&Recursive=true&api_key=[APIKEY]",
        )
        items = response.get("Items") if isinstance(response, dict) else None
        found: Optional[str] = None
        normalized = re.sub(r"\s+", "", str(title)).strip()
        for entry in items or []:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("Id") or "")
            if not item_id:
                continue
            name = str(entry.get("Name") or "").strip()
            if not name:
                continue
            if name == str(title).strip() or re.sub(r"\s+", "", name) == normalized:
                found = item_id
                break
            if not exact_only and not found:
                found = item_id

        self._cache[cache_key] = found
        return found

    def _resolve_user_id(self, instance: Any) -> Optional[str]:
        """
        解析用于查询播放状态的 userId。

        播放状态（UserData.Played）在媒体服务器里是按用户隔离的，
        宿主的 [USER] 占位符固定替换为管理员（SUPERUSER），
        管理员没看过的剧会返回 Played=false，导致「明明看完却判定未看完」。
        这里按配置的用户名解析真实 userId，解析不到才退回宿主默认。
        """
        if self._user_resolved:
            return self._user_id
        self._user_resolved = True

        if not self._username:
            self._user_id = None
            return None

        try:
            users = instance.get_data("[HOST]Users?api_key=[APIKEY]")
            if users is None or getattr(users, "status_code", None) != 200:
                return None
            data = users.json()
        except Exception as error:
            logger.debug(f"读取媒体服务器用户列表失败：{error}")
            return None

        if not isinstance(data, list):
            return None
        for user in data:
            if isinstance(user, dict) and str(user.get("Name") or "") == self._username:
                self._user_id = str(user.get("Id") or "") or None
                if self._user_id:
                    logger.debug(f"播放状态查询使用用户 {self._username}（{self._user_id}）")
                return self._user_id

        logger.warn(f"媒体服务器中未找到用户 {self._username}，播放状态可能不准确")
        self._user_id = None
        return None

    def _user_scope_url(self, instance: Any, path: str) -> str:
        """
        拼接带用户维度的查询地址。

        path 可能已带查询串（形如 ".../Episodes?Season=1&IsMissing=false"），
        因此这里用 & 追加参数，不能再用 ?，否则会产生两个问号导致后续参数全部失效。
        """
        user_id = self._resolve_user_id(instance)
        api_key = getattr(instance, "_apikey", "")
        host = getattr(instance, "_host", "") or ""
        separator = "&" if "?" in path else "?"
        extra = f"userId={user_id}" if user_id else ""
        if api_key:
            extra = f"{extra}&api_key={api_key}" if extra else f"api_key={api_key}"
        return f"{host.rstrip('/')}{path}{separator}{extra}" if extra \
            else f"{host.rstrip('/')}{path}"

    def get_season_play_state(self, series_id: str, season_no: int) -> Dict[int, bool]:
        """
        一次性读取某一季所有集的已播放状态，返回 {集号: 是否已播放}。
        重扫档案时用它判断整季是否看完，只需一次请求。
        """
        if not series_id:
            return {}
        instance, server_type = self._locate()
        if not instance:
            return {}

        cache_key = (server_type, f"{series_id}-playstate-{season_no}")
        if cache_key in self._cache:
            return dict(self._cache[cache_key])

        url = self._user_scope_url(
            instance,
            f"/emby/Shows/{series_id}/Episodes?Season={season_no}&IsMissing=false",
        )
        response = self._get(instance, url)
        items = response.get("Items") if isinstance(response, dict) else None
        state: Dict[int, bool] = {}
        for entry in items or []:
            if not isinstance(entry, dict):
                continue
            index = self._to_int(entry.get("IndexNumber"))
            if index is None or index <= 0:
                continue
            user_data = entry.get("UserData")
            if isinstance(user_data, dict) and "Played" in user_data:
                state[index] = bool(user_data.get("Played"))
            else:
                state[index] = False

        self._cache[cache_key] = state
        return dict(state)

    def list_library_series(self, library_id: str) -> List[Dict[str, Any]]:
        """
        遍历一个媒体库下的全部剧集，返回条目摘要列表。

        用于全量导入：插件此前的档案完全依赖 webhook 事件，
        没被播放事件触发过的剧集永远不会进档案，而重扫只能处理已有条目。
        """
        if not library_id:
            return []
        instance, server_type = self._locate()
        if not instance:
            return []

        cache_key = (server_type, f"libseries-{library_id}")
        if cache_key in self._cache:
            return list(self._cache[cache_key])

        result: List[Dict[str, Any]] = []
        start_index = 0
        page_size = 200
        # 分页上限保护，避免超大库无限翻页
        for _ in range(50):
            response = self._get(
                instance,
                f"[HOST]emby/Users/[USER]/Items?ParentId={library_id}"
                f"&IncludeItemTypes=Series&Recursive=true"
                f"&StartIndex={start_index}&Limit={page_size}&api_key=[APIKEY]",
            )
            items = response.get("Items") if isinstance(response, dict) else None
            if not items:
                break
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                item_id = str(entry.get("Id") or "")
                name = str(entry.get("Name") or "").strip()
                if not item_id or not name:
                    continue
                result.append({
                    "id": item_id,
                    "name": name,
                    "type": str(entry.get("Type") or ""),
                    "provider_ids": entry.get("ProviderIds") or {},
                    "path": str(entry.get("Path") or ""),
                })
            total = response.get("TotalRecordCount") if isinstance(response, dict) else None
            start_index += len(items)
            if not items or (total is not None and start_index >= int(total)):
                break

        self._cache[cache_key] = result
        return list(result)

    def get_series_season_state(self, series_id: str) -> Dict[int, Dict[int, bool]]:
        """
        读取一个剧集所有季的播放状态，返回 {季号: {集号: 是否已播放}}。
        全量导入时用来判断每季是否看完，无需逐季单独请求。
        """
        if not series_id:
            return {}
        instance, server_type = self._locate()
        if not instance:
            return {}

        cache_key = (server_type, f"allseasons-{series_id}")
        if cache_key in self._cache:
            return dict(self._cache[cache_key])

        url = self._user_scope_url(instance, f"/emby/Shows/{series_id}/Episodes")
        response = self._get(instance, url)
        items = response.get("Items") if isinstance(response, dict) else None

        by_season: Dict[int, Dict[int, bool]] = {}
        for entry in items or []:
            if not isinstance(entry, dict):
                continue
            season = self._to_int(entry.get("ParentIndexNumber")) or 0
            index = self._to_int(entry.get("IndexNumber"))
            if index is None or index <= 0:
                continue
            user_data = entry.get("UserData")
            played = bool(user_data.get("Played")) \
                if isinstance(user_data, dict) and "Played" in user_data else False
            by_season.setdefault(season, {})[index] = played

        self._cache[cache_key] = by_season
        return dict(by_season)

    def get_item_path(self, item_id: str) -> Optional[str]:
        """读取条目的物理路径，用于按媒体路径关键词过滤。"""
        if not item_id:
            return None
        item = self.get_item(item_id)
        path = item.get("Path")
        if isinstance(path, str) and path:
            return path
        return None

    def get_item_library_id(self, item_id: str) -> Optional[str]:
        """
        溯源条目所属的媒体库 ID。

        宿主的 get_librarys() 走 Users/{user}/Views，该接口只返回 Id / Name /
        CollectionType，不含 Path，因此无法用路径前缀反推归属。这里改为沿
        ParentId 逐级向上，找到祖先中命中媒体库 ID 的那个即为所属库。
        """
        if not item_id:
            return None
        library_ids = {library["id"] for library in self.get_librarys() if library.get("id")}
        if not library_ids:
            return None

        current = str(item_id)
        # 剧集往上是 单集 → 季 → 剧集 → 库，最多回溯若干层足够覆盖
        for _ in range(6):
            item = self.get_item(current)
            if not item:
                return None
            current = str(item.get("ParentId") or item.get("SeasonId") or "")
            if not current:
                return None
            if current in library_ids:
                return current
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

    def _locate(self, server_name: Optional[str] = None) -> Tuple[Optional[Any], str]:
        """
        定位可用的媒体服务器实例，返回 (实例, 类型)。
        传入 server_name 时优先精确匹配，匹配不到再回退到第一个可用实例。
        缓存以「目标服务器名」为键，目标变化时重新枚举，避免拿到别的实例。
        """
        wanted = server_name or self._server_name or ""
        if getattr(self, "_located", None) is not None \
                and wanted == getattr(self, "_located_name", ""):
            return (self._located, getattr(self, "_located_type", ""))

        result: Tuple[Optional[Any], str] = (None, "")
        fallback: Tuple[Optional[Any], str] = (None, "")
        try:
            for service in MediaServerHelper().iterate_module_instances():
                server_type = (service.type or "").lower()
                if server_type not in _SUPPORTED_TYPES or not service.instance:
                    continue
                if wanted and service.name == wanted:
                    result = (service.instance, server_type)
                    break
                if not fallback[0]:
                    fallback = (service.instance, server_type)
        except Exception as error:
            logger.debug(f"枚举媒体服务器实例失败：{error}")
            return (None, "")

        located = result[0] if result[0] else fallback[0]
        located_type = result[1] if result[0] else fallback[1]
        if located is not None:
            self._located = located
            self._located_type = located_type
            self._located_name = wanted or ""
        return (located, located_type)

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
    def _pick(data: Any, key: str) -> Any:
        """兼容 pydantic 模型与字典两种形态的取值。"""
        if isinstance(data, dict):
            return data.get(key)
        return getattr(data, key, None)

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
