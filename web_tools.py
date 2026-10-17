# -*- coding: utf-8 -*-
"""web_search / web_fetch —— 联网搜索与网页正文抓取（本地工具，2026-10-17 米爸需求）。

背景：模型知识有截止日期，问「特斯拉最新的飞行汽车」这类时效性问题答不了。
本模块给 Agent 一条从互联网获取最新消息的通道（read-only，无需人工确认）。

引擎选择（2026-10-17 实测本机网络）：
- 主引擎 Bing（cn.bing.com → www.bing.com 回退）：国内可达、结果保真
  （⚠️ 必须用 urlencode 完整参数 + Accept-Language 头——实测 + 号拼接且无
  语言头时 cn.bing 会把查询截断到第一个词，搜「特斯拉 飞行汽车」只回「特」）；
- DuckDuckGo：本机代理 502 不可达（备用，通的环境自动生效）；
- 百度：本机 SSL 被中断（备用，通的环境自动生效）。

执行通道：标准库 urllib（.venv 无 requests/bs4，不引第三方）；代理跟随环境变量
（企业代理下走代理，清了 env 就直连，与平台其他出网组件同口径）。
"""
import http.cookiejar
import html as _html
import re
import urllib.parse
import urllib.request

__all__ = ["exec_tool", "WEB_TOOL_NAMES"]

WEB_TOOL_NAMES = ("web_search", "web_fetch")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
_TIMEOUT_S = 15

#: 会话化 opener（带 cookie）
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    return _OPENER.open(req, timeout=_TIMEOUT_S).read().decode("utf-8", "ignore")


def _strip_tags(fragment: str) -> str:
    txt = re.sub(r"<[^>]+>", "", fragment)
    return _html.unescape(txt).strip()


def _bing_rss(xml: str, limit: int) -> list:
    """Bing 官方 RSS 输出（format=rss）：结构化、无反爬降级，主引擎首选。
    （HTML 版 b_algo 会被 Bing 对机器人流量软降级——首页塞无关结果，2026-10-17 实测。）"""
    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S)[:limit]:
        tm = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
        lm = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", it, re.S)
        dm = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", it, re.S)
        out.append({"title": _strip_tags(tm.group(1)) if tm else "",
                    "url": (lm.group(1).strip() if lm else ""),
                    "snippet": _strip_tags(dm.group(1))[:200] if dm else ""})
    return [x for x in out if x["url"]]


def _bing(html: str, limit: int) -> list:
    out = []
    for it in re.findall(r'<li class="b_algo"(.*?)</li>', html, re.S)[:limit]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', it, re.S)
        if not m:
            continue
        url, title = m.group(1), _strip_tags(m.group(2))
        sm = re.search(r"<p[^>]*>(.*?)</p>", it, re.S)
        snip = _strip_tags(sm.group(1))[:200] if sm else ""
        out.append({"title": title, "url": url, "snippet": snip})
    return out


