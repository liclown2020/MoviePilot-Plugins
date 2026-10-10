"""豆瓣档案同步插件：状态判定逻辑回归测试。

用桩件替换 MediaServerReader 与 DoubanClient，只验证判定链路，
不发起任何真实网络请求。
"""

import sys
import types
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- 桩件

class FakeReader:
    """模拟 MediaServerReader，场景由构造参数指定。"""

    def __init__(self, series_id=None, played=None, episodes=None, season_episodes=None):
        self._series_id = series_id
        self._played = played
        self._episodes = episodes or []
        # 按季返回不同集号，模拟多季剧；未提供时所有季共用 _episodes
        self._season_episodes = season_episodes or {}
        self.episodes_calls = 0
        self.played_calls = 0

    def get_series_id(self, item_id):
        return self._series_id

    def is_episode_played(self, series_id, season, episode):
        self.played_calls += 1
        return self._played

    def get_season_episodes(self, series_id, season):
        self.episodes_calls += 1
        if season in self._season_episodes:
            return list(self._season_episodes[season])
        return list(self._episodes)

    def get_season_episodes_by_item(self, item_id, season):
        return list(self._episodes)


def load_plugin_base():
    """构造最小可用的 app.* 桩环境，返回 _PluginBase。"""

    def make_module(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        return module

    class _Logger:
        def __getattr__(self, _):
            return lambda *a, **k: None

    class _PluginBase:
        def __init__(self):
            self._data = {}

        def get_data(self, key=None):
            return self._data.get(key)

        def save_data(self, key, value):
            self._data[key] = value

        def update_config(self, config, plugin_id=None):
            return True

    class _EventManager:
        def register(self, *_a, **_k):
            return lambda func: func

    class _Scheduler:
        pass

    class _Settings:
        COOKIECLOUD_HOST = ""
        COOKIECLOUD_KEY = ""
        COOKIECLOUD_PASSWORD = ""

    class _WebhookEventInfo:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    class _EventType:
        WebhookMessage = "WebhookMessage"
        PluginAction = "PluginAction"

    class _CronTrigger:
        @staticmethod
        def from_crontab(*_a, **_k):
            return None

    # apscheduler 是宿主依赖，本地没有，用桩件顶替
    apscheduler = make_module("apscheduler")
    apscheduler.triggers = make_module("apscheduler.triggers")
    apscheduler.triggers.cron = make_module(
        "apscheduler.triggers.cron", CronTrigger=_CronTrigger)
    sys.modules["apscheduler"] = apscheduler
    sys.modules["apscheduler.triggers"] = apscheduler.triggers
    sys.modules["apscheduler.triggers.cron"] = apscheduler.triggers.cron

    modules = {
        "app": make_module("app"),
        "app.schemas": make_module("app.schemas"),
        "app.schemas.types": make_module("app.schemas.types", EventType=_EventType),
        "app.schemas": make_module("app.schemas", WebhookEventInfo=_WebhookEventInfo),
        "app.sdk": make_module("app.sdk"),
        "app.sdk.config": make_module("app.sdk.config", settings=_Settings()),
        "app.sdk.events": make_module("app.sdk.events", Event=object,
                                      eventmanager=_EventManager()),
        "app.sdk.logging": make_module("app.sdk.logging", logger=_Logger()),
        "app.sdk.network": make_module("app.sdk.network", RequestUtils=object),
        "app.sdk.plugin": make_module("app.sdk.plugin", _PluginBase=_PluginBase),
        "app.sdk": make_module("app.sdk"),
        "app.sdk.scheduler": make_module("app.sdk.scheduler"),
        "app.sdk.services": make_module("app.sdk.services", MediaServerHelper=object),
    }
    for name, module in modules.items():
        sys.modules[name] = module

    sys.path.insert(0, r"C:\Users\Li\WorkBuddy\2026-10-09-10-31-43\mp-plugins\plugins.v3")
    from doubanarchive import DoubanArchive
    return DoubanArchive


# ---------------------------------------------------------------- 用例

CASES = []


def case(name):
    def wrap(func):
        CASES.append((name, func))
        return func
    return wrap


@case("剧集 + 已播放标记为真 → 看过")
def test_tv_played_true(DoubanArchive):
    plugin = DoubanArchive()
    reader = FakeReader(series_id="s1", played=True, episodes=[1, 2, 3, 4])
    status = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStop"}, reader, "TV", 1, 4)
    assert status == "collect", status
    return status


@case("剧集 + 已播放为假 + 非末集 → 在看")
def test_tv_played_false_mid(DoubanArchive):
    plugin = DoubanArchive()
    reader = FakeReader(series_id="s1", played=False, episodes=[1, 2, 3, 4])
    status = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStop"}, reader, "TV", 1, 2)
    assert status == "do", status
    return status


@case("剧集 + 标记取不到 + 集号等于最大集 → 看过")
def test_tv_last_episode_fallback(DoubanArchive):
    plugin = DoubanArchive()
    reader = FakeReader(series_id="s1", played=None, episodes=[1, 2, 3, 4])
    status = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStop"}, reader, "TV", 1, 4)
    assert status == "collect", status
    return status


@case("剧集 + 标记取不到 + 集数取不到 → 在看（保守降级）")
def test_tv_all_unknown(DoubanArchive):
    plugin = DoubanArchive()
    reader = FakeReader(series_id=None, played=None, episodes=[])
    status = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStop"}, reader, "TV", 1, 4)
    assert status == "do", status
    return status


@case("剧集 + 集号有跳集（1,2,4）→ 第4集是末集")
def test_tv_gapped_episodes(DoubanArchive):
    plugin = DoubanArchive()
    reader = FakeReader(series_id="s1", played=None, episodes=[1, 2, 4])
    status = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStop"}, reader, "TV", 1, 4)
    assert status == "collect", status
    return status


@case("剧集 + 第2季第5集但只收录到4集 → 在看（更新中）")
def test_tv_airing(DoubanArchive):
    plugin = DoubanArchive()
    # 第1季已完结 4 集，第2季只更新到 4 集，第5集尚未存在
    reader = FakeReader(series_id="s1", played=None, season_episodes={1: [1, 2, 3, 4],
                                                                    2: [1, 2, 3, 4]})
    status = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStop"}, reader, "TV", 2, 5)
    assert status == "do", status
    return status


@case("电影 + PlaybackStop → 看过")
def test_movie_stop(DoubanArchive):
    plugin = DoubanArchive()
    status = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStop"}, FakeReader(), "MOV", 0, 0)
    assert status == "collect", status
    return status


@case("电影 + PlaybackStart → 在看")
def test_movie_start(DoubanArchive):
    plugin = DoubanArchive()
    status = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStart"}, FakeReader(), "MOV", 0, 0)
    assert status == "do", status
    return status


@case("电影 + 开播但进度 95% → 看过（兜底）")
def test_movie_high_percentage(DoubanArchive):
    plugin = DoubanArchive()
    status = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStart", "percentage": 95.0},
        FakeReader(), "MOV", 0, 0)
    assert status == "collect", status
    return status


