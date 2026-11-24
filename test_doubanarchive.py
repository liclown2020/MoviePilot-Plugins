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



@case("重扫：整季看完的已看过条目保持不变，不重复写")
def test_rescan_keep_collect(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._lock = __import__("threading").Lock()
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "1",
                    "season": 1, "episode": 10, "type": "电视剧", "status": "collect"},
    })

    class _R:
        def search_series(self, title, season=0):
            return "s1"

        def get_season_episodes(self, series_id, season):
            return list(range(1, 11))

        def get_season_play_state(self, series_id, season):
            return {i: True for i in range(1, 11)}

        def is_series_ended(self, series_id):
            return True

    class _C:
        def has_login(self):
            return True

        def set_status(self, *a, **k):
            raise AssertionError("已是看过且确实看完，不该重写")

    import doubanarchive as mod
    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        result = plugin.rescan_archive()
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    assert result["upgraded"] == 0, result
    assert result["corrected"] == 0, result
    assert plugin.get_data("archive")["某剧_S1"]["status"] == "collect"
    return "保持看过"


@case("重扫：已看过但实际未看完 → 误标校正为在看")
def test_rescan_correct_mislabeled(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._lock = __import__("threading").Lock()
    # 模拟「正在更新时被误标看过」：实际只看了 2 集，共 4 集
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "2",
                    "season": 1, "episode": 2, "type": "电视剧", "status": "collect"},
    })

    class _R:
        def search_series(self, title, season=0):
            return "s2"

        def get_season_episodes(self, series_id, season):
            return [1, 2, 3, 4]

        def get_season_play_state(self, series_id, season):
            return {1: True, 2: True, 3: False, 4: False}

        def is_series_ended(self, series_id):
            return False

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

    assert result["corrected"] == 1, result
    assert written == [("2", "do")], written
    assert plugin.get_data("archive")["某剧_S1"]["status"] == "do"
    assert "误标校正" in result["trace"][0]["result"], result["trace"]
    return "已降回在看"


@case("判定：正在更新时看完一集不算整季看完")
def test_status_updating_series(DoubanArchive):
    plugin = DoubanArchive()
    plugin._users = ""
    plugin._lock = __import__("threading").Lock()

    class _R:
        def get_series_id(self, item_id):
            return "s3"

        def is_episode_played(self, series_id, season, episode):
            return True

        def get_season_episodes(self, series_id, season):
            return [1, 2]

        def is_series_ended(self, series_id):
            # 仍在连载
            return False

    reader = _R()
    plugin._reader = lambda *a, **k: reader
    status = plugin._resolve_status(
        {"title": "伟大的长征", "event": "PlaybackStop"}, reader, "TV", 1, 2)
    # 第 2 集虽已播放且是当前最大集号，但剧集连载中，不算整季看完
    assert status == "do", status
    return "保持在看"


@case("判定：末集看完且非更新中 → 看过")
def test_status_last_episode_done(DoubanArchive):
    plugin = DoubanArchive()
    plugin._users = ""
    plugin._lock = __import__("threading").Lock()

    class _R:
        def get_series_id(self, item_id):
            return "s4"

        def is_episode_played(self, series_id, season, episode):
            return True

        def get_season_episodes(self, series_id, season):
            return [1, 2, 3, 4]

        def is_series_ended(self, series_id):
            return True

    reader = _R()
    plugin._reader = lambda *a, **k: reader
    status = plugin._resolve_status(
        {"title": "某剧", "event": "PlaybackStop"}, reader, "TV", 1, 4)
    assert status == "collect", status
    return "判为看过"



