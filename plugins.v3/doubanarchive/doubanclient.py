"""豆瓣接口：负责搜索条目、维护登录态（ck）以及写入书影音档案状态。"""

from __future__ import annotations

import base64
import json
from http.cookies import SimpleCookie
from typing import Any, Dict, Optional, Tuple

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
        """判断当前是否具备写入豆瓣的登录态。"""
        return bool(self.cookies)

    def search(self, title: str, media_type: str = "TV") -> Tuple[Optional[str], Optional[str]]:
        """
        搜索豆瓣影视条目，返回 (条目标题, subject_id)。
        优先使用 MP 内置的 frodo API（与「豆瓣想看」同通道，反爬宽松），
        不可用时回退网页搜索。
        """
        result = self._search_by_frodo(title, media_type)
        if result is not None:
            return result
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
        """回退方案：解析豆瓣网页搜索结果。"""
        url = f"https://www.douban.com/search?cat=1002&q={title}"
        try:
            response = RequestUtils(headers=self._build_base_headers(), timeout=self._timeout).get_res(url)
        except Exception as error:
            logger.error(f"豆瓣网页搜索 {title} 失败：{error}")
            return None, None
        if response is None or response.status_code != 200:
            logger.error(f"豆瓣网页搜索 {title} 失败：无响应或状态码异常")
            return None, None

        try:
            from bs4 import BeautifulSoup
        except Exception:
            logger.error("豆瓣网页搜索需要 BeautifulSoup，当前环境不可用")
            return None, None

        soup = BeautifulSoup(response.text, "html.parser")
        for div in soup.select("div.result"):
            link = div.find("a", href=True)
            if not link or "subject" not in link["href"]:
                continue
            subject_id = ""
            parts = [part for part in link["href"].rstrip("/").split("/") if part]
            if len(parts) >= 2 and parts[-2] == "subject":
                subject_id = parts[-1]
            if subject_id.isdigit():
                return link.get_text(strip=True) or title, subject_id
        return None, None

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