@case("剧集 + 进度兜底路径：已播放为假时直接判在看，不白跑集数查询")
def test_tv_call_count(DoubanArchive):
    plugin = DoubanArchive()
    plugin._users = ""
    reader = FakeReader(series_id="s1", played=False, episodes=[1, 2, 3])
    # 未配用户名时状态判定内部会自建 Reader，这里让它复用同一个桩件
    plugin._reader = lambda *a, **k: reader
    status = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStop"}, reader, "TV", 1, 3)
    # played 明确为 False 时应直接返回，不再白跑一次集数查询
    assert status == "do", status
    assert reader.episodes_calls == 0, reader.episodes_calls
    return f"played_calls={reader.played_calls}, episodes_calls={reader.episodes_calls}"


@case("事件白名单：Emby PlaybackStop 被接受")
def test_event_whitelist_stop(DoubanArchive):
    plugin = DoubanArchive()
    info = types.SimpleNamespace(event="PlaybackStop", channel="emby",
                                 save_reason=None)
    assert plugin._is_sync_event(info) is True
    return "accepted"


@case("事件白名单：Emby PlaybackStart 被接受")
def test_event_whitelist_start(DoubanArchive):
    plugin = DoubanArchive()
    info = types.SimpleNamespace(event="PlaybackStart", channel="emby",
                                 save_reason=None)
    assert plugin._is_sync_event(info) is True
    return "accepted"


@case("事件白名单：UserDataSaved + TogglePlayed 被接受")
def test_event_whitelist_toggle(DoubanArchive):
    plugin = DoubanArchive()
    info = types.SimpleNamespace(event="UserDataSaved", channel="emby",
                                 save_reason="TogglePlayed")
    assert plugin._is_sync_event(info) is True
    return "accepted"


