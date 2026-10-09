# MoviePilot 插件库

面向 MoviePilot V3（`system_version >= 3.0.0`）的第三方插件。两个插件各自独立，无依赖关系。

| 插件 | 类名 / 目录 | 版本 | 作者 | 来源 |
| --- | --- | --- | --- | --- |
| 豆瓣档案同步 | `DoubanArchive` / `plugins.v3/doubanarchive` | 1.6.0 | liclown2020 | 本仓库原创 |
| 缺失集数订阅V3版 | `EpisodeNoExistV3` / `plugins.v3/episodenoexistv3` | 1.0.0 | boeto / liclown2020 | 改编自 [boeto/MoviePilot-Plugins](https://github.com/boeto/MoviePilot-Plugins) 的「缺失集数订阅」0.0.8 |

---

# 豆瓣档案同步（DoubanArchive）

把 Emby / Jellyfin 的播放进度同步到豆瓣「书影音档案」。看完自动标记「看过」。

作者：liclown2020（本仓库原创）

## 特点

| 能力 | 说明 |
| --- | --- |
| 不依赖 TMDB | 豆瓣 ID 优先读媒体服务器已刮削的 `ProviderIds.Douban`，取不到才搜索 |
| 看完判定准确 | 以媒体服务器的真实已播放标记为准，不靠「看到最后一集」猜 |
| 支持多账号 | 播放状态按用户隔离，可指定多个用户名，任一账号看完即算看完 |
| 不阻塞宿主 | 网络请求交给宿主延后任务队列，webhook 事件线程立即返回 |
| 失败自动重试 | 写入失败的条目进入队列，每 30 分钟重试，也可用 `/douban_retry` 手动触发 |

## 状态判定

- **电影**：出现播完事件，或播放进度 ≥ 90% → 「看过」。
- **剧集**：读该集的真实已播放标记；取不到时回退到「是否本季末集」；仍取不到则记为「在看」。

只升不降：已是「看过」的记录不会被「在看」事件改回去。

## 配置项

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 总开关 |
| 仅自己可见 | 豆瓣条目是否设为私密 |
| 不标记第一集 | 跳过每季第一集（不影响末集升级） |
| 媒体库用户名 | 逗号分隔。既是事件过滤，也是播放状态查询的账号 |
| 媒体服务器 | 配了多台 Emby/Jellyfin 时必须指定 |
| 同步的媒体库 | 多选，不选表示全部 |
| 媒体路径排除关键词 | 命中关键词的路径不处理 |
| 豆瓣cookie | 留空时从 CookieCloud 获取 |
| 立即重试失败队列 | 保存后立即重试一次 |
| 重扫档案 | 按已播放状态批量校正历史条目 |
| 显示月份数 / 每月最多显示 | 仪表盘展示数量控制 |
| 内联豆瓣图片 | 豆瓣图片有防盗链，开启后由后端取回并内联（默认开启） |

## 命令与接口

| 用途 | 入口 |
| --- | --- |
| 重试失败队列 | 命令 `/douban_retry` |
| 重扫历史档案 | 命令 `/douban_rescan`，或 `POST /rescan` |
| 排查 | `GET /diagnose`，返回每条的实际读取结果 |

重扫只升级为「看过」，不降级；电影不参与；查不到的条目原样保留。

## 仪表盘

按月（含年份）分组的时间线海报墙。海报优先取豆瓣竖版封面，缺失的会在重试任务里自动补齐；始终没有封面时显示片名占位，记录不会从墙上消失。

豆瓣图片有防盗链（直连返回 403），因此由后端取回后内联为 data URI 并缓存，刷新不会反复回源。

## 数据说明

同步档案存在插件数据里，键为 `archive`，失败队列键为 `pending`。插件不会修改媒体服务器中的任何数据。

图标 `icons/douban.png` 取自插件社区常见素材，仅用于识别插件。

---

# 缺失集数订阅V3版（EpisodeNoExistV3）

定时比对媒体库里**实际存在的集数**与**TMDB 已播出的集数**，找出缺集并自动补订阅。

原作者：[boeto/MoviePilot-Plugins](https://github.com/boeto/MoviePilot-Plugins) 的「缺失集数订阅」0.0.8。
本版本由 liclown2020 适配 MoviePilot V3 后收录，与本仓库的豆瓣档案同步插件无关联。

## 它解决什么问题

手动整理媒体库时容易漏集——某部剧下载了但缺第 8 集，或整季都没入库。
本插件按季比对：TMDB 已播 10 集、库里只有 1-5 集，则记录「缺失 6-10」并可自动订阅补齐。

未播出的集（`air_date` 晚于当前日期）不计入总集数，不会误报缺集。

## 配置项

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 总开关 |
| 执行周期 | 5 位 cron，留空则每天 08:00 执行 |
| 立即运行一次 | 保存后延时 3 秒跑一次，不影响定时计划 |
| 清理检查记录 | 清空历史记录后重新开始统计 |
| 历史数据类型 | 详情页展示哪类记录（最新 6 条 / 存在缺失 / 非全集缺失 / 已加订阅 / 全部存在 / 失败 / 所有） |
| 缺失处理方式 | 仅记录 / 自动添加到订阅 / 直接标记为存在 |
| 媒体服务器白名单 | 多选，留空表示全部 |
| 电视剧媒体库白名单 | **必填**，逗号分隔。只有列出的库会被扫描 |
| 下载路径替换 | 一行一条，格式 `媒体库路径前缀:下载路径前缀` |

### 下载路径替换怎么写

取剧集**目录的父目录**做前缀替换。例如想把
`/media/library/tv/上载新生 (2020)` 的下载位置改到 `/downloads/tv`：

```
/media/library:/downloads
```

父目录 `/media/library/tv` 替换后得到 `/downloads/tv`。

## 详情页操作

每条记录配三个按钮：**订阅缺失**（补订阅该剧缺失的季）、**标记存在**（不再检查）、**删除记录**（让它下次重新检查）。

顶部统计卡展示：总处理 / 存在缺失 / 非全集缺失 / 未识别 / 全部存在 / 已订阅。

## V3 适配说明

原插件为 V2 编写，V3 上无法运行，本版本改动如下：

| 项目 | V2（原插件） | V3（本版本） |
| --- | --- | --- |
| 导入路径 | `app.core.config.settings`、`app.log` | `app.sdk.config`、`app.sdk.logging` |
| 插件基类 | `app.plugins._PluginBase` | `app.plugins`（契约迁至 `app.sdk.plugin`，旧路径保留） |
| 媒体身份 | `item.tmdbid` 单字段 | `media_source` + `media_id` 成对传递 |
| 识别媒体 | `recognize_media(tmdbid=...)` | `recognize_media(media_source=..., media_id=...)` |
| 订阅去重 | `exists(tmdbid, season=...)` | `exists(media_source, media_id, season=...)`（身份为必填位置参数） |
| 添加订阅 | `add(tmdbid=...)` | `add(media_source=..., media_id=...)` |
| 媒体服务器 | `settings.MEDIASERVER.split(",")` + `MediaServerChain` | `MediaServerHelper().get_services()` 枚举实例，读取走模块的 `get_librarys()` / `get_items()` / `get_tv_episodes()` |
| API 密钥 | `settings.API_TOKEN` | 运行时设置读取，失败回落 `settings` |
| 序列化 | `.dict()`（pydantic v1） | `.model_dump()`（pydantic v2） |
| 详情页链接 | `mediaid=tmdb:<id>` | `mediaid=<source>:<id>` |

另外两处行为修正：

- **下载路径替换**：原实现用 `pathlib.Path(save_path).parent` 取父目录，在非 POSIX 宿主上会
  按平台解析路径把前缀吃掉，现改为显式按 `/` 截取。
- **非 TMDB 来源**：豆瓣等来源的 `media_id` 不是数字且无季集数据，原实现仍会拿去查 TMDB
  季集并参与统计，本版本识别到非 TMDB 来源时直接跳过，不再误判缺失。

## 数据说明

检查记录存在插件数据里，键为 `history`。插件只读取媒体服务器数据、写入 MoviePilot 订阅，
不会修改媒体库内容。

图标 `icons/episodenoexist.png` 沿用原插件素材。
