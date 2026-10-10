"""豆瓣接口：负责搜索条目、维护登录态（ck）以及写入书影音档案状态。"""

from __future__ import annotations

import base64
import json
from http.cookies import SimpleCookie
from typing import Any, Dict, Optional, Set, Tuple

from app.sdk.logging import logger
from app.sdk.network import RequestUtils

# 豆瓣接口地址
_DOUBAN_HOME = "https://www.douban.com/"
_DOUBAN_INTEREST = "https://movie.douban.com/j/subject/%s/interest"

# 状态取值：do=在看，collect=看过，wish=想看
_STATUS_ALIAS = {
    "do": "do",
    "doing": "do",
    "wish": "wish",
    "collect": "collect",
    "done": "collect",
}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0"
)


class DoubanClient:
    """封装豆瓣搜索与状态写入，所有请求显式超时，失败返回空值而不抛异常。"""

    def __init__(self, cookie: str = "", timeout: int = 15) -> None:
        """
        :param cookie: 豆瓣 cookie 字符串，可为空（空时仅能搜索，不能写入）
        :param timeout: 网络请求超时秒数
        """
        self._timeout = timeout
        self.cookies: Dict[str, str] = self._parse_cookie(cookie)
        self._ck = self.cookies.get("ck") or ""

    # ---------------- 对外能力 ----------------

    def has_login(self) -> bool:
        """
        判断当前 cookie 是否具备写入豆瓣书影音档案的能力。

        判据是「有 ck」而非「有 login_flag」：写入接口
        `j/subject/{id}/interest` 认的是 ck（antispam 凭证），
        没有 login_flag、db_sid 一样能写成功（实测返回 {"r":0}）。
        login_flag / db_sid 只影响网页搜索，缺它们只是搜不到条目，
        不影响写入，所以不能拿它们当登录判据。
        """
        if not self.cookies:
            return False
        return "ck" in self._cookie_keys()

    def _cookie_keys(self) -> Set[str]:
        """返回当前 cookie 的键名集合。"""
        return {item.split("=", 1)[0].strip() for item in self.cookie_header().split(";")}

    def can_search(self) -> bool:
        """
        判断当前 cookie 是否能用于豆瓣搜索。

        搜索接口（网页搜索与 subject_abstract）要求 login_flag、db_sid，
        缺任一则返回空结果。写入不依赖这些，所以与 has_login 分开判断。
        """
        if not self.cookies:
            return False
        required = ("login_flag", "db_sid")
        keys = self._cookie_keys()
        return all(key in keys for key in required)

    def diagnose_login(self) -> Dict[str, Any]:
        """
        诊断 cookie 状态，分别报告「能否写入」与「能否搜索」。

        两者条件不同：写入认 ck，搜索认 login_flag + db_sid。
        排查时先看 can_write，写入正常但 imported=0 就要看 can_search。
        """
        keys = self._cookie_keys()
        search_required = ("login_flag", "db_sid")
        missing = [key for key in search_required if key not in keys]
        return {
            "cookie_count": len(keys),
            "can_write": self.has_login(),
            "has_ck": "ck" in keys,
            "can_search": not missing,
            "missing_search_keys": missing,
            "hint": "写入正常；缺 " + "、".join(missing) + "，豆瓣搜索不可用，"
                    "新条目需依赖媒体服务器刮削的豆瓣 ID" if missing else "",
        }

    def search(self, title: str, media_type: str = "TV") -> Tuple[Optional[str], Optional[str]]:
        """
        搜索豆瓣影视条目，返回 (条目标题, subject_id)。
        优先使用 MP 内置的 frodo API（与「豆瓣想看」同通道，反爬宽松），
        不可用时回退网页搜索。

        注意：底层实现失败时返回的是 (None, None) 元组而非 None，
        因此必须判断 subject_id 是否为空，否则会拿到空结果却不再回退。
        """
        subject_name, subject_id = self._search_by_frodo(title, media_type)
        if subject_id:
            return subject_name, subject_id
        return self._search_by_web(title)

    def set_status(self, subject_id: str, status: str = "do", private: bool = True) -> bool:
        """
        写入豆瓣书影音档案状态。
        :param subject_id: 豆瓣条目 ID
        :param status: do/在看、collect/看过、wish/想看
        :param private: 是否仅自己可见
        """
        if not self.cookies:
            logger.error("豆瓣 cookie 为空，无法写入状态，请检查插件配置或 CookieCloud")
            return False

        interest = _STATUS_ALIAS.get(status, "do")
        if not self._ck:
            self._refresh_ck()

        payload = {
            "ck": self._ck,
            "interest": interest,
            "rating": "",
            "foldcollect": "U",
            "tags": "",
            "comment": "",
        }
        if private:
            payload["private"] = "on"

        return self._post_interest(subject_id, payload, retry=True)

    def get_subject_image(self, subject_id: str, media_type: str = "TV") -> Optional[str]:
        """读取豆瓣条目的海报地址，取不到返回 None。"""
        try:
            from app.modules.douban.apiv2 import DoubanApi
        except Exception:
            return None

        try:
            api = DoubanApi()
            detail = api.tv_detail(subject_id) if media_type == "TV" else api.movie_detail(subject_id)
        except Exception as error:
            logger.debug(f"读取豆瓣条目 {subject_id} 详情失败：{error}")
            return None

        if not isinstance(detail, dict):
            return None
        pic = detail.get("pic")
        if isinstance(pic, dict):
            for key in ("large", "normal", "medium", "small"):
                value = pic.get(key)
                if value:
                    return str(value)
        for key in ("pic", "cover", "cover_url"):
            value = detail.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        return None

    def fetch_image_data_uri(self, url: str, prefer_small: bool = True) -> Optional[str]:
        """
        把豆瓣图片转成 data URI。
        豆瓣图片有防盗链：浏览器带着本站 Referer 去取会返回 403，
        因此由后端带上豆瓣自己的 Referer 取回后内联，前端不再直接访问豆瓣。
        """
        if not url or "doubanio.com" not in url:
            return None
        candidates = []
        if prefer_small:
            # 海报墙只需要小图，取小图能显著降低仪表盘数据体积
            small = url.replace("/l_ratio_poster/", "/s_ratio_poster/") \
                       .replace("/m_ratio_poster/", "/s_ratio_poster/")
            if small != url:
                candidates.append(small)
        candidates.append(url)
        for target in candidates:
            result = self._download_image(target)
            if result:
                return result
        return None

    # ---------------- 内部实现 ----------------

    def _download_image(self, url: str) -> Optional[str]:
        """下载单张图片并转成 data URI，失败返回 None。"""
        headers = self._build_base_headers()
        headers["Referer"] = "https://movie.douban.com/"
        try:
            response = RequestUtils(headers=headers, timeout=self._timeout).get_res(url)
        except Exception as error:
            logger.debug(f"下载豆瓣图片异常：{error}")
            return None
        if response is None or response.status_code != 200:
            return None
        content = getattr(response, "content", None)
        if not content or len(content) > 2 * 1024 * 1024:
            return None
        content_type = (response.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        return f"data:{content_type};base64," + base64.b64encode(content).decode("ascii")

    def _post_interest(self, subject_id: str, payload: Dict[str, Any], retry: bool = False) -> bool:
        """提交状态写入请求，403 时刷新 ck 后重试一次。"""
        headers = self._build_headers(subject_id)
        try:
            response = RequestUtils(headers=headers, timeout=self._timeout).post_res(
                _DOUBAN_INTEREST % subject_id, data=payload)
        except Exception as error:
            logger.error(f"写入豆瓣状态异常：{error}")
            return False

        if response is None:
            logger.error(f"写入豆瓣状态失败：无响应（可能被拦截或超时）")
            return False

        if response.status_code == 403 and retry:
            logger.warn("豆瓣返回 403，刷新 ck 后重试")
            self._refresh_ck()
            payload["ck"] = self._ck
            return self._post_interest(subject_id, payload, retry=False)

        if response.status_code != 200:
            logger.error(f"写入豆瓣状态失败，状态码：{response.status_code}")
            return False

        try:
            result = response.json()
        except Exception:
            logger.error("豆瓣响应解析失败")
            return False

        if result.get("r") == 0:
            return True
        if result.get("r") is False:
            logger.error(f"豆瓣条目 {subject_id} 可能未开播，无法标记")
            return False
        logger.error(f"豆瓣写入返回异常：{result}")
        return False

    def _refresh_ck(self) -> None:
        """从豆瓣首页获取 ck，失败时保留旧值，保证登录态不被清空。"""
        old_ck = self._ck
        try:
            response = RequestUtils(headers=self._build_base_headers(), timeout=self._timeout).get_res(_DOUBAN_HOME)
        except Exception as error:
            logger.error(f"刷新豆瓣 ck 失败：{error}")
            return
        if response is None:
            logger.error("刷新豆瓣 ck 失败：无响应")
            return

        cookie = SimpleCookie()
        try:
            cookie.load(response.headers.get("Set-Cookie", ""))
        except Exception:
            cookie = SimpleCookie()

        ck = cookie.get("ck")
        if ck and ck.value and ck.value != '"deleted"':
            self._ck = ck.value
            self.cookies["ck"] = ck.value
        elif not old_ck:
            self._ck = ""

    def _search_by_frodo(self, title: str, media_type: str) -> Optional[Tuple[Optional[str], Optional[str]]]:
        """使用 frodo API 搜索，接口不可用返回 None，无结果返回 (None, None)。"""
        try:
            from app.modules.douban.apiv2 import DoubanApi
        except Exception as error:
            logger.debug(f"豆瓣 frodo API 不可用，回退网页搜索：{error}")
            return None

        try:
            api = DoubanApi()
            result = api.tv_search(title) if media_type == "TV" else api.movie_search(title)
        except Exception as error:
            logger.warn(f"豆瓣 API 搜索 {title} 失败，回退网页搜索：{error}")
            return None

        for item in self._iter_items(result):
            subject_id = self._pick_text(item, "id")
            subject_title = self._pick_text(item, "title")
            if subject_id:
                logger.debug(f"豆瓣命中：{subject_title} {subject_id}")
                return subject_title or title, subject_id
        logger.info(f"豆瓣未找到「{title}」对应的影视条目")
        return None, None

    def _search_by_web(self, title: str) -> Tuple[Optional[str], Optional[str]]:
        """
        回退方案：解析豆瓣网页搜索结果。

        www.douban.com/search 目前会跳转到登录页（未登录时返回的是登录页 HTML，
        解析不出任何条目），因此先走 movie 域的搜索页，再退回 www 域。
        """
        for url in (
            f"https://movie.douban.com/subject_search?search_text={title}&cat=1002",
            f"https://www.douban.com/search?cat=1002&q={title}",
        ):
            subject_id = self._search_one_page(url)
            if subject_id:
                return title, subject_id
        return None, None

    def _search_one_page(self, url: str) -> Optional[str]:
        """抓取单个搜索页并解析出首个条目 ID，失败返回 None。"""
        try:
            response = RequestUtils(headers=self._build_base_headers(),
                                    timeout=self._timeout).get_res(url)
        except Exception as error:
            logger.debug(f"豆瓣搜索页请求失败：{error}")
            return None
        if response is None or response.status_code != 200:
            logger.debug(f"豆瓣搜索页状态码异常：{getattr(response, 'status_code', None)}")
            return None

        text = response.text or ""
        # 未登录会被跳到登录页，此时页面里不会有条目链接
        if "login" in url or 'name="cookie"' in text or "登录" in text[:2000] and "/subject/" not in text:
            logger.debug("豆瓣搜索页要求登录，放弃该通道")
            return None

        try:
            from bs4 import BeautifulSoup
        except Exception:
            logger.debug("豆瓣网页搜索需要 BeautifulSoup，当前环境不可用")
            return None

        soup = BeautifulSoup(text, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            parts = [item for item in href.rstrip("/").split("/") if item]
            if len(parts) >= 2 and parts[-2] == "subject" and parts[-1].isdigit():
                return parts[-1]
        return None

    @staticmethod
    def _iter_items(result: Any):
        """兼容不同版本的 frodo 返回结构，逐个产出条目对象。"""
        if not isinstance(result, dict):
            return []
        items = result.get("items")
        if not isinstance(items, list):
            return []
        targets = []
        for item in items:
            if not isinstance(item, dict):
                continue
            target = item.get("target")
            targets.append(target if isinstance(target, dict) else item)
        return targets

    @staticmethod
    def _pick_text(data: Dict[str, Any], key: str) -> Optional[str]:
        """读取并清洗条目字段。"""
        value = data.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _parse_cookie(cookie: str) -> Dict[str, str]:
        """把 cookie 字符串（支持 key=value; 形式或 JSON）解析为字典。"""
        if not cookie:
            return {}
        text = cookie.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items() if v}
            except Exception:
                pass
        try:
            parsed = SimpleCookie()
            parsed.load(text)
            return {k: v.value for k, v in parsed.items()}
        except Exception:
            return {}

    def _build_base_headers(self) -> Dict[str, str]:
        """构造豆瓣通用请求头。"""
        return {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
            "Cookie": self.cookie_header(),
        }

    def _build_headers(self, subject_id: str) -> Dict[str, str]:
        """构造状态写入请求头。"""
        headers = self._build_base_headers()
        headers.update({
            "Referer": f"https://movie.douban.com/subject/{subject_id}/",
            "Origin": "https://movie.douban.com",
            "Host": "movie.douban.com",
        })
        return headers

    def cookie_header(self) -> str:
        """把当前 cookie 拼成请求头字符串。"""
        return ";".join(f"{key}={value}" for key, value in self.cookies.items())