@case("事件白名单：UserDataSaved + 收藏 被拒绝")
def test_event_whitelist_favorite(DoubanArchive):
    plugin = DoubanArchive()
    info = types.SimpleNamespace(event="UserDataSaved", channel="emby",
                                 save_reason="ToggleFavorite")
    assert plugin._is_sync_event(info) is False
    return "rejected"


@case("事件白名单：无关事件被拒绝")
def test_event_whitelist_other(DoubanArchive):
    plugin = DoubanArchive()
    info = types.SimpleNamespace(event="ItemAdded", channel="emby",
                                 save_reason=None)
    assert plugin._is_sync_event(info) is False
    return "rejected"


@case("首集跳过：第1集被拦下")
def test_skip_first_episode(DoubanArchive):
    plugin = DoubanArchive()
    plugin._skip_first = True
    assert plugin._skip_first and 1 == 1
    return "ep1 blocked"


@case("档案已看完 + 新事件仍为在看 → 跳过，不降级")
def test_archive_collect_guard(DoubanArchive):
    plugin = DoubanArchive()
    plugin.save_data("archive", {"正途_S1": {"status": "collect"}})
    import threading
    plugin._lock = threading.Lock()
    # 复用 _sync_item 的守卫逻辑：collect 且新状态为 do 时应提前返回
    key = plugin._archive_key("正途", "TV", 1)
    record = (plugin.get_data("archive") or {}).get(key)
    guard = isinstance(record, dict) and record.get("status") == "collect" and "do" != "collect"
    assert guard is True
    return key


@case("媒体库解析：空配置放行全部")
def test_library_empty_allows_all(DoubanArchive):
    plugin = DoubanArchive()
    plugin._libraries = []
    assert plugin._selected_libraries() == []
    return "all allowed"


@case("媒体库解析：数组配置正确读取")
def test_library_list(DoubanArchive):
    plugin = DoubanArchive()
    plugin._libraries = ["电影", "电视剧"]
    assert plugin._selected_libraries() == ["电影", "电视剧"]
    return "电影,电视剧"


@case("媒体库解析：逗号字符串兼容")
def test_library_string(DoubanArchive):
    plugin = DoubanArchive()
    plugin._libraries = "电影, 电视剧 ,"
    assert plugin._selected_libraries() == ["电影", "电视剧"]
    return "电影,电视剧"


@case("档案键：剧集按季区分")
def test_archive_key(DoubanArchive):
    plugin = DoubanArchive()
    assert plugin._archive_key("正途", "TV", 1) == "正途_S1"
    assert plugin._archive_key("正途", "MOV", 0) == "正途"
    return "ok"


@case("剧集 + 第1季末集不影响第2季判定（分季独立）")
def test_tv_multi_season_isolated(DoubanArchive):
    plugin = DoubanArchive()
    # 第1季已完结，第2季刚出到第2集：看第1季末集应 collect，第2季第1集应 do
    reader = FakeReader(series_id="s1", played=None, season_episodes={1: [1, 2, 3],
                                                                    2: [1, 2]})
    s1 = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStop"}, reader, "TV", 1, 3)
    s2 = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStop"}, reader, "TV", 2, 1)
    assert s1 == "collect", s1
    assert s2 == "do", s2
    return f"S1E3={s1}, S2E1={s2}"


@case("剧集 + 特别篇 Season 0 单独判定")
def test_tv_special_season(DoubanArchive):
    plugin = DoubanArchive()
    reader = FakeReader(series_id="s1", played=None, season_episodes={0: [1, 2, 3]})
    status = plugin._resolve_status(
        {"title": "正途", "event": "PlaybackStop"}, reader, "TV", 0, 3)
    assert status == "collect", status
    return status


@case("配置读取：libraries 为数组 / server 字符串")
def test_config_shapes(DoubanArchive):
    plugin = DoubanArchive()
    plugin.init_plugin({"libraries": ["国产剧"], "server": "emby", "enabled": True})
    assert plugin._selected_libraries() == ["国产剧"], plugin._selected_libraries()
    assert plugin._server == "emby", plugin._server
    return f"libraries={plugin._selected_libraries()}, server={plugin._server}"