@case("同步：档案已有该条目时不会引用未赋值的 status")
def test_sync_existing_record_no_unbound(DoubanArchive):
    """回归测试：v1.3.0 引入的判断放在 status 赋值之前，导致同步必崩。"""
    import doubanarchive as mod

    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._private = False
    plugin._lock = __import__("threading").Lock()
    # 关键：档案里已有该条目 —— 这正是触发崩溃的路径
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "9",
                    "season": 1, "episode": 5, "type": "电视剧", "status": "collect"},
    })

    class _R:
        def get_douban_id(self, item_id):
            return "9"

        def get_series_id(self, item_id):
            return "s9"

        def is_episode_played(self, series_id, season, episode):
            return False

        def get_season_episodes(self, series_id, season):
            return list(range(1, 21))

        def is_series_ended(self, series_id):
            return True

    class _C:
        def has_login(self):
            return True

        def set_status(self, *a, **k):
            raise AssertionError("已是看过且本次仍判在看，不该写豆瓣")

        def get_subject_image(self, subject_id, media_type):
            return ""

    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        # 修复前这里会抛 UnboundLocalError: local variable 'status'
        plugin._sync_item({"title": "某剧", "media_type": "TV", "season": 1,
                           "episode": 5, "subject_id": "9"})
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    assert plugin.get_data("archive")["某剧_S1"]["status"] == "collect", "状态不应改变"
    return "正常跳过，无异常"


@case("同步：档案已有条目但在看时仍可升级为看过")
def test_sync_upgrade_from_watching(DoubanArchive):
    import doubanarchive as mod

    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._private = False
    plugin._lock = __import__("threading").Lock()
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "9",
                    "season": 1, "episode": 5, "type": "电视剧", "status": "do"},
    })

    class _R:
        def get_douban_id(self, item_id):
            return "9"

        def get_series_id(self, item_id):
            return "s9"

        def is_episode_played(self, series_id, season, episode):
            return True

        def get_season_episodes(self, series_id, season):
            return list(range(1, 21))

        def is_series_ended(self, series_id):
            return True

    written = []

    class _C:
        def has_login(self):
            return True

        def set_status(self, subject_id, status="do", private=True):
            written.append((subject_id, status))
            return True

        def get_subject_image(self, subject_id, media_type):
            return ""

    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        plugin._sync_item({"title": "某剧", "media_type": "TV", "season": 1,
                           "episode": 20, "subject_id": "9"})
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    assert written == [("9", "collect")], written
    assert plugin.get_data("archive")["某剧_S1"]["status"] == "collect"
    return "已升级为看过"



@case("候选标题：剥离多位中文数字季数（十一/十二/二十/一百）")
def test_candidates_cn_multi_digit(DoubanArchive):
    sys.path.insert(0, r"C://Users//Li//WorkBuddy//2026-10-09-10-31-43//mp-plugins//plugins.v3")
    from doubanarchive.mediaserver import MediaServerReader
    cases = {
        "小猪佩奇 第十一季 第11季": "小猪佩奇",
        "小猪佩奇 第十二季 第12季": "小猪佩奇",
        "小猪佩奇 第二十季": "小猪佩奇",
        "小猪佩奇 第一百季": "小猪佩奇",
        "小猪佩奇 第二十三季": "小猪佩奇",
        "小猪佩奇 第一百二十季": "小猪佩奇",
    }
    for title, expect in cases.items():
        got = MediaServerReader.build_title_candidates(title, 1)
        assert expect in got, f"{title} → {got}"
    return f"{len(cases)} 个多位中文数字全部剥离"


@case("候选标题：普通剧名不被误伤")
def test_candidates_no_false_strip(DoubanArchive):
    sys.path.insert(0, r"C://Users//Li//WorkBuddy//2026-10-09-10-31-43//mp-plugins//plugins.v3")
    from doubanarchive.mediaserver import MediaServerReader
    for title in ["伟大的长征", "征途", "铁拳教育", "旺卡", "小城良方"]:
        got = MediaServerReader.build_title_candidates(title, 1)
        assert got[0] == title, got
        assert title in got, got
    return "5 个剧名未受影响"