def _ddg(html: str, limit: int) -> list:
    out = []
    links = re.findall(r'<a[^>]*class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
    snips = re.findall(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', html, re.S)
    for i, (url, title) in enumerate(links[:limit]):
        out.append({"title": _strip_tags(title), "url": url,
                    "snippet": _strip_tags(snips[i])[:200] if i < len(snips) else ""})
    return out


def _baidu(html: str, limit: int) -> list:
    out = []
    for m in re.finditer(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        out.append({"title": _strip_tags(m.group(2)), "url": m.group(1), "snippet": ""})
        if len(out) >= limit:
            break
    return out


def _engines(query: str, limit: int):
    # ⚠️ 主引擎 = Bing RSS（format=rss）：官方结构化输出、无反爬降级（2026-10-17 实测
    #   HTML 版 b_algo 会被机器人流量软降级——首页塞无关结果）。
    # ⚠️ 空格必须编码为 %20（urllib.parse.quote）：+ 号拼接曾致查询截词。
    qp = urllib.parse.quote(query)
    yield ("bing-rss", f"https://www.bing.com/search?q={qp}&format=rss", _bing_rss)
    yield ("bing", f"https://www.bing.com/search?q={qp}", _bing)
    yield ("bing-cn", f"https://cn.bing.com/search?q={qp}&mkt=zh-CN&setlang=zh-hans", _bing)
    yield ("ddg", f"https://lite.duckduckgo.com/lite?q={qp}", _ddg)
    yield ("baidu", f"https://www.baidu.com/s?wd={qp}", _baidu)


def _authentic(items: list, query: str) -> bool:
    """结果保真校验：cn.bing 对无 cookie 的程序化请求会**偶发降级**——把查询截断到
    第一个词（实测搜「特斯拉 飞行汽车」回「特」字字典，2026-10-17）。
    ⚠️ 校验必须打在**结果条目**上而非整页——降级页的相关搜索推荐区也可能含查询词，
    整页校验会误放行（实测踩坑）。条目 title/snippet 里任一 ≥2 字实词出现才算可信。"""
    terms = [w for w in re.split(r"[\s,，。？?！!]+", query) if len(w) >= 2]
    if not terms or not items:
        return bool(items)
    for it in items:
        blob = (it.get("title") or "") + (it.get("snippet") or "")
        if any(t in blob for t in terms):
            return True
    return False


def _api_search(query: str, limit: int) -> dict | None:
    """配置化的正式搜索 API 通道（免费抓取链的上一级，配了 key 就优先走）。

    config: [web_search] provider = tavily | bocha ；api_key = ...
    - tavily:  POST https://api.tavily.com/search（国际，免费档 1000 次/月）
    - bocha:   POST https://api.bochaai.com/v1/web-search（国内博查，对标 Bing API）
    未配置返回 None（静默落免费链）。API 恒保真，无降级问题。
    """
    try:
        from core import config as _cfg
        provider = str(_cfg.get("web_search", "provider", "") or "").strip().lower()
        key = str(_cfg.get("web_search", "api_key", "") or "").strip()
    except Exception:
        return None
    if not provider or not key:
        return None
    try:
        import json as _json
        if provider == "tavily":
            req = urllib.request.Request(
                "https://api.tavily.com/search",
                data=_json.dumps({"api_key": key, "query": query,
                                  "max_results": limit, "search_depth": "basic"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            data = _json.loads(_OPENER.open(req, timeout=_TIMEOUT_S).read().decode("utf-8", "ignore"))
            items = [{"title": r.get("title", ""), "url": r.get("url", ""),
                      "snippet": str(r.get("content", ""))[:200]}
                     for r in (data.get("results") or [])][:limit]
        elif provider == "bocha":
            req = urllib.request.Request(
                "https://api.bochaai.com/v1/web-search",
                data=_json.dumps({"query": query, "summary": True, "count": limit}).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                method="POST")
            data = _json.loads(_OPENER.open(req, timeout=_TIMEOUT_S).read().decode("utf-8", "ignore"))
            pages = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
            items = [{"title": r.get("name", ""), "url": r.get("url", ""),
                      "snippet": str(r.get("summary") or r.get("snippet", ""))[:200]}
                     for r in pages][:limit]
        else:
            return None
        if not items:
            return {"ok": False, "result": f"{provider} API 返回空结果"}
        lines = [f"[{provider} api · {len(items)} 条结果]"]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {it['title']}\n   {it['url']}"
                         + (f"\n   {it['snippet']}" if it["snippet"] else ""))
        lines.append("（信息来自互联网搜索，注意核对时效与来源）")
        return {"ok": True, "result": "\n".join(lines)}
    except Exception as e:    # noqa: BLE001
        return {"ok": False, "result": f"{provider} API 调用失败: {type(e).__name__}: {str(e)[:120]}"}


def web_search(args: dict) -> dict:
    """联网搜索。优先配置化 API（保真），落免费抓取链（多引擎+保真校验+退避重试）。"""
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "result": "搜索失败：query 不能为空"}
    try:
        limit = max(1, min(int(args.get("max_results") or 5), 10))
    except Exception:
        limit = 5
    _api = _api_search(query, limit)
    if _api is not None:
        return _api
    _errs = []
    # 两轮引擎循环：Bing 对代理出口 IP 有**间歇性降级**（同一 URL 时好时坏，
    # 2026-10-17 实测），第一轮全败后隔 3s 重试一轮（只重试 Bing 系）。
    import time as _time
    for _round in range(2):
        for name, url, parser in _engines(query, limit):
            if _round == 1 and name not in ("bing", "bing-cn"):
                continue    # 第二轮只重试主引擎
            try:
                html = _get(url)
                items = parser(html, limit)
                if not items:
                    _errs.append(f"{name}: 无结果")
                    continue
                if not _authentic(items, query):
                    _errs.append(f"{name}: 结果与查询不符（引擎降级）")
                    continue
                lines = [f"[{name} · {len(items)} 条结果]"]
                for i, it in enumerate(items, 1):
                    lines.append(f"{i}. {it['title']}\n   {it['url']}"
                                 + (f"\n   {it['snippet']}" if it["snippet"] else ""))
                lines.append("（信息来自互联网搜索，注意核对时效与来源）")
                return {"ok": True, "result": "\n".join(lines)}
            except Exception as e:    # noqa: BLE001
                _errs.append(f"{name}: {type(e).__name__}: {str(e)[:60]}")
        if _round == 0:
            _time.sleep(3)
    return {"ok": False,
            "result": ("搜索失败（各引擎均不可达或无结果）：" + "；".join(_errs)[:400]
                       + "。网络可能受限，请如实告知用户无法联网检索，不要编造最新消息。")}


def web_fetch(args: dict) -> dict:
    """抓取网页正文（去 script/style，按段落聚合文本）。异常不抛出。"""
    url = str(args.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "result": "抓取失败：url 必须以 http(s):// 开头"}
    try:
        limit = max(500, min(int(args.get("max_chars") or 6000), 20000))
    except Exception:
        limit = 6000
    try:
        html = _get(url)
        html = re.sub(r"<(script|style|noscript|svg|nav|footer)[^>]*>.*?</\1>", " ", html,
                      flags=re.S | re.I)
        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = _strip_tags(title_m.group(1))[:120] if title_m else ""
        paras = [_strip_tags(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", html, re.S)]
        paras = [p for p in paras if len(p) >= 20]
        body = "\n".join(paras)[:limit] if paras else _strip_tags(html)[:limit]
        if not body.strip():
            return {"ok": False, "result": "抓取成功但未提取到正文（页面可能是纯脚本渲染），可换搜索结果里的其他链接。"}
        return {"ok": True, "result": f"[{title or url}]\n{body}"}
    except Exception as e:    # noqa: BLE001
        return {"ok": False, "result": f"抓取失败: {type(e).__name__}: {str(e)[:150]}（网络受限或页面不可达，请如实说明）"}


_HANDLERS = {"web_search": web_search, "web_fetch": web_fetch}


def exec_tool(name: str, arguments: dict | None = None) -> dict:
    """统一入口（对齐 file_tools.exec_file_tool 风格）：异常不抛出，返回 {"ok", "result"}。"""
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "result": f"未知联网工具: {name}"}
    try:
        return handler(arguments or {})
    except Exception as e:    # noqa: BLE001
        return {"ok": False, "result": f"联网工具执行失败: {str(e)[:200]}"}


WEB_TOOL_DEFS = [
    {"name": "web_search",
     "description": ("联网搜索互联网最新消息（多引擎：Bing→DuckDuckGo→百度自动回退）；"
                     "参数 query:str, max_results:int 可选(默认5)；时效性问题（最新/今天/新闻）必用"),
     "version": "v1.0", "side_effect": "read", "risk_level": "low",
     "input_schema": {"type": "object",
                      "properties": {"query": {"type": "string", "description": "搜索关键词"},
                                     "max_results": {"type": "integer", "description": "返回条数，默认5"}},
                      "required": ["query"]}},
    {"name": "web_fetch",
     "description": ("抓取指定网页的正文文本（去脚本/样式，提取段落）；"
                     "参数 url:str, max_chars:int 可选(默认6000)；配合 web_search 的结果链接深读"),
     "version": "v1.0", "side_effect": "read", "risk_level": "low",
     "input_schema": {"type": "object",
                      "properties": {"url": {"type": "string", "description": "网页地址"},
                                     "max_chars": {"type": "integer", "description": "正文截断长度，默认6000"}},
                      "required": ["url"]}},
]