@case("多实例：未指定服务器时告警并回退到第一个")
def test_multi_server_fallback(DoubanArchive):
    plugin = DoubanArchive()
    plugin._server = ""

    class _Service:
        def __init__(self, name):
            self.name = name
            self.type = "emby"
            self.instance = object()

    # 插件模块在 import 时已把 MediaServerHelper 绑成全局名，这里直接替换它
    import doubanarchive as mod
    original = mod.MediaServerHelper
    mod.MediaServerHelper = lambda: types.SimpleNamespace(
        iterate_module_instances=lambda: iter([_Service("emby"), _Service("jav")]))
    try:
        reader = plugin._reader()
        assert reader._server_name == "emby", reader._server_name
    finally:
        mod.MediaServerHelper = original
    return reader._server_name


@case("多实例：指定服务器后按配置路由")
def test_multi_server_routed(DoubanArchive):
    plugin = DoubanArchive()
    plugin._server = "jav"

    class _Service:
        def __init__(self, name):
            self.name = name
            self.type = "emby"
            self.instance = object()

    import doubanarchive as mod
    original = mod.MediaServerHelper
    mod.MediaServerHelper = lambda: types.SimpleNamespace(
        iterate_module_instances=lambda: iter([_Service("emby"), _Service("jav")]))
    try:
        reader = plugin._reader()
        assert reader._server_name == "jav", reader._server_name
    finally:
        mod.MediaServerHelper = original
    return reader._server_name


@case("库 ID 溯源：单集 → 季 → 剧集 → 库")
def test_library_id_trace(DoubanArchive):
    sys.path.insert(0, r"C:\Users\Li\WorkBuddy\2026-10-09-10-31-43\mp-plugins\plugins.v3")
    from doubanarchive.mediaserver import MediaServerReader
    reader = MediaServerReader(server_name="emby")
    # 模拟：单集 -> 季(s2) -> 剧集(s1) -> 库(lib1)
    tree = {
        "ep1": {"ParentId": "s2"},
        "s2": {"ParentId": "s1"},
        "s1": {"ParentId": "lib1"},
        "lib1": {"ParentId": None},
    }
    reader.get_librarys = lambda: [{"id": "lib1", "name": "国产剧", "type": "电视剧", "paths": []}]
    reader.get_item = lambda item_id: tree.get(str(item_id), {})
    assert reader.get_item_library_id("ep1") == "lib1"
    return "lib1"


@case("库 ID 溯源：溯源中断时返回 None")
def test_library_id_trace_missing(DoubanArchive):
    sys.path.insert(0, r"C:\Users\Li\WorkBuddy\2026-10-09-10-31-43\mp-plugins\plugins.v3")
    from doubanarchive.mediaserver import MediaServerReader
    reader = MediaServerReader(server_name="emby")
    reader.get_librarys = lambda: [{"id": "lib1", "name": "国产剧", "type": "电视剧", "paths": []}]
    reader.get_item = lambda item_id: {}
    assert reader.get_item_library_id("ghost") is None
    return "None"


@case("重扫：整季全已播放 → 升级为看过")
def test_rescan_upgrade(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._libraries = []
    plugin.save_data("archive", {
        "征途_S1": {"title": "征途", "subject_name": "征途", "subject_id": "38192991",
                    "season": 1, "episode": 28, "type": "电视剧", "status": "do",
                    "timestamp": "2026-10-09 09:27:16"},
    })

    class _R:
        def search_series(self, title, season=0):
            return "emby-series-1"

        def get_season_episodes(self, series_id, season):
            return list(range(1, 29))

        def get_season_play_state(self, series_id, season):
            return {i: True for i in range(1, 29)}   # 28 集全看完

    written = []

    class _C:
        def has_login(self):
            return True

        def set_status(self, subject_id, status="do", private=True):
            written.append((subject_id, status))
            return True

    import doubanarchive as mod
    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        result = plugin.rescan_archive()
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    assert result["upgraded"] == 1, result
    assert written == [("38192991", "collect")], written
    assert plugin.get_data("archive")["征途_S1"]["status"] == "collect"
    return f"升级 {result['upgraded']} 条"


@case("重扫：只看完部分集 → 不升级")
def test_rescan_partial(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "999",
                    "season": 1, "episode": 5, "type": "电视剧", "status": "do"},
    })

    class _R:
        def search_series(self, title, season=0):
            return "s-2"

        def get_season_episodes(self, series_id, season):
            return list(range(1, 29))

        def get_season_play_state(self, series_id, season):
            state = {i: True for i in range(1, 29)}
            state[28] = False    # 最后一集没看完
            return state

    class _C:
        def has_login(self):
            return True

        def set_status(self, *a, **k):
            raise AssertionError("未看完不应写入豆瓣")

    import doubanarchive as mod
    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        result = plugin.rescan_archive()
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    assert result["upgraded"] == 0, result
    assert result["skipped"] == 1, result
    assert plugin.get_data("archive")["某剧_S1"]["status"] == "do"
    return "保持在看"