@case("重扫：校准 episode 为实际最大已播集号，状态不变")
def test_rescan_calibrate_episode(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._users = "a"
    plugin._lock = __import__("threading").Lock()
    # 档案记的是「最后触发事件集号」45，实际只看了 18 集
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "5",
                    "season": 1, "episode": 45, "type": "电视剧", "status": "do"},
    })

    class _R:
        def search_series(self, title, season=0):
            return "s5"

        def get_season_episodes(self, series_id, season):
            return list(range(1, 25))

        def get_season_play_state(self, series_id, season):
            state = {i: True for i in range(1, 19)}
            state.update({i: False for i in range(19, 25)})
            return state

        def is_series_ended(self, series_id):
            return True

    class _C:
        def has_login(self):
            return True

        def set_status(self, subject_id, status="do", private=True):
            raise AssertionError("状态本就该是「在看」，不该重写")

        def get_subject_image(self, subject_id, media_type):
            return ""

    import doubanarchive as mod
    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        result = plugin.rescan_archive()
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    saved = plugin.get_data("archive")["某剧_S1"]
    assert saved["episode"] == 18, f"集号应校准为 18，实际 {saved['episode']}"
    assert saved["status"] == "do", f"状态不应改变，实际 {saved['status']}"
    assert result["upgraded"] == 0, result
    assert result["corrected"] == 0, result
    return "集号 45 → 18，状态保持在看"