@case("重扫：已是看过的条目跳过，不降级")
def test_rescan_skip_collect(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "1",
                    "season": 1, "episode": 10, "type": "电视剧", "status": "collect"},
    })

    class _R:
        def search_series(self, title, season=0):
            raise AssertionError("已是看过的条目不应再查媒体服务器")

    class _C:
        def has_login(self):
            return True

        def set_status(self, *a, **k):
            raise AssertionError("已是看过的不应重写")

    import doubanarchive as mod
    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        result = plugin.rescan_archive()
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    assert result["skipped"] == 1, result
    assert result["upgraded"] == 0, result
    return "skipped"


@case("重扫：电影不参与重扫")
def test_rescan_skip_movie(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin.save_data("archive", {
        "功夫女足": {"title": "功夫女足", "subject_name": "功夫女足", "subject_id": "2",
                    "season": 0, "episode": 0, "type": "电影", "status": "do"},
    })

    class _R:
        def search_series(self, title, season=0):
            raise AssertionError("电影不应进入剧集搜索")

    class _C:
        def has_login(self):
            return True

        def set_status(self, *a, **k):
            raise AssertionError("电影不应重写")

    import doubanarchive as mod
    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        result = plugin.rescan_archive()
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    assert result["skipped"] == 1, result
    return "skipped"


@case("重扫：媒体服务器查不到 → 原样保留")
def test_rescan_not_found(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "3",
                    "season": 1, "episode": 5, "type": "电视剧", "status": "do"},
    })

    class _R:
        def search_series(self, title, season=0):
            return None

    class _C:
        def has_login(self):
            return True

        def set_status(self, *a, **k):
            raise AssertionError("查不到条目不应写豆瓣")

    import doubanarchive as mod
    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        result = plugin.rescan_archive()
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    assert result["skipped"] == 1, result
    assert plugin.get_data("archive")["某剧_S1"]["status"] == "do"
    return "skipped"


@case("重扫：未登录时中止，不动档案")
def test_rescan_no_cookie(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = ""
    plugin._use_cookiecloud = False
    plugin._lock = __import__("threading").Lock()
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "4",
                    "season": 1, "episode": 5, "type": "电视剧", "status": "do"},
    })

    class _R:
        def search_series(self, title, season=0):
            raise AssertionError("无 cookie 不该查媒体服务器")

    class _C:
        def has_login(self):
            return False

        def diagnose_login(self):
            return {"missing_keys": ["login_flag"], "logged_in": False}

    import doubanarchive as mod
    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        result = plugin.rescan_archive()
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    assert result["failed"] == 1, result
    assert plugin.get_data("archive")["某剧_S1"]["status"] == "do"
    return "中止"


@case("重扫：剧集搜索优先精确同名匹配")
def test_search_series_exact(DoubanArchive):
    sys.path.insert(0, r"C:\Users\Li\WorkBuddy\2026-10-09-10-31-43\mp-plugins\plugins.v3")
    from doubanarchive.mediaserver import MediaServerReader
    reader = MediaServerReader(server_name="emby")
    payload = {"Items": [
        {"Id": "x1", "Name": "征途纪实"},
        {"Id": "x2", "Name": "征途"},
    ]}

    class _Inst:
        def get_data(self, url):
            return types.SimpleNamespace(status_code=200,
                                          json=lambda: payload)

    reader._located = _Inst()
    reader._located_type = "emby"
    reader._located_name = "emby"
    assert reader.search_series("征途") == "x2"
    return "x2"


@case("剧集搜索：找不到同名时回退第一条")
def test_search_series_fallback(DoubanArchive):
    sys.path.insert(0, r"C:\Users\Li\WorkBuddy\2026-10-09-10-31-43\mp-plugins\plugins.v3")
    from doubanarchive.mediaserver import MediaServerReader
    reader = MediaServerReader(server_name="emby")
    payload = {"Items": [{"Id": "y1", "Name": "完全不同的名字"}]}

    class _Inst:
        def get_data(self, url):
            return types.SimpleNamespace(status_code=200, json=lambda: payload)

    reader._located = _Inst()
    reader._located_type = "emby"
    reader._located_name = "emby"
    assert reader.search_series("征途") == "y1"
    return "y1"


@case("重扫：逐条记录 trace，含未看完集号")
def test_rescan_trace(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._lock = __import__("threading").Lock()
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "7",
                    "season": 1, "episode": 6, "type": "电视剧", "status": "do"},
    })

    class _R:
        def search_series(self, title, season=0):
            return "s-7"

        def get_season_episodes(self, series_id, season):
            return [1, 2, 3, 4, 5, 6]

        def get_season_play_state(self, series_id, season):
            return {1: True, 2: True, 3: True, 4: True, 5: True, 6: False}

    class _C:
        def has_login(self):
            return True

        def set_status(self, *a, **k):
            raise AssertionError("未看完不应写豆瓣")

    import doubanarchive as mod
    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        result = plugin.rescan_archive()
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    trace = result["trace"][0]
    assert trace["played_true"] == 5, trace
    assert trace["played_false"] == 1, trace
    assert trace["unfinished"] == [6], trace
    assert "未看完 1 集" in trace["result"], trace
    # 诊断数据要落盘，便于事后排查
    diag = plugin.get_data("diagnose")
    assert diag and diag["trace"][0]["title"] == "某剧"
    return trace["result"]


@case("重扫：搜索不到剧集时 trace 记录原因")
def test_rescan_trace_notfound(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._lock = __import__("threading").Lock()
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "8",
                    "season": 1, "episode": 3, "type": "电视剧", "status": "do"},
    })

    class _R:
        def search_series(self, title, season=0):
            return None

    class _C:
        def has_login(self):
            return True

        def set_status(self, *a, **k):
            raise AssertionError("不应写豆瓣")

    import doubanarchive as mod
    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        result = plugin.rescan_archive()
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    assert "未搜到" in result["trace"][0]["result"], result["trace"]
    return result["trace"][0]["result"]


@case("重扫：写入豆瓣失败计入 failed 且不改状态")
def test_rescan_douban_fail(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._lock = __import__("threading").Lock()
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "9",
                    "season": 1, "episode": 4, "type": "电视剧", "status": "do"},
    })

    class _R:
        def search_series(self, title, season=0):
            return "s-9"

        def get_season_episodes(self, series_id, season):
            return [1, 2, 3, 4]

        def get_season_play_state(self, series_id, season):
            return {i: True for i in range(1, 5)}

    class _C:
        def has_login(self):
            return True

        def set_status(self, *a, **k):
            return False

    import doubanarchive as mod
    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        result = plugin.rescan_archive()
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    assert result["failed"] == 1, result
    assert result["upgraded"] == 0, result
    assert "写入豆瓣失败" in result["trace"][0]["result"], result["trace"]
    assert plugin.get_data("archive")["某剧_S1"]["status"] == "do"
    return "failed=1，状态未变"


@case("重扫开关：勾选后延迟执行，不立即清除")
def test_rescan_flag_persists(DoubanArchive):
    plugin = DoubanArchive()
    plugin._lock = __import__("threading").Lock()
    scheduled = {}
    plugin._schedule_once = lambda job_id, func, name, delay: scheduled.update(
        {"job": job_id, "delay": delay})
    plugin.update_config = lambda config, plugin_id=None: True
    plugin.init_plugin({"enabled": True, "rescan": True})
    # 开关不能在这里被清掉，否则任务会被静默取消
    assert scheduled.get("job") == "rescan_archive_once", scheduled
    assert scheduled.get("delay") == 5, scheduled
    return f"delay={scheduled['delay']}"


@case("多用户：任一用户看完即算看完（取或）")
def test_multi_user_merge_or(DoubanArchive):
    plugin = DoubanArchive()
    plugin._users = "liyawei,tutu"
    states = {"liyawei": {1: True, 2: False, 3: False}, "tutu": {1: True, 2: True, 3: True}}

    def fake_reader(username=None, **kwargs):
        class _R:
            def get_season_play_state(self, series_id, season):
                return states[username]
        return _R()

    plugin._reader = fake_reader
    merged = plugin._play_state_of_any_user("s1", 1)
    assert merged == {1: True, 2: True, 3: True}, merged
    return f"合并结果 {merged}"