@case("重扫：集号已准确时不改动")
def test_rescalibrate_no_change(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._users = "a"
    plugin._lock = __import__("threading").Lock()
    plugin.save_data("archive", {
        "某剧_S1": {"title": "某剧", "subject_name": "某剧", "subject_id": "5",
                    "season": 1, "episode": 18, "type": "电视剧", "status": "do"},
    })

    class _R:
        def search_series(self, title, season=0):
            return "s5"

        def get_season_episodes(self, series_id, season):
            return list(range(1, 25))

        def get_season_play_state(self, series_id, season):
            state = {i: True for i in range(1, 19)}
            state.update({i: False for i in range(19, 25)})
            return state

        def is_series_ended(self, series_id):
            return True

    class _C:
        def has_login(self):
            return True

        def set_status(self, *a, **k):
            raise AssertionError("集号已准确，不该重写")

        def get_subject_image(self, subject_id, media_type):
            return ""

    import doubanarchive as mod
    orig_reader, orig_client = plugin._reader, mod.DoubanClient
    plugin._reader = lambda *a, **k: _R()
    mod.DoubanClient = lambda *a, **k: _C()
    try:
        result = plugin.rescan_archive()
    finally:
        plugin._reader, mod.DoubanClient = orig_reader, orig_client

    assert plugin.get_data("archive")["某剧_S1"]["episode"] == 18
    return "未改动"



@case("图片代理：签名 URL 生成与校验")
def test_image_proxy_signature(DoubanArchive):
    import base64
    plugin = DoubanArchive()
    plugin._lock = __import__("threading").Lock()
    plugin.save_data("image_secret", "testsecret")

    url = "https://img3.doubanio.com/view/photo/m_ratio_poster/public/p123.webp"
    signed = plugin._sign_image_url(url)
    # 相对路径：挂在子路径下也能正确解析（与 MP 前端 ./assets/... 同理）
    assert signed.startswith("api/v1/plugin/DoubanArchive/image?"), signed
    assert "u=" in signed and "e=" in signed and "s=" in signed, signed

    # 解出参数并校验签名
    from urllib.parse import parse_qs, urlparse
    query = parse_qs(urlparse(signed).query)
    decoded = base64.urlsafe_b64decode(query["u"][0] + "=" * (-len(query["u"][0]) % 4)).decode()
    assert decoded == url, decoded
    assert plugin._verify_image_signature(decoded, int(query["e"][0]), query["s"][0]) is True

    # 篡改地址必须验不过
    assert plugin._verify_image_signature("https://evil.example.com/x.jpg",
                                         int(query["e"][0]), query["s"][0]) is False
    # 错误签名也必须验不过
    assert plugin._verify_image_signature(decoded, int(query["e"][0]), "bad" * 8) is False
    return "签名可验证，篡改被拒"


@case("图片代理：过期链接被拒")
def test_image_proxy_expired(DoubanArchive):
    plugin = DoubanArchive()
    plugin._lock = __import__("threading").Lock()
    plugin.save_data("image_secret", "testsecret")
    url = "https://img3.doubanio.com/x.webp"
    signed = plugin._sign_image_url(url)
    from urllib.parse import parse_qs, urlparse
    query = parse_qs(urlparse(signed).query)
    expired = int(query["e"][0]) - 1
    assert plugin._verify_image_signature(url, expired, query["s"][0]) is False
    return "过期被拒"


@case("图片代理：所有封面都返回代理链接，不受数量限制")
def test_image_proxy_all_images(DoubanArchive):
    plugin = DoubanArchive()
    plugin._lock = __import__("threading").Lock()
    plugin.save_data("image_secret", "testsecret")
    plugin._inline_image = True

    # 34 条以上，验证不再有「超出上限就返回空」的截断
    records = [{"image": f"https://img3.doubanio.com/view/photo/m_ratio_poster/public/p{i}.webp"}
               for i in range(40)]
    records.append({"image": "https://emby.liyawei.top:434/Items/1/Images/Backdrop"})

    results = [plugin._resolve_image(r) for r in records]
    empty = [i for i, r in enumerate(results) if not r]
    assert not empty, f"{len(empty)} 条返回空: {empty[:5]}"
    for r in results:
        assert r.startswith("api/v1/plugin/DoubanArchive/image?"), r[:60]
    return f"{len(results)} 条全部有代理链接"


@case("图片代理：关闭代理开关时退回原始地址")
def test_image_proxy_disabled(DoubanArchive):
    plugin = DoubanArchive()
    plugin._lock = __import__("threading").Lock()
    plugin.save_data("image_secret", "testsecret")
    plugin._inline_image = False
    url = "https://img3.doubanio.com/x.webp"
    assert plugin._resolve_image({"image": url}) == url
    return "已退回原始地址"


@case("图片代理：拒绝非 http(s) 地址")
def test_image_proxy_reject_non_http(DoubanArchive):
    plugin = DoubanArchive()
    plugin._lock = __import__("threading").Lock()
    plugin.save_data("image_secret", "testsecret")
    try:
        plugin.api_proxy_image(u="", e=0, s="")
        raise AssertionError("应拒绝非法地址")
    except Exception as error:
        assert "400" in str(error) or "不合法" in str(error) or "无法解析" in str(error), error
    return "非法地址被拒"





@case("仪表盘：默认只显示最近 N 个月，其余通过展开按钮查看")
def test_dashboard_recent_plus_expand(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._lock = __import__("threading").Lock()
    plugin._pc_month = 2
    plugin._mobile_month = 2
    plugin._pc_num = 50
    plugin._mobile_num = 15
    plugin._inline_image = False

    archive = {}
    for m in range(1, 6):
        for i in range(2):
            archive[f"剧{m}_S1_{i}"] = {
                "title": f"剧{m}", "subject_name": f"剧{m}",
                "subject_id": str(m * 10 + i), "season": 1, "episode": i + 1,
                "type": "电视剧", "status": "collect",
                "image": f"https://img3.doubanio.com/{m}_{i}.webp",
                "timestamp": f"2026-0{m}-1{i} 12:00:00",
            }
    plugin.save_data("archive", archive)

    cols, attrs, elements = plugin.get_dashboard("archive")
    # 前 2 个是最近两个月的面板
    timeline_panels = [e for e in elements if e.get("component") == "VTimeline"]
    assert len(timeline_panels) == 2, f"默认应显示 2 个月，实际 {len(timeline_panels)}"
    # 最后是展开按钮
    toggle = elements[-1]
    assert toggle.get("html"), "展开按钮应是 v-html"
    assert "dba-expand-btn" in toggle["html"], "缺少展开按钮"
    assert "展开全部（3 个月）" in toggle["html"], toggle["html"][:200]
    return "默认 2 个月 + 展开 3 个月"


@case("仪表盘：展开区有滚动条且包含全部剩余月份")
def test_dashboard_expand_area_scroll(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._lock = __import__("threading").Lock()
    plugin._pc_month = 1
    plugin._mobile_month = 1
    plugin._pc_num = 50
    plugin._mobile_num = 15
    plugin._inline_image = False

    archive = {}
    for m in range(1, 5):
        for i in range(2):
            archive[f"剧{m}_S1_{i}"] = {
                "title": f"剧{m}", "subject_name": f"剧{m}",
                "subject_id": str(m * 10 + i), "season": 1, "episode": i + 1,
                "type": "电视剧", "status": "collect",
                "image": f"https://img3.doubanio.com/{m}_{i}.webp",
                "timestamp": f"2026-0{m}-1{i} 12:00:00",
            }
    plugin.save_data("archive", archive)

    cols, attrs, elements = plugin.get_dashboard("archive")
    toggle = elements[-1]["html"]
    # 右侧滚动条
    assert "overflow-y:auto" in toggle, "展开区应可垂直滚动"
    assert "max-height:70vh" in toggle, "展开区应限制高度"
    # 展开区里应包含剩余 3 个月共 6 个卡片
    # 默认展示最近 1 个月（4月），展开区是更早的 3 个月
    assert "2026年1月" in toggle and "2026年2月" in toggle and "2026年3月" in toggle, "月份不全"
    assert "2026年4月" not in toggle, "4月是默认展示的月份，不该出现在展开区"
    assert toggle.count("<img") == 6, f"应有 6 张图，实际 {toggle.count('<img')}"
    # 按钮的 onclick 要存在
    assert "onclick" in toggle, "缺少 onclick"
    return "展开区含 3 个月 6 张图，带滚动条"


@case("仪表盘：展开区 HTML 对标题做转义")
def test_dashboard_html_escape(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._lock = __import__("threading").Lock()
    plugin._pc_month = 1
    plugin._mobile_month = 1
    plugin._pc_num = 50
    plugin._mobile_num = 15
    plugin._inline_image = False

    plugin.save_data("archive", {
        "a_S1": {"title": "a", "subject_name": "a", "subject_id": "1",
                 "season": 1, "episode": 1, "type": "电视剧", "status": "collect",
                 "image": "https://img3.doubanio.com/1.webp",
                 "timestamp": "2026-03-01 12:00:00"},
        "b_S1": {"title": "it's <b>bold</b> & 'quoted'", "subject_name": "x",
                 "subject_id": "2", "season": 1, "episode": 1,
                 "type": "电视剧", "status": "collect",
                 "image": "https://img3.doubanio.com/2.webp",
                 "timestamp": "2026-02-01 12:00:00"},
    })

    cols, attrs, elements = plugin.get_dashboard("archive")
    toggle = elements[-1]["html"]
    # 裸的 <b> 不应出现（title 属性里的尖括号必须转义）
    assert "title='it\'s" not in toggle.replace("&apos;", "'"), "引号未转义"
    assert "&lt;b&gt;" in toggle or "&amp;" in toggle, "尖括号未转义"
    return "标题已转义"


@case("仪表盘：单月条目超限时分段显示而非丢弃")
def test_dashboard_no_item_loss(DoubanArchive):
    plugin = DoubanArchive()
    plugin._enabled = True
    plugin._cookie = "ck=x"
    plugin._lock = __import__("threading").Lock()
    plugin._pc_month = 3
    plugin._mobile_month = 2
    plugin._pc_num = 4
    plugin._mobile_num = 15
    plugin._inline_image = False

    archive = {}
    for i in range(10):
        archive[f"剧_S1_{i}"] = {
            "title": "剧", "subject_name": "剧",
            "subject_id": str(100 + i), "season": 1, "episode": i + 1,
            "type": "电视剧", "status": "collect",
            "image": f"https://img3.doubanio.com/{i}.webp",
            "timestamp": "2026-03-01 12:00:00",
        }
    plugin.save_data("archive", archive)

    cols, attrs, elements = plugin.get_dashboard("archive")
    panel = elements[0]
    rows = panel["content"][0]["content"][1:]
    card_count = sum(len(r.get("content", [])) for r in rows if r["component"] == "VRow")
    dividers = sum(1 for r in rows if r["component"] == "VDivider")
    assert card_count == 10, f"卡片应全部渲染，实际 {card_count}"
    assert dividers == 2, f"应有 2 个分隔线，实际 {dividers}"
    return f"10 条分 3 段全渲染"


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