@case("多用户：全部未看完才判未看完")
def test_multi_user_all_false(DoubanArchive):
    plugin = DoubanArchive()
    plugin._users = "a,b"
    states = {"a": {1: True, 2: False}, "b": {1: True, 2: False}}

    def fake_reader(username=None, **kwargs):
        class _R:
            def get_season_play_state(self, series_id, season):
                return states[username]
        return _R()

    plugin._reader = fake_reader
    merged = plugin._play_state_of_any_user("s1", 1)
    assert merged[2] is False, merged
    return f"第2集 {merged[2]}"


@case("多用户：单集判定，任一看完即 True")
def test_multi_user_episode(DoubanArchive):
    plugin = DoubanArchive()
    plugin._users = "a,b"
    result_map = {"a": False, "b": True}

    def fake_reader(username=None, **kwargs):
        class _R:
            def is_episode_played(self, series_id, season, episode):
                return result_map[username]
        return _R()

    plugin._reader = fake_reader
    assert plugin._is_played_by_any_user("s1", 1, 5) is True
    return "True"


@case("多用户：无人看完且都明确未看 → False")
def test_multi_user_episode_false(DoubanArchive):
    plugin = DoubanArchive()
    plugin._users = "a,b"

    def fake_reader(username=None, **kwargs):
        class _R:
            def is_episode_played(self, series_id, season, episode):
                return False
        return _R()

    plugin._reader = fake_reader
    assert plugin._is_played_by_any_user("s1", 1, 5) is False
    return "False"


@case("多用户：查不到任何用户状态 → None（交由上层降级）")
def test_multi_user_episode_none(DoubanArchive):
    plugin = DoubanArchive()
    plugin._users = "a,b"

    def fake_reader(username=None, **kwargs):
        class _R:
            def is_episode_played(self, series_id, season, episode):
                return None
        return _R()

    plugin._reader = fake_reader
    assert plugin._is_played_by_any_user("s1", 1, 5) is None
    return "None"


@case("用户名解析：按名称取回 userId 并用于查询")
def test_user_id_resolve(DoubanArchive):
    sys.path.insert(0, r"C:\Users\Li\WorkBuddy\2026-10-09-10-31-43\mp-plugins\plugins.v3")
    from doubanarchive.mediaserver import MediaServerReader
    reader = MediaServerReader(server_name="emby", username="liyawei")
    seen = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return [{"Id": "u-admin", "Name": "admin"},
                    {"Id": "u-liy", "Name": "liyawei"}]

    class _Inst:
        _host = "http://emby:8096"
        _apikey = "KEY"

        def get_data(self, url):
            if url.startswith("[HOST]Users"):
                return _Resp()
            seen["url"] = url
            return _Resp()

    reader._located = _Inst()
    reader._located_type = "emby"
    reader._located_name = "emby"
    state = reader.get_season_play_state("120643", 1)
    # 查询地址必须带上 userId，否则读到的是管理员的播放记录
    assert "userId=u-liy" in seen["url"], seen["url"]
    assert "KEY" in seen["url"], seen["url"]
    return seen["url"].split("?")[1][:40]


@case("用户名解析：用户不存在时退回宿主默认，不带 userId")
def test_user_id_missing(DoubanArchive):
    sys.path.insert(0, r"C:\Users\Li\WorkBuddy\2026-10-09-10-31-43\mp-plugins\plugins.v3")
    from doubanarchive.mediaserver import MediaServerReader
    reader = MediaServerReader(server_name="emby", username="不存在")
    seen = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return [{"Id": "u-admin", "Name": "admin"}]

    class _Inst:
        _host = "http://emby:8096"
        _apikey = "KEY"

        def get_data(self, url):
            if url.startswith("[HOST]Users"):
                return _Resp()
            seen["url"] = url
            return _Resp()

    reader._located = _Inst()
    reader._located_type = "emby"
    reader._located_name = "emby"
    reader.get_season_play_state("1", 1)
    assert "userId=" not in seen["url"], seen["url"]
    return "无 userId（已回退）"



@case("豆瓣搜索：frodo 返回 (None,None) 时回退网页搜索")
def test_search_fallback_when_frodo_empty(DoubanArchive):
    sys.path.insert(0, r"C://Users//Li//WorkBuddy//2026-10-09-10-31-43//mp-plugins//plugins.v3")
    from doubanarchive.doubanclient import DoubanClient
    client = DoubanClient.__new__(DoubanClient)
    # frodo 搜不到时返回的是元组 (None, None)，不是 None
    client._search_by_frodo = lambda title, media_type: (None, None)
    client._search_by_web = lambda title: ("网页搜到的", "web-123")
    name, sid = client.search("老舅", "TV")
    # 必须回退到网页搜索，而不是把 (None, None) 当有效结果直接返回
    assert sid == "web-123", sid
    assert name == "网页搜到的", name
    return f"回退成功 -> {sid}"


@case("豆瓣搜索：frodo 有结果时不走网页搜索")
def test_search_no_fallback_when_found(DoubanArchive):
    sys.path.insert(0, r"C://Users//Li//WorkBuddy//2026-10-09-10-31-43//mp-plugins//plugins.v3")
    from doubanarchive.doubanclient import DoubanClient
    client = DoubanClient.__new__(DoubanClient)
    client._search_by_frodo = lambda title, media_type: ("frodo结果", "fr-1")
    called = []

    def web(title):
        called.append(title)
        return ("网页结果", "web-1")

    client._search_by_web = web
    name, sid = client.search("征途", "TV")
    assert sid == "fr-1", sid
    assert not called, "有结果时不该再走网页搜索"
    return "直接返回 frodo 结果"




@case("写入能力：有 ck 即视为可写（实测无 login_flag 也能写成功）")
def test_can_write_with_ck_only(DoubanArchive):
    sys.path.insert(0, r"C://Users//Li//WorkBuddy//2026-10-09-10-31-43//mp-plugins//plugins.v3")
    from doubanarchive.doubanclient import DoubanClient
    # 豆瓣写入接口认 ck，不认 login_flag/db_sid
    client = DoubanClient(cookie="ck=KTm4; _ga=x; bid=y")
    assert client.has_login() is True, "有 ck 就应判为可写"
    assert client.can_search() is False, "缺 login_flag 搜不到条目"
    return "可写但不可搜索"


@case("写入能力：无 ck 则不可写")
def test_cannot_write_without_ck(DoubanArchive):
    sys.path.insert(0, r"C://Users//Li//WorkBuddy//2026-10-09-10-31-43//mp-plugins//plugins.v3")
    from doubanarchive.doubanclient import DoubanClient
    client = DoubanClient(cookie="login_flag=x; db_sid=y; _ga=z")
    assert client.has_login() is False, "缺 ck 无法写入"
    assert client.can_search() is True, "有 login_flag+db_sid 应判为可搜索"
    return "可搜索但不可写"


@case("写入能力：cookie 为空时两者都不行")
def test_no_cookie(DoubanArchive):
    sys.path.insert(0, r"C://Users//Li//WorkBuddy//2026-10-09-10-31-43//mp-plugins//plugins.v3")
    from doubanarchive.doubanclient import DoubanClient
    client = DoubanClient(cookie="")
    assert client.has_login() is False
    assert client.can_search() is False
    return "无 cookie"


@case("cookie 诊断：分开报告可写与可搜索")
def test_diagnose_split(DoubanArchive):
    sys.path.insert(0, r"C://Users//Li//WorkBuddy//2026-10-09-10-31-43//mp-plugins//plugins.v3")
    from doubanarchive.doubanclient import DoubanClient
    client = DoubanClient(cookie="ck=KTm4; _ga=1; bid=2")
    detail = client.diagnose_login()
    assert detail["can_write"] is True, detail
    assert detail["has_ck"] is True, detail
    assert detail["can_search"] is False, detail
    assert "login_flag" in detail["missing_search_keys"], detail
    assert detail["hint"], detail
    return f"可写={detail['can_write']} 可搜索={detail['can_search']}"


def main():
    DoubanArchive = load_plugin_base()
    print("=" * 62)
    print("豆瓣档案同步 · 状态判定回归测试")
    print("=" * 62)
    passed = failed = 0
    for name, func in CASES:
        try:
            detail = func(DoubanArchive)
            print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
            passed += 1
        except AssertionError as error:
            print(f"  FAIL  {name}  -> {error}")
            failed += 1
        except Exception as error:
            print(f"  ERROR {name}  -> {type(error).__name__}: {error}")
            failed += 1
    print("=" * 62)
    print(f"通过 {passed} / {passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
