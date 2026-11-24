#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日 AI & 全球资讯早报 (Daily AI & Global Digest)
--------------------------------------------------
自动化多源数据抓取 -> 大模型智能提炼 (GPT-6.2-sol / 本地网关) -> 邮件与微信卡片推送

核心功能板块：
1. 🌍 全球时事（直白讲清来龙去脉与实际影响，附权威来源）
2. 🤖 AI 核心御三家 & 科技四巨头深度追踪（OpenAI/Codex, Google/Gemini, Anthropic/Claude + NVIDIA/Tesla/华为/苹果）
3. 🔥 GitHub 开源项目寻宝（今日飙升热榜 + 每日宝藏精选，大白话支招）
"""

import os
import sys
import re
import json
import time
import hashlib
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Set, Iterable

# 保证在各类控制台（尤其 Windows GBK 默认终端）下输出中文与 Emoji 不发生 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 确保在便携 Python 环境下也能顺畅载入 .venv 中的第三方依赖库
_venv_site = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "Lib", "site-packages")
if os.path.isdir(_venv_site) and _venv_site not in sys.path:
    sys.path.insert(0, _venv_site)

import requests
from bs4 import BeautifulSoup
import feedparser
from dotenv import load_dotenv

# 邮件发送标准库
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

# 加载 .env 环境变量
load_dotenv()

# ==================== 全局配置 ====================
BEIJING_TZ = timezone(timedelta(hours=8))
PREBUILT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prebuilt")
HISTORY_FILE = os.path.join(PREBUILT_DIR, "history_seen.json")
HISTORY_KEEP_DAYS = 30


def init_network_proxy():
    """智能探测本地代理端口 (7897 / 7890)。若未连通则自动清空代理变量，避免 requests 抛 ProxyError"""
    proxy_env = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
    ports_to_check = []
    if proxy_env:
        m = re.search(r":(\d+)", proxy_env)
        if m:
            try:
                ports_to_check.append(int(m.group(1)))
            except ValueError:
                pass
    ports_to_check.extend([7897, 7890])

    working_port = None
    import socket
    for p in dict.fromkeys(ports_to_check):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.6)
            res = s.connect_ex(("127.0.0.1", p))
            s.close()
            if res == 0:
                working_port = p
                break
        except Exception:
            pass

    if working_port:
        proxy_url = f"http://127.0.0.1:{working_port}"
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        print(f"🌐 [代理探测] 本地代理已接入: {proxy_url}")
    else:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "all_proxy", "ALL_PROXY"):
            os.environ.pop(k, None)
        print("🌐 [代理探测] 本地代理端口未开放，已自动清空代理变量走直连")


# 初始化网络代理探测
init_network_proxy()


def load_prebuilt_digest(date_str: str) -> Optional[str]:
    path = os.path.join(PREBUILT_DIR, f"{date_str}.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        return text if len(text) >= 800 else None
    except OSError:
        return None


def report_now() -> datetime:
    """早报署名日期；预生成模式下通过 REPORT_DATE_OFFSET_DAYS=1 写明天的日期"""
    offset = int((os.getenv("REPORT_DATE_OFFSET_DAYS") or "0").strip() or 0)
    return datetime.now(BEIJING_TZ) + timedelta(days=offset)


def normalize_title(title: str) -> str:
    """标准化标题，用于模糊去重匹配"""
    t = re.sub(r"\[.*?\]|\(.*?\)|###|\*\*|[0-9]+[.:、]", "", title)
    t = re.sub(r"[\s\-_—：:，,。！？!?（）()\"'“”‘’]+", "", t)
    return t.lower()


def is_title_duplicate(title: str, seen_titles: Optional[Iterable[str]], threshold: float = 0.65) -> bool:
    """判断新标题是否与已报道历史标题高度相似（包含/前缀/文本相似度比对）"""
    if not seen_titles or not title:
        return False
    norm = normalize_title(title)
    if len(norm) < 4:
        return False
    import difflib
    for st in seen_titles:
        st_norm = normalize_title(st)
        if len(st_norm) < 4:
            continue
        if (norm in st_norm or st_norm in norm) and min(len(norm), len(st_norm)) >= 6:
            return True
        min_len = min(len(norm), len(st_norm))
        if min_len >= 8 and norm[:8] == st_norm[:8]:
            return True
        if difflib.SequenceMatcher(None, norm, st_norm).ratio() >= threshold:
            return True
    return False


def scan_recent_history(days: int = 5, exclude_date: Optional[str] = None) -> Dict[str, Any]:
    """
    扫描 prebuilt/ 目录下最近 days 天的 .md 文件与 history_seen.json：
    提取已报道的 URL、GitHub repo 以及核心标题，返回去重集合与 Prompt 黑名单摘要。
    """
    os.makedirs(PREBUILT_DIR, exist_ok=True)
    seen_urls: Set[str] = set()
    seen_repos: Set[str] = set()
    seen_titles: List[str] = []
    daily_summaries: List[Dict[str, Any]] = []

    # 1. 扫描 prebuilt/*.md 文件
    all_files = sorted(Path(PREBUILT_DIR).glob("*.md"), reverse=True)
    valid_files = []
    for fp in all_files:
        date_name = fp.stem
        if exclude_date and date_name == exclude_date:
            continue
        valid_files.append(fp)
        if len(valid_files) >= days:
            break

    for fp in valid_files:
        date_str = fp.stem
        try:
            content = fp.read_text(encoding="utf-8")
        except Exception:
            continue

        file_urls = re.findall(r"\[(?:[^\]]+)\]\((https?://[^\s)]+)\)", content)
        file_repos = re.findall(r"github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", content)
        file_titles = re.findall(r"###\s+(?:\d+\.\s*)?([^\n]+)", content)

        clean_repos = set()
        for r in file_repos:
            r_clean = r.rstrip("/").lower()
            parts = r_clean.split("/")
            if len(parts) == 2 and parts[0] not in ("topics", "features", "collections", "trending", "search", "events"):
                clean_repos.add(r_clean)

        clean_titles = [t.strip() for t in file_titles if len(t.strip()) >= 5]
        clean_urls = {u.strip() for u in file_urls if u.strip()}

        seen_urls.update(clean_urls)
        seen_repos.update(clean_repos)
        seen_titles.extend(clean_titles)

        daily_summaries.append({
            "date": date_str,
            "titles": clean_titles[:8],
            "repos": sorted(list(clean_repos)),
            "urls_count": len(clean_urls)
        })

    # 2. 读取并合并 history_seen.json（记录 7 天）
    history_data = {}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history_data = json.load(f)
        except Exception as e:
            print(f"  [提示] 读取 history_seen.json 异常: {e}")

    entries = history_data.get("entries", {})
    now_dt = report_now()
    cutoff_str = (now_dt - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    news_cutoff_str = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d")

    pruned_entries = {}
    for d_str, d_info in entries.items():
        if d_str >= cutoff_str:
            pruned_entries[d_str] = d_info
            if exclude_date and d_str == exclude_date:
                continue
            # GitHub 项目按 30 天去重；新闻链接与标题只看 7 天，避免模糊匹配误杀新进展
            for r in d_info.get("repos", []):
                seen_repos.add(r.lower())
            if d_str >= news_cutoff_str:
                seen_urls.update(d_info.get("urls", []))
                seen_titles.extend(d_info.get("titles", []))

    # 构建注入到 Prompt 中的黑名单精简摘要
    blacklist_lines = []
    for day_info in daily_summaries[:4]:
        d = day_info["date"]
        titles_str = "；".join(day_info["titles"][:5])
        blacklist_lines.append(f"- 【{d} 已发要闻】: {titles_str}")
    if seen_repos:
        blacklist_lines.append(f"- 【近 {HISTORY_KEEP_DAYS} 天已推荐开源仓库】: {', '.join(sorted(seen_repos))}")

    blacklist_summary = "\n".join(blacklist_lines) if blacklist_lines else "暂无近期历史黑名单"

    return {
        "seen_urls": seen_urls,
        "seen_repos": seen_repos,
        "seen_titles": set(seen_titles),
        "blacklist_summary": blacklist_summary,
        "history_entries": pruned_entries
    }


def get_sent_timestamp(date_str: str) -> Optional[str]:
    """查询指定日期是否已经成功发送过早报邮件（用于多触发源幂等防重）"""
    if not os.path.exists(HISTORY_FILE):
        return None
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history_data = json.load(f)
        entry = (history_data.get("entries") or {}).get(date_str) or {}
        sent_at = (entry.get("sent_at") or "").strip()
        return sent_at or None
    except Exception:
        return None


def record_history_seen(date_str: str, content: str, mark_sent: bool = False):
    """把当天生成的早报内容持久化追加或更新至 prebuilt/history_seen.json"""
    try:
        urls = set(re.findall(r"\[(?:[^\]]+)\]\((https?://[^\s)]+)\)", content))
        raw_repos = re.findall(r"github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", content)
        repos = set()
        for r in raw_repos:
            r_clean = r.rstrip("/").lower()
            parts = r_clean.split("/")
            if len(parts) == 2 and parts[0] not in ("topics", "features", "collections", "trending", "search", "events"):
                repos.add(r_clean)

        raw_titles = re.findall(r"###\s+(?:\d+\.\s*)?([^\n]+)", content)
        titles = [t.strip() for t in raw_titles if len(t.strip()) >= 5]

        history_data = {"entries": {}}
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    history_data = json.load(f)
            except Exception:
                pass

        entries = history_data.get("entries", {})
        prev_entry = entries.get(date_str) or {}
        now_iso = datetime.now(BEIJING_TZ).isoformat()
        sent_at = now_iso if mark_sent else prev_entry.get("sent_at")

        new_entry: Dict[str, Any] = {
            "urls": sorted(list(urls)),
            "repos": sorted(list(repos)),
            "titles": titles[:15],
            "updated_at": now_iso,
        }
        if sent_at:
            new_entry["sent_at"] = sent_at
        entries[date_str] = new_entry

        cutoff_str = (report_now() - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
        entries = {k: v for k, v in entries.items() if k >= cutoff_str}
        history_data["entries"] = entries

        os.makedirs(PREBUILT_DIR, exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        sent_note = f", 已标记推送时间 {sent_at[11:19]}" if sent_at else ""
        print(f"📁 已更新历史去重记录库: {HISTORY_FILE} (当天记录 {len(repos)} 个仓库, {len(urls)} 个链接{sent_note})")
    except Exception as e:
        print(f"  [警告] 更新 history_seen.json 异常: {e}")


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 15

# ==================== 工具函数 ====================
def clean_html_text(raw_html: str, max_len: int = 500) -> str:
    """清理 HTML 标签并截断长文本，默认保留 500 字符丰富上下文"""
    if not raw_html:
        return ""
    # 用 BeautifulSoup 剥离标签
    soup = BeautifulSoup(raw_html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    # 合并连续空白字符
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return text


_GITHUB_TOKEN_CACHE: Optional[str] = None


def github_token() -> str:
    """GitHub API Token：环境变量（云端 Actions 自带）-> 本地 .gh_token.json；无则匿名（每小时仅 60 次）"""
    global _GITHUB_TOKEN_CACHE
    if _GITHUB_TOKEN_CACHE is not None:
        return _GITHUB_TOKEN_CACHE
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if not token:
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gh_token.json")
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            token = (data.get("access_token") or data.get("token") or "").strip()
        except Exception:
            token = ""
    _GITHUB_TOKEN_CACHE = token
    return token


def http_get(url: str, params: Optional[dict] = None, extra_headers: Optional[dict] = None) -> Optional[requests.Response]:
    """统一的网络请求封装，附带标准浏览器 User-Agent 和超时控制"""
    headers = {"User-Agent": USER_AGENT}
    if url.startswith("https://api.github.com") and github_token():
        headers["Authorization"] = f"Bearer {github_token()}"
    if extra_headers:
        headers.update(extra_headers)
    # 429（限流）与 5xx 属于临时故障：退避后重试，最多 3 次；其余状态码直接放弃
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                return resp
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if retryable and attempt < max_attempts:
                retry_after = resp.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else 2 ** attempt
                time.sleep(min(wait, 15))
                continue
            print(f"  [警告] 请求失败 ({resp.status_code}): {url}")
            return None
        except Exception as e:
            if attempt < max_attempts:
                time.sleep(2 ** attempt)
                continue
            print(f"  [异常] 网络请求错误: {url} -> {e}")
            return None
    return None


def fetch_rss_feed(url: str, source_name: str, max_items: int = 6) -> List[Dict[str, str]]:
    """通用的 RSS 抓取与解析函数，先通过 requests 获取以防 feedparser 默认 UA 被拦截"""
    items = []
    print(f"  -> 正在抓取 RSS: {source_name} ...")
    resp = http_get(url)
    if not resp:
        return items

    try:
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:max_items]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            
            # 若源名称包含 Google News 或检索词，尝试从标题末尾提取真实发布机构名
            cleaned_source = source_name
            if ("Google News" in source_name or "检索" in source_name) and " - " in title:
                parts = title.rsplit(" - ", 1)
                if len(parts) == 2 and 1 < len(parts[1].strip()) <= 25:
                    title = parts[0].strip()
                    cleaned_source = parts[1].strip()

            # 优先提取 content 内容，若无则使用 summary 或 description
            raw_content = ""
            if entry.get("content") and isinstance(entry["content"], list):
                raw_content = entry["content"][0].get("value", "")
            if not raw_content:
                raw_content = entry.get("summary") or entry.get("description", "")

            summary = clean_html_text(raw_content, max_len=650)
            # Google News RSS 的 summary 往往只是把标题+来源重复一遍，没有任何正文信息；清空以免误导模型硬凑废话
            norm_t = normalize_title(title)
            norm_s = normalize_title(summary)
            if norm_t and norm_s and norm_s.startswith(norm_t) and (len(norm_s) - len(norm_t) <= 20):
                summary = ""

            pub_date = entry.get("published", "") or entry.get("updated", "")

            if title and link:
                items.append({
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "date": pub_date,
                    "source": cleaned_source
                })
        print(f"     ✅ 成功获取 {len(items)} 条资讯 ({source_name})")
    except Exception as e:
        print(f"     ❌ 解析 RSS 失败 ({source_name}): {e}")

    return items


def fetch_hacker_news_algolia(max_items: int = 6) -> List[Dict[str, str]]:
    """
    通过 Algolia HN Search API 稳定采集高赞 AI/科技要闻，彻底替代经常 SSLEOFError 的 hnrss
    """
    print("  -> 正在通过 Algolia HN API 抓取科技深度要闻...")
    items: List[Dict[str, str]] = []

    # 方案 1: search_by_date 检索近期高分 AI 话题
    try:
        url = "https://hn.algolia.com/api/v1/search_by_date"
        params = {
            "tags": "story",
            "query": "AI OR LLM OR OpenAI OR Claude OR GPU OR NVIDIA",
            "numericFilters": "points>30",
            "hitsPerPage": 15
        }
        resp = http_get(url, params=params)
        if resp:
            hits = resp.json().get("hits", [])
            for h in hits:
                title = (h.get("title") or "").strip()
                if not title:
                    continue
                link = (h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}").strip()
                points = h.get("points") or 0
                comments = h.get("num_comments") or 0
                created_at = h.get("created_at") or ""
                summary = f"Hacker News 社区高赞讨论 ({points} points, {comments} 评论)"
                items.append({
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "date": created_at,
                    "source": "Hacker News"
                })
                if len(items) >= max_items:
                    break
    except Exception as e:
        print(f"  [警告] Algolia search_by_date 异常: {e}")

    # 方案 2: 降级回退到 front_page
    if not items:
        print("  [降级触发] 尝试抓取 Hacker News Front Page 热门...")
        try:
            url = "https://hn.algolia.com/api/v1/search"
            params = {"tags": "front_page", "hitsPerPage": 20}
            resp = http_get(url, params=params)
            if resp:
                hits = resp.json().get("hits", [])
                for h in hits:
                    title = (h.get("title") or "").strip()
                    if not title:
                        continue
                    link = (h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}").strip()
                    points = h.get("points") or 0
                    comments = h.get("num_comments") or 0
                    created_at = h.get("created_at") or ""
                    summary = f"Hacker News 首页热门 ({points} points, {comments} 评论)"
                    items.append({
                        "title": title,
                        "link": link,
                        "summary": summary,
                        "date": created_at,
                        "source": "Hacker News"
                    })
                    if len(items) >= max_items:
                        break
        except Exception as e:
            print(f"  [警告] Algolia front_page 异常: {e}")

    print(f"     ✅ 成功获取 {len(items)} 条 Hacker News 深度讨论")
    return items


# ==================== 数据采集模块 ====================
def collect_global_news(
    seen_urls: Optional[Set[str]] = None,
    seen_titles: Optional[Set[str]] = None
) -> List[Dict[str, str]]:
    """采集全球重大时事政经资讯（优先带完整正文摘要的一手权威源 + Google News 补充 + 跨天排重）"""
    print("\n[1/4] 正在采集【全球重大时事】资讯...")
    sources = [
        ("https://feeds.bbci.co.uk/zhongwen/simp/rss.xml", "BBC 中文", 6),
        ("http://feeds.bbci.co.uk/news/world/rss.xml", "BBC World News", 6),
        ("https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "NYTimes World", 5),
        ("https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "Google News (中文热点)", 7),
        ("https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en", "Google News (US Top Stories)", 5),
    ]

    all_news = []
    current_seen_titles = set()

    for url, name, limit in sources:
        feed_items = fetch_rss_feed(url, name, max_items=limit)
        for item in feed_items:
            if seen_urls and item["link"] in seen_urls:
                continue
            if seen_titles and is_title_duplicate(item["title"], seen_titles):
                continue

            title_key = item["title"][:25].lower()
            if title_key not in current_seen_titles:
                current_seen_titles.add(title_key)
                all_news.append(item)

    print(f"  => 全球时事汇总去重后共 {len(all_news)} 条数据底座")
    return all_news


AI_TECH_TARGET_KEYWORDS = re.compile(
    r"openai|gpt|codex|chatgpt|gemini|antigravity|deepmind|google ai|claude|anthropic|"
    r"nvidia|英伟达|黄仁勋|jensen huang|blackwell|rubin|cuda|rtx|"
    r"tesla|特斯拉|fsd|optimus|robotaxi|cybercab|马斯克|elon musk|"
    r"华为|huawei|鸿蒙|harmonyos|麒麟|kirin|mate\s?\d|matebook|matepad|pura|freebuds|"
    r"苹果|apple|iphone|ipad|macbook|airpods|apple watch|ios\s?\d|macos|m5|m6|"
    r"大模型|开源模型|deepseek|qwen|千问|glm",
    re.IGNORECASE,
)


def collect_ai_news(
    seen_urls: Optional[Set[str]] = None,
    seen_titles: Optional[Set[str]] = None
) -> List[Dict[str, str]]:
    """采集 AI 核心御三家（OpenAI/Google/Anthropic）及英伟达/特斯拉/华为/苹果四巨头最新要闻"""
    print("\n[2/4] 正在采集【AI 御三家 & 英伟达/特斯拉/华为/苹果】最新资讯...")
    all_ai_items = []
    current_seen_titles = set()

    def add_filtered_item(it: Dict[str, str]):
        if seen_urls and it["link"] in seen_urls:
            return
        if seen_titles and is_title_duplicate(it["title"], seen_titles):
            return
        t_key = it["title"][:25].lower()
        if t_key not in current_seen_titles:
            current_seen_titles.add(t_key)
            all_ai_items.append(it)

    # 1. 一手官方与带完整正文摘要的主流科技媒体 RSS（避免模型只拿到空标题没细节）
    primary_rich_feeds = [
        ("https://openai.com/news/rss.xml", "OpenAI Official", 5, False),
        ("https://www.ithome.com/rss/", "IT之家", 30, True),
        ("https://www.theverge.com/rss/index.xml", "The Verge", 18, True),
        ("https://feeds.arstechnica.com/arstechnica/technology-lab", "Ars Technica", 12, True),
    ]
    for feed_url, feed_label, limit, need_kw_filter in primary_rich_feeds:
        for it in fetch_rss_feed(feed_url, feed_label, max_items=limit):
            if need_kw_filter and not AI_TECH_TARGET_KEYWORDS.search(f"{it['title']} {it.get('summary', '')}"):
                continue
            add_filtered_item(it)

    # 2. Google News 专项靶向检索（补全各家细分动态）
    targeted_queries = [
        # AI 御三家
        ('("OpenAI" OR "GPT" OR "Codex") when:2d', "OpenAI / GPT / Codex 检索", "en-US", "US:en"),
        ('("Gemini" OR "Antigravity" OR "Google AI") when:2d', "Google Gemini / Antigravity 检索", "en-US", "US:en"),
        ('("Claude" OR "Anthropic") when:2d', "Anthropic Claude 检索", "en-US", "US:en"),
        # 英伟达 (NVIDIA) 算力与芯片生态
        ('("NVIDIA" OR "Jensen Huang" OR "Blackwell" OR "Rubin" OR "CUDA") when:2d', "NVIDIA / 英伟达生态 (Global)", "en-US", "US:en"),
        ('("英伟达" OR "黄仁勋") when:2d', "英伟达 / 黄仁勋 (中文热点)", "zh-CN", "CN:zh-Hans"),
        # 特斯拉 (Tesla) 自动驾驶与具身智能
        ('("Tesla" OR "FSD" OR "Optimus" OR "Robotaxi" OR "Elon Musk") when:2d', "Tesla / 特斯拉智能 (Global)", "en-US", "US:en"),
        ('("特斯拉" OR "FSD" OR "Optimus" OR "马斯克") when:2d', "特斯拉 / FSD / 具身智能 (中文热点)", "zh-CN", "CN:zh-Hans"),
        # 华为 (Huawei) 消费电子
        ('(华为 发布会 OR 华为 新品 OR 华为 官宣) when:3d', "华为 / 发布会与新品", "zh-CN", "CN:zh-Hans"),
        ('(华为Mate OR 华为Pura OR 华为nova OR 华为Pocket OR 麒麟芯片 OR 麒麟9) when:3d', "华为 / 手机与麒麟芯片", "zh-CN", "CN:zh-Hans"),
        ('(MateBook OR MatePad OR FreeBuds OR 华为WATCH OR 华为手表 OR 华为平板) when:3d', "华为 / 电脑平板耳机手表", "zh-CN", "CN:zh-Hans"),
        ('(鸿蒙 OR HarmonyOS OR 鸿蒙电脑 OR 鸿蒙6) when:3d', "华为 / 鸿蒙系统", "zh-CN", "CN:zh-Hans"),
        # 苹果 (Apple) 消费电子
        ('(苹果 发布会 OR 苹果 新品 OR 苹果 官宣) when:3d', "苹果 / 发布会与新品", "zh-CN", "CN:zh-Hans"),
        ('(iPhone OR 苹果A19 OR 苹果A20 OR MacBook OR iPad OR AirPods) when:3d', "苹果 / 硬件新品", "zh-CN", "CN:zh-Hans"),
        ('(iOS OR macOS OR iPadOS OR "Apple Intelligence") 苹果 when:3d', "苹果 / 系统与 Apple Intelligence", "zh-CN", "CN:zh-Hans"),
        ('("iPhone" OR "MacBook" OR "iPad" OR "Apple event" OR "Apple Intelligence") when:2d', "Apple / Devices (Global)", "en-US", "US:en"),
    ]

    for q, label, hl, ceid in targeted_queries:
        encoded_q = urllib.parse.quote(q)
        search_rss_url = f"https://news.google.com/rss/search?q={encoded_q}&hl={hl}&gl=US&ceid={ceid}"
        feed_items = fetch_rss_feed(search_rss_url, label, max_items=4)
        for it in feed_items:
            add_filtered_item(it)
        time.sleep(1.0)

    # 3. Hacker News 高分 AI/科技深度讨论帖
    hn_items = fetch_hacker_news_algolia(max_items=6)
    for it in hn_items:
        add_filtered_item(it)

    print(f"  => AI 核心御三家与英伟达/特斯拉/华为/苹果汇总去重后共 {len(all_ai_items)} 条数据底座")
    return all_ai_items


# 别名兼容
fetch_ai_and_perks_news = collect_ai_news


def collect_github_trending(seen_repos: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """抓取今日 GitHub Trending 热门榜单，自带 GitHub Search API 优雅降级与跨天防重复"""
    print("\n[3/3] 正在抓取【GitHub Trending 今日热门】...")
    trending_url = "https://github.com/trending?since=daily"
    resp = http_get(trending_url)
    projects = []

    normalized_seen_repos = {r.lower() for r in (seen_repos or set())}

    if resp:
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.select("article.Box-row")
            print(f"  -> GitHub Trending HTML 解析成功，共扫描到 {len(rows)} 个项目")

            for row in rows[:25]:
                title_elem = row.select_one("h2 a")
                if not title_elem:
                    continue

                repo_path = title_elem.get("href", "").strip().lstrip("/")
                repo_url = f"https://github.com/{repo_path}"
                name = re.sub(r"\s+", "", title_elem.text)

                if repo_path.lower() in normalized_seen_repos or name.lower() in normalized_seen_repos:
                    print(f"  [跨天去重] 跳过近期已推荐项目: {name}")
                    continue

                desc_elem = row.select_one("p")
                description = desc_elem.text.strip() if desc_elem else "暂无项目描述"

                lang_elem = row.select_one("span[itemprop='programmingLanguage']")
                language = lang_elem.text.strip() if lang_elem else "Unknown"

                stars_today_elem = row.select_one("span.d-inline-block.float-sm-right")
                stars_today = stars_today_elem.text.strip() if stars_today_elem else ""
                if not stars_today:
                    match = re.search(r"(\d[\d,]*\s+stars?\s+today)", row.text)
                    stars_today = match.group(1) if match else "N/A"

                total_stars_elem = row.select_one("a[href$='/stargazers']")
                total_stars = total_stars_elem.text.strip() if total_stars_elem else "N/A"

                projects.append({
                    "name": name,
                    "url": repo_url,
                    "description": description,
                    "language": language,
                    "stars_today": stars_today,
                    "total_stars": total_stars
                })

            if projects:
                # 优先将贴近创作者/桌面软件/AI提效/音视频的项目排在前面，避免前 8 个全是纯底层编译器或冷门库
                creator_pat = re.compile(
                    r"video|audio|voice|tts|subtitle|whisper|clip|image|photo|camera|raw|ocr|pdf|"
                    r"markdown|obsidian|note|desktop|windows|gui|app|clipboard|screenshot|screen|"
                    r"transfer|sync|media|assistant|agent|skill|prompt|workflow|automat|browser",
                    re.IGNORECASE,
                )
                projects.sort(
                    key=lambda p: (0 if creator_pat.search(f"{p['name']} {p['description']}") else 1)
                )
                print(f"  => 成功提取 GitHub Trending {len(projects)} 个项目 (已按创作者实用度优先排序)")
                return projects
        except Exception as e:
            print(f"  ❌ 解析 GitHub Trending HTML 发生异常: {e}")

    # Fallback: 使用 GitHub Search API 获取最近 7 天内新成立的高星热点项目
    print("  [降级触发] 启动 GitHub Search API 备用方案...")
    try:
        now_bj = datetime.now(BEIJING_TZ)
        seven_days_ago = (now_bj - timedelta(days=7)).strftime("%Y-%m-%d")
        api_url = f"https://api.github.com/search/repositories?q=created:>{seven_days_ago}&sort=stars&order=desc&per_page=15"
        api_resp = http_get(api_url)
        if api_resp:
            data = api_resp.json()
            for repo in data.get("items", []):
                full_name = repo.get("full_name", "")
                if full_name.lower() in normalized_seen_repos:
                    continue
                projects.append({
                    "name": full_name,
                    "url": repo.get("html_url", ""),
                    "description": repo.get("description", "暂无描述"),
                    "language": repo.get("language", "Unknown"),
                    "stars_today": f"+{repo.get('stargazers_count', 0)} (近期飙升)",
                    "total_stars": str(repo.get("stargazers_count", 0))
                })
                if len(projects) >= 10:
                    break
            print(f"  => GitHub Search API 备用方案成功抓取 {len(projects)} 个飙升项目")
    except Exception as e:
        print(f"  ❌ GitHub Search API 降级方案亦失败: {e}")

    return projects


# ==================== 每日经典宝藏项目池 (贴合创作者与桌面效率场景) ====================
CLASSIC_QUERY_POOL = [
    {"topic": "生产力与效率工具", "query": "stars:8000..150000 topic:productivity", "sort": "stars"},
    {"topic": "自动化工作流利器", "query": "stars:5000..90000 topic:automation", "sort": "stars"},
    {"topic": "桌面端装机实用软件", "query": "stars:8000..120000 desktop app in:readme,description", "sort": "stars"},
    {"topic": "音视频处理与剪辑工具", "query": "stars:5000..100000 video editor OR subtitle in:description,readme", "sort": "stars"},
    {"topic": "语音识别与配音工具", "query": "stars:4000..80000 speech OR tts OR audio in:description", "sort": "stars"},
    {"topic": "Windows 桌面效率增强", "query": "stars:4000..80000 windows utility OR windows app in:description,readme", "sort": "stars"},
    {"topic": "照片管理与图像增强", "query": "stars:4000..90000 photo OR image upscaler in:description,readme", "sort": "stars"},
    {"topic": "自托管与家庭媒体中心", "query": "stars:8000..120000 topic:self-hosted", "sort": "stars"},
    {"topic": "OCR 文字识别与文档提取", "query": "stars:4000..70000 ocr in:description,readme", "sort": "stars"},
    {"topic": "Markdown 与知识管理", "query": "stars:6000..100000 markdown in:description", "sort": "stars"},
    {"topic": "浏览器增强与效率扩展", "query": "stars:5000..80000 browser extension in:description", "sort": "stars"},
    {"topic": "AI 创作与生产力桌面端", "query": "stars:8000..150000 ai desktop OR ai assistant in:description,readme", "sort": "stars"},
    {"topic": "视觉设计与白板绘图", "query": "stars:6000..100000 whiteboard OR design tool in:description", "sort": "stars"},
    {"topic": "高速下载与跨端传输", "query": "stars:6000..90000 file transfer OR downloader in:description,readme", "sort": "stars"},
    {"topic": "数据备份与同步工具", "query": "stars:5000..80000 backup OR sync in:description", "sort": "stars"},
    {"topic": "PDF 处理与格式转换", "query": "stars:6000..90000 pdf in:description", "sort": "stars"},
    {"topic": "本地文件秒级搜索", "query": "stars:6000..120000 file search in:description,readme", "sort": "stars"},
    {"topic": "双链笔记与个人外脑", "query": "stars:6000..100000 note-taking OR obsidian in:description,readme", "sort": "stars"},
    {"topic": "新一代文件管理器", "query": "stars:6000..90000 file-manager in:description", "sort": "stars"},
    {"topic": "剪贴板历史与效率工具", "query": "stars:4000..60000 clipboard in:description", "sort": "stars"},
    {"topic": "截图与屏幕录制工具", "query": "stars:6000..90000 screenshot OR screen recorder in:description", "sort": "stars"},
    {"topic": "局域网跨设备互传", "query": "stars:5000..90000 airdrop OR lan share in:description,readme", "sort": "stars"},
    {"topic": "字幕生成与视频翻译", "query": "stars:3500..80000 subtitle OR video translation in:description,readme", "sort": "stars"},
    {"topic": "本地密码管理保险箱", "query": "stars:7000..100000 password manager in:description", "sort": "stars"},
]

LOCAL_CLASSIC_GEMS_FALLBACK = [
    {"name": "localsend/localsend", "url": "https://github.com/localsend/localsend", "description": "开源跨平台局域网文件互传工具，免配置打通 iPhone、鸿蒙/安卓与 Windows 电脑秒传大文件。", "language": "Dart", "total_stars": "56000+", "topic": "跨平台局域网传输"},
    {"name": "mifi/lossless-cut", "url": "https://github.com/mifi/lossless-cut", "description": "免重编码无损秒剪长视频素材的桌面工具，粗剪大体积录屏与相机原片只需几秒。", "language": "TypeScript", "total_stars": "45000+", "topic": "无损极速粗剪"},
    {"name": "hiroi-sora/Umi-OCR", "url": "https://github.com/hiroi-sora/Umi-OCR", "description": "免费开源离线批量图片与 PDF 文字识别桌面软件，解压即用无需联网。", "language": "Python/C++", "total_stars": "32000+", "topic": "离线截图与文档 OCR"},
    {"name": "excalidraw/excalidraw", "url": "https://github.com/excalidraw/excalidraw", "description": "手绘风白板绘图工具，适合画视频分镜草图、对比图与思维导图。", "language": "TypeScript", "total_stars": "85000+", "topic": "手绘风白板设计"},
    {"name": "obsproject/obs-studio", "url": "https://github.com/obsproject/obs-studio", "description": "免费开源屏幕录制与直播推流软件，支持多机位画面合成与显卡硬件编码。", "language": "C/C++", "total_stars": "62000+", "topic": "高清录屏与直播"},
    {"name": "Stirling-Tools/Stirling-PDF", "url": "https://github.com/Stirling-Tools/Stirling-PDF", "description": "本地部署的全功能 PDF 合并、拆分、压缩、转图片工具箱。", "language": "Java", "total_stars": "52000+", "topic": "全功能 PDF 工具箱"},
    {"name": "syncthing/syncthing", "url": "https://github.com/syncthing/syncthing", "description": "点对点文件夹自动同步工具，在多台电脑平板间直连同步，不经过第三方网盘。", "language": "Go", "total_stars": "67000+", "topic": "多设备自动同步"},
    {"name": "pot-app/pot-desktop", "url": "https://github.com/pot-app/pot-desktop", "description": "跨平台划词翻译与截图翻译桌面小工具，支持接入多种大模型接口。", "language": "Rust", "total_stars": "14000+", "topic": "桌面划词与截图翻译"},
    {"name": "ShareX/ShareX", "url": "https://github.com/ShareX/ShareX", "description": "Windows 平台老牌免费截图、长图拼接、录 GIF 与自动化标注工具。", "language": "C#", "total_stars": "29000+", "topic": "截图与录屏标注"},
    {"name": "HandBrake/HandBrake", "url": "https://github.com/HandBrake/HandBrake", "description": "跨平台视频格式转换与体积压制软件，支持显卡硬件加速压缩大视频。", "language": "C", "total_stars": "18000+", "topic": "视频压制转换"},
    {"name": "upscayl/upscayl", "url": "https://github.com/upscayl/upscayl", "description": "开箱即用的本地显卡加速 AI 老照片与模糊图片高清放大桌面软件。", "language": "TypeScript", "total_stars": "34000+", "topic": "AI 图片无损放大"},
    {"name": "EcoPasteHub/EcoPaste", "url": "https://github.com/EcoPasteHub/EcoPaste", "description": "轻量跨平台剪贴板历史管理工具，本地存储复制过的文字与图片，随取随用。", "language": "Rust/TypeScript", "total_stars": "6000+", "topic": "剪贴板历史管理"},
    {"name": "rustdesk/rustdesk", "url": "https://github.com/rustdesk/rustdesk", "description": "开源跨平台远程桌面工具，支持手机平板远程控制电脑并自建中继。", "language": "Rust", "total_stars": "78000+", "topic": "跨端远程桌面"},
    {"name": "gorhill/uBlock", "url": "https://github.com/gorhill/uBlock", "description": "轻量低占用的浏览器广告与弹窗拦截扩展，提升网页浏览清爽度。", "language": "JavaScript", "total_stars": "47000+", "topic": "浏览器广告拦截"},
    {"name": "keepassxreboot/keepassxc", "url": "https://github.com/keepassxreboot/keepassxc", "description": "纯本地加密存储的开源密码管理器，不依赖云端即可安全管理各平台账号密码。", "language": "C++", "total_stars": "24000+", "topic": "本地密码保险箱"},
    {"name": "Flow-Launcher/Flow.Launcher", "url": "https://github.com/Flow-Launcher/Flow.Launcher", "description": "Windows 桌面快捷启动与秒搜文件工具，按下快捷键即可快速搜文件、算算术、开软件。", "language": "C#", "total_stars": "10000+", "topic": "Windows 快捷启动器"}
]


def fetch_github_classic_gems(exclude_names: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """
    抓取板块三“每日经典宝藏项目”候选池：
    1. 基于北京时间日期 YYYY-MM-DD 计算确定性哈希种子，从 32 个领域查询池中轮换抽取 2 个主题
    2. 调用 GitHub Search API 检索高分殿堂级项目，过滤掉已在今日 Trending 中的项目
    3. 挑选 6~8 个候选高分宝藏项目传给大模型做深度推荐
    4. 自带 22+ 本地精选项目切片轮换兜底，防止 GitHub API 限流
    """
    print("\n[4/4] 正在检索【GitHub 每日经典宝藏/殿堂级项目】候选池...")
    if exclude_names is None:
        exclude_names = set()
    else:
        exclude_names = {n.lower() for n in exclude_names}

    now_bj = datetime.now(BEIJING_TZ)
    date_str = now_bj.strftime("%Y-%m-%d")
    seed = int(hashlib.md5(date_str.encode("utf-8")).hexdigest()[:8], 16)

    pool_len = len(CLASSIC_QUERY_POOL)
    idx1 = seed % pool_len
    idx2 = (seed // pool_len + 7) % pool_len
    if idx2 == idx1:
        idx2 = (idx1 + 1) % pool_len

    selected_queries = [CLASSIC_QUERY_POOL[idx1], CLASSIC_QUERY_POOL[idx2]]
    print(f"  🎯 今日宝藏探索主题: 1.【{selected_queries[0]['topic']}】 2.【{selected_queries[1]['topic']}】 (确定性日期种子: {seed})")

    candidate_gems: List[Dict[str, Any]] = []
    seen_repo_names = set()

    for q_item in selected_queries:
        topic_label = q_item["topic"]
        q_text = q_item["query"]
        encoded_q = urllib.parse.quote(q_text)
        api_url = f"https://api.github.com/search/repositories?q={encoded_q}&sort={q_item['sort']}&order=desc&per_page=10"

        resp = http_get(api_url)
        if resp:
            try:
                data = resp.json()
                for repo in data.get("items", [])[:8]:
                    full_name = repo.get("full_name", "")
                    fn_lower = full_name.lower()
                    if not full_name or fn_lower in exclude_names or fn_lower in seen_repo_names:
                        continue

                    seen_repo_names.add(fn_lower)
                    desc = repo.get("description") or "暂无项目描述"
                    lang = repo.get("language") or "Unknown"
                    stargazers = repo.get("stargazers_count", 0)

                    candidate_gems.append({
                        "name": full_name,
                        "url": repo.get("html_url", f"https://github.com/{full_name}"),
                        "description": desc,
                        "language": lang,
                        "total_stars": f"{stargazers:,}",
                        "stars_today": "经典高分宝藏",
                        "topic_category": topic_label
                    })
                    if len(candidate_gems) >= 8:
                        break
            except Exception as e:
                print(f"  [警告] 解析宝藏搜索结果异常 ({topic_label}): {e}")

        if len(candidate_gems) >= 8:
            break

    # 兜底补充逻辑：若因网络或 GitHub API 限流导致候选少于 4 个，从本地兜底池切片轮换补充
    if len(candidate_gems) < 6:
        print(f"  [宝藏池兜底机制] API 候选数量较少 ({len(candidate_gems)} 个)，启动本地精选殿堂级项目切片补充...")
        fallback_len = len(LOCAL_CLASSIC_GEMS_FALLBACK)
        start_offset = seed % fallback_len
        for i in range(fallback_len):
            fb_item = LOCAL_CLASSIC_GEMS_FALLBACK[(start_offset + i) % fallback_len]
            fn_lower = fb_item["name"].lower()
            if fn_lower not in exclude_names and fn_lower not in seen_repo_names:
                seen_repo_names.add(fn_lower)
                candidate_gems.append({
                    "name": fb_item["name"],
                    "url": fb_item["url"],
                    "description": fb_item["description"],
                    "language": fb_item["language"],
                    "total_stars": fb_item["total_stars"],
                    "stars_today": "经典高分宝藏",
                    "topic_category": fb_item.get("topic", "精选神器")
                })
            if len(candidate_gems) >= 8:
                break

    print(f"  => 成功就绪 {len(candidate_gems)} 个高分经典宝藏候选项目供大模型精编")
    return candidate_gems


# 别名兼容
collect_github_classic_gems = fetch_github_classic_gems


# ==================== GitHub 项目实时资料补全 ====================
README_CAVEAT_PATTERN = re.compile(
    r"alpha|beta|experimental|early|preview|not yet|work in progress|\bWIP\b|unstable|breaking|"
    r"deprecated|archived|unmaintained|no longer|fork|limitation|known issue|caveat|warning|"
    r"requires?|requirement|prerequisite|GPU|VRAM|CUDA|\bRAM\b|memory|docker|self-host|"
    r"windows|macos|linux|android|\bios\b|install|download|subscription|pricing|paid|free|license",
    re.IGNORECASE,
)


def clean_readme_text(raw: str) -> Dict[str, Any]:
    """把 README 原文清洗成：开头正文摘要 + 状态徽章 + 含限制/门槛关键词的句子"""
    badges = []
    for alt in re.findall(r'alt="([^"]+)"', raw) + re.findall(r"!\[([^\]]+)\]\(", raw):
        if re.search(r"status|alpha|beta|stable|experimental|maintained|deprecated", alt, re.IGNORECASE):
            badges.append(alt.strip())

    text = re.sub(r"<!--.*?-->", " ", raw, flags=re.DOTALL)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip(" #>*-|`")
        if len(line) >= 4:
            lines.append(line)

    intro = " ".join(lines)[:900]
    caveats = []
    for line in lines[8:]:
        if len(line) > 25 and README_CAVEAT_PATTERN.search(line) and line not in caveats:
            caveats.append(line[:220])
        if len(caveats) >= 8:
            break
    return {"badges": badges[:4], "intro": intro, "caveat_lines": caveats}


def detect_asset_platforms(asset_names: List[str]) -> List[str]:
    rules = [
        ("Windows", r"\.exe$|\.msi$|\.msix|win(dows)?[-_.]|win64|x86_64-pc-windows"),
        ("macOS", r"\.dmg$|\.pkg$|mac(os)?|darwin|apple"),
        ("Linux", r"\.appimage$|\.deb$|\.rpm$|\.flatpak|linux"),
        ("Android", r"\.apk$|android"),
        ("iOS", r"\.ipa$|[-_.]ios[-_.]"),
    ]
    found = []
    for label, pat in rules:
        if any(re.search(pat, n, re.IGNORECASE) for n in asset_names):
            found.append(label)
    return found


def fetch_repo_live_info(full_name: str) -> Dict[str, Any]:
    """实时拉取仓库最新状态：元数据 + 最新 Release + README（默认分支最新版），不使用任何缓存"""
    info: Dict[str, Any] = {"fetched_at": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")}
    api = f"https://api.github.com/repos/{full_name}"

    meta_resp = http_get(api)
    if meta_resp:
        m = meta_resp.json()
        pushed = (m.get("pushed_at") or "")[:10]
        created = (m.get("created_at") or "")[:10]
        info.update({
            "stars": m.get("stargazers_count"),
            "created_at": created,
            "last_code_push": pushed,
            "archived": bool(m.get("archived")),
            "is_fork": bool(m.get("fork")),
            "license": (m.get("license") or {}).get("spdx_id") or "未声明",
            "open_issues": m.get("open_issues_count"),
            "homepage": m.get("homepage") or "",
            "topics": (m.get("topics") or [])[:8],
            "description": m.get("description") or "",
        })
        try:
            now_d = datetime.now(BEIJING_TZ).date()
            if created:
                info["project_age_days"] = (now_d - datetime.strptime(created, "%Y-%m-%d").date()).days
            if pushed:
                info["days_since_last_push"] = (now_d - datetime.strptime(pushed, "%Y-%m-%d").date()).days
        except ValueError:
            pass

    rel_resp = http_get(f"{api}/releases", params={"per_page": 10})
    releases = [r for r in (rel_resp.json() if rel_resp else []) if not r.get("draft")]
    if releases:
        r0 = releases[0]
        asset_names = [a.get("name", "") for a in r0.get("assets", [])]
        info["latest_release"] = {
            "tag": r0.get("tag_name"),
            "published_at": (r0.get("published_at") or "")[:10],
            "is_prerelease": bool(r0.get("prerelease")),
            "installer_platforms": detect_asset_platforms(asset_names),
            "asset_count": len(asset_names),
            "release_notes_excerpt": clean_html_text(r0.get("body") or "", 400),
        }
        stable = next((r for r in releases if not r.get("prerelease")), None)
        if stable and stable is not r0:
            info["latest_stable_release"] = {
                "tag": stable.get("tag_name"),
                "published_at": (stable.get("published_at") or "")[:10],
                "installer_platforms": detect_asset_platforms([a.get("name", "") for a in stable.get("assets", [])]),
            }
    else:
        info["latest_release"] = "无正式发布版本（没有现成安装包，通常需要自己从源码部署）"

    readme_resp = http_get(f"{api}/readme", extra_headers={"Accept": "application/vnd.github.raw"})
    if readme_resp:
        info["readme"] = clean_readme_text(readme_resp.text)
    info["hard_facts"] = build_hard_facts(info)
    return info


def build_hard_facts(info: Dict[str, Any]) -> List[str]:
    """由代码直接判定的硬性事实，模型必须原样体现，不得淡化"""
    facts = []
    if info.get("archived"):
        facts.append("仓库已被作者归档（archived），彻底停止维护")
    days = info.get("days_since_last_push")
    if isinstance(days, int) and days > 180:
        facts.append(f"已经 {days} 天没有更新代码，属于长期没人维护，必须如实写明")
    age = info.get("project_age_days")
    if isinstance(age, int) and age < 90:
        facts.append(f"项目创建才 {age} 天，非常新，可能不稳定")
    badges = " ".join((info.get("readme") or {}).get("badges", []))
    intro = (info.get("readme") or {}).get("intro", "")
    m = re.search(r"\b(early alpha|alpha|beta|experimental|preview)\b", badges + " " + intro[:400], re.IGNORECASE)
    if m:
        facts.append(f"README 自己标注为 {m.group(1)}（测试/早期版本）")
    if re.search(r"(community )?fork of|no longer maintained|unmaintained|abandoned", intro, re.IGNORECASE):
        facts.append("README 提到项目是别人接手的分支或原项目已停止维护")
    rel = info.get("latest_release")
    if isinstance(rel, str):
        facts.append("没有发布任何安装包，只能自己从源码部署")
    elif isinstance(rel, dict):
        stable = info.get("latest_stable_release")
        if rel.get("is_prerelease"):
            facts.append(f"最新版本 {rel.get('tag')} 是预发布（测试）版")
        platforms = set(rel.get("installer_platforms") or [])
        if stable:
            platforms |= set(stable.get("installer_platforms") or [])
        if "Windows" not in platforms:
            facts.append(
                "最近的发布版本里没有 Windows 安装包，不能写“下载安装即用”"
                + (f"（现有安装包平台：{'/'.join(sorted(platforms))}）" if platforms else "")
            )
    return facts


def enrich_github_projects(projects: List[Dict[str, Any]], label: str) -> List[Dict[str, Any]]:
    """并发为候选项目补全实时资料，写入 project['live_info']"""
    from concurrent.futures import ThreadPoolExecutor

    def worker(p: Dict[str, Any]):
        name = p.get("name", "").strip().strip("/")
        if name.count("/") != 1:
            return
        try:
            p["live_info"] = fetch_repo_live_info(name)
        except Exception as e:
            print(f"  [警告] 补全 {name} 实时资料失败: {e}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, projects))
    ok = sum(1 for p in projects if p.get("live_info", {}).get("readme"))
    print(f"  => 【{label}】已实时补全 {ok}/{len(projects)} 个项目的 README / 最新版本 / 维护状态")
    return projects


def load_reader_profile() -> str:
    """读者画像：云端从 Secret READER_PROFILE 读取（仓库公开，不提交文件），本地读 reader_profile.md"""
    env_profile = (os.getenv("READER_PROFILE") or "").strip()
    if env_profile:
        return env_profile
    fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reader_profile.md")
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


# ==================== 大模型提炼核心模块 ====================
SYSTEM_PERSONA = (
    "你是我的私人科技与时事早报搭子——既懂技术与国际大势，又特别会说人话。"
    "你写早报就像两个熟人早上端着咖啡聊天：直白、清晰、有重点、好理解，绝不端架子打官腔，也绝不拽晦涩术语。"
    "【头号文风红线】：严禁使用“不是…而是…”、“不只是…更是…”、“A不等于B”、“不能据此推导出…”、"
    "“不宜把…理解为…”、“需要分开看/不能混为一谈”这类否定式说教句和免责车轱辘话！"
    "直接把「发生了什么、为什么会有这事、有什么实在看点或影响」痛痛快快讲明白。"
    "必须按顺序完整输出三大板块，严禁截断，严禁输出任何编读说明或免责声明。"
)


def format_gpt_label(model: str, effort: str) -> str:
    m_disp = model.replace("gpt-", "GPT-") if model.lower().startswith("gpt-") else model
    return f"{m_disp} ({effort.capitalize()})"


def build_digest_prompt(
    global_news: List[Dict[str, str]],
    ai_news: List[Dict[str, str]],
    github_projects: List[Dict[str, Any]],
    github_classic_gems: Optional[List[Dict[str, Any]]] = None,
    blacklist_summary: str = ""
) -> str:
    """构建通俗、自然、拒绝说教腔的早报提炼 Prompt"""
    now_bj = report_now()
    date_str = now_bj.strftime("%Y年%m月%d日")
    weekday_map = {0: "星期一", 1: "星期二", 2: "星期三", 3: "星期四", 4: "星期五", 5: "星期六", 6: "星期日"}
    weekday_str = weekday_map[now_bj.weekday()]

    if github_classic_gems is None:
        github_classic_gems = []

    context_data = {
        "report_date": f"{date_str} {weekday_str} (北京时间)",
        "raw_global_news": global_news[:16],
        "raw_ai_news": ai_news[:85],
        "raw_github_trending": github_projects[:8],
        "raw_github_classic_gems": github_classic_gems[:6]
    }
    context_json = json.dumps(context_data, ensure_ascii=False, indent=2)

    reader_profile = load_reader_profile()
    profile_section = f"""
### 🙋 读者画像（供板块三判断项目实不实用，切忌生搬硬套）
{reader_profile}
""" if reader_profile else ""

    blacklist_section = ""
    if blacklist_summary and blacklist_summary.strip() != "暂无近期历史黑名单":
        blacklist_section = f"""
### 🚫 近期已报道事件与开源项目黑名单（严禁炒冷饭）：
{blacklist_summary}

- 同一事件除非有实打实的新进展（标题写明“最新进展”），否则一律跳过；
- 黑名单里的 GitHub 仓库绝对不许重复推荐。
"""

    prompt = f"""
请基于下方的【原始数据底座】，帮我整理今天的早报（在手机微信和邮箱里看）。

### 📅 今日日期
{date_str} {weekday_str}

### 📦 原始数据底座 (JSON)
{context_json}
{profile_section}
{blacklist_section}
---

### 🗣️ 语言风格总原则（最重要，决定整篇质量！）：
1. **像熟人聊天一样说人话**：用自然、顺畅、一读就懂的现代口语化书面语，把复杂事情讲简单。读起来要像一个懂行的好朋友直接告诉你重点，不要像公文、社论或律所免责声明。
2. **彻底戒掉“否定说教体 / 杠精句式”**：
   - **全文严禁扎堆出现** `“不是A，而是B”`、`“不只是…更是…”`、`“A不等于B”`、`“不能据此认定/推导出…”`、`“不宜把…直接理解为…”`、`“需要把X和Y分开看”` 这类句式！
   - 读者没有误解的事，不要自己立个稻草人去反驳或泼冷水。比如减刑就是“改判终身监禁”，直接写“把 548 名死囚改判为终身监禁”就一清二楚，不要写“减刑为终身监禁，不是释放”；测试版功能直接写“苹果在 iOS 测试版里试水某某功能”，不要绕弯写“代码中的测试内容不等于已正式发布，也不保证最终上线”。
3. **有料多聊细节，没料干脆简写，绝不注水**：
   - 数据底座里有具体数字、产品型号、前因后果的，把干货细节写出来；
   - 如果某条新闻原始数据里只有简短标题快讯、缺乏细节，就痛快用两句话把已知动态交代清楚，**绝对不要为了凑字数去写“后续值得观察、需要权衡多方利益”这种正确的废话**。
4. **标题直接说事，别故弄玄虚**：小标题直接点明“谁干了什么/出了什么事”，不要写抽象的议论文题目。
5. **严禁元说明**：开头导语之后直接进板块一，绝不输出“依据数据底座整理”、“未联网核实”等任何废话。

---

### 📝 三大板块结构与要求：

#### 🌅 主标题：`# 每日 AI & 全球资讯早报 | {date_str} {weekday_str}`
紧跟一句 `> 💡 **今日速览**：...`（40~60 字，用大白话点出今天最值得看的 2~3 件大事）。

#### 🌍 板块一：全球时事
- 挑过去 24 小时最关键的 **4~5 条**国际政经、科技或社会大事。
- 每条用 `### 序号. 直白事件标题` 开头，正文写 **1~2 个简练自然段（120~180 字左右）**：自然串起「到底发生了啥 → 为什么会闹出这事（背景脉络） → 接下来会带来什么实在影响」，不用生硬拆分小标签。
- 末尾附上可点击来源：`[🔗 来源：媒体名](链接)`。

#### 🤖 板块二：AI 核心御三家 & 科技四巨头深度追踪
- **宁缺毋滥**：哪家今天没有实质新动向，就直接写一句“今日无重要新动态”（或把篇幅留给今天有重磅发布的其他前沿 AI 实验室如 DeepSeek、通义千问、智谱等），**严禁拿官网首页链接或套话凑数**。
- 固定分两个子部分：
  1. `### 🧠 AI 大模型与工具御三家（GPT/Codex、Gemini/Antigravity、Claude）`
     - 聚焦 OpenAI、Google、Anthropic 的新模型、新功能、API 价格/能力变化、编程与桌面助手实战动态。讲清楚“更新了啥、实际用起来有什么变化”。
  2. `### ⚡ 科技四巨头（英伟达 NVIDIA / 特斯拉 Tesla / 华为 Huawei / 苹果 Apple）`
     - **英伟达**：AI 芯片（Blackwell/Rubin）、显卡、CUDA 生态、黄仁勋与核心业务大事。
     - **特斯拉**：FSD 自动驾驶、Optimus 机器人、Robotaxi 量产与落地进展。
     - **华为 & 苹果**：只聊消费电子产品（手机、平板、电脑、耳机、手表、自家芯片、鸿蒙 / iOS / macOS 系统与 AI 新功能）。有具体型号、配置、价格、发布时间就写清楚；早期传闻在标题前加 `【爆料】` 即可，不用反复叠甲。
- 每条末尾附真实来源链接 `[🔗 查看原文：媒体名](链接)`。

#### 🔥 板块三：GitHub 开源项目寻宝
- **黑名单铁律**：近 {HISTORY_KEEP_DAYS} 天推过的仓库一律跳过。
- **把读者当不懂写代码的朋友**：如果正文不得不提技术词，第一次出现时顺口用括号大白话解释一句（例：`Docker（相当于在电脑里开个独立小隔间来装软件，不弄乱主系统）`）。**打比方必须接地气、一眼秒懂**，严禁造出比原词更绕口的比喻！
- **事实以 `live_info` 为准，提醒要真诚自然**：
  - 每个项目的实时资料（README 摘要、最新 Release 安装包平台、最后更新时间、创建天数、`hard_facts`）都是刚从 GitHub 查出来的。资料里没写的功能不脑补，禁用“神器、吊打、完美平替、零门槛、一键丝滑”这类夸张词。
  - `live_info.hard_facts` 里的硬事实（如“没有 Windows 安装包”、“项目刚建 X 天还很早期”、“测试版”、“超过半年没更新”）必须在「🚪 上手门槛」或「⚠️ 提前避坑」里如实告诉读者，但请用**朋友提醒的口吻**自然说出来，不要像背诵免责条款。
- **「🙋 适合我装吗」自然贴合画像，严禁生搬硬套**：
  - 只有当这个项目真的跟读者的日常需求（做数码/摄影视频、剪辑字幕配音、修图管照片、跨设备传文件）或现有方案（Immich 相册、Emby 影视库、Obsidian 笔记、Win11 + RTX 4060 8G + 16G 内存、iPhone + 华为平板）**同赛道或能互补**时，才拿出来对比，说说值不值得折腾；
  - 如果项目本来就跟读者领域不沾边（比如纯程序员查代码用的工具），直接痛快写一句“这是给程序员写代码用的，咱们平时拍片剪辑用不上，看看新鲜就行”，**严禁硬凑“它不能增强你的 Immich，也不能帮你挑废片”这种废话**！

- 固定分两个子部分输出：
  1. `### 📈 今日飙升热榜`（从 `raw_github_trending` 挑 **3 个**，优先挑普通人或自媒体创作者能用上的；纯开发者工具最多选 1 个）：
     格式（每个项目 250~350 字）：
     `### 序号. [项目名](链接) 🌟 今日 +新增Stars / 总Stars | 语言`
     - **📌 一句话说清**：不带术语，一句大白话讲明白它是个啥。
     - **💡 打个比方**：用生活里一听就懂的例子类比它的作用。
     - **🎬 平时能怎么用**：结合 README 支持的真实功能，举 1~2 个具体用法。
     - **🚪 上手门槛**：三选一并顺嘴说明原因——「下载安装即用」（仅限最新发布有 Windows 安装包时可用）/「需要跟着教程敲几行命令」/「需要会编程，仅开发者适用」。
     - **⚠️ 提前避坑**：1~2 条实在提醒（是不是刚建几天的新项目、测试版、缺 Windows 安装包、吃不吃配置、收不收费等）。
     - **🙋 适合我装吗**：「✅ 值得一试」/「🤔 可以先收藏观望」/「❌ 跟你关系不大」三选一，用大白话给个实在建议。

  2. `### 💎 每日宝藏推荐`（从 `raw_github_classic_gems` 挑 **2 个**，优先挑 Windows 桌面能用、对创作或日常提效有帮助的）：
     格式（每个项目 280~380 字）：
     `### 🌟 宝藏推荐 序号：[项目名](链接) 🏆 总Stars | 语言`
     - **📌 一句话说清**：同上。
     - **💡 打个比方**：同上。
     - **🎬 平时能怎么用**：同上。
     - **🆚 和常见软件比**：跟大家平时熟悉的同类软件比，实在说说它好在哪、哪块还不如商业软件。
     - **🚪 上手门槛**：同上，有现成安装包的可提示去项目页右侧 Releases 下载。
     - **⚠️ 提前避坑**：同上。
     - **🙋 适合我装吗**：同上。
"""
    return prompt


def generate_digest_with_local(
    global_news: List[Dict[str, str]],
    ai_news: List[Dict[str, str]],
    github_projects: List[Dict[str, Any]],
    github_classic_gems: Optional[List[Dict[str, Any]]],
    api_key: str,
    blacklist_summary: str = ""
) -> str:
    """调用本机 Antigravity 网关 (OpenAI 兼容)，仅在本机开机且配置 Key 时可用"""
    base_url = (os.getenv("LOCAL_LLM_BASE_URL") or "http://127.0.0.1:8317/v1").strip().rstrip("/")
    model = (os.getenv("LOCAL_LLM_MODEL") or "gemini-4-hydrogen-xhigh").strip()
    prompt = build_digest_prompt(global_news, ai_news, github_projects, github_classic_gems, blacklist_summary=blacklist_summary)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PERSONA},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.5,
        "max_tokens": 16384
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    print(f"  🚀 正在向本地网关发送生成请求 (模型: {model}, 接口: {base_url}) ...")
    t0 = time.time()
    resp = requests.post(f"{base_url}/chat/completions", headers=headers, json=payload,
                         proxies={"http": None, "https": None}, timeout=(10, 600))
    if resp.status_code != 200:
        raise RuntimeError(f"本地网关 HTTP {resp.status_code}: {resp.text[:300]}")
    choices = resp.json().get("choices", [])
    content = (choices[0].get("message", {}).get("content") or "").strip() if choices else ""
    if len(content) < 800:
        raise RuntimeError(f"本地网关返回内容过短 ({len(content)} 字符)")
    print(f"  🎉 本地网关生成成功！耗时: {time.time() - t0:.2f}s, 字符数: {len(content)}")
    return content


def generate_digest_with_gpt_sol(
    global_news: List[Dict[str, str]],
    ai_news: List[Dict[str, str]],
    github_projects: List[Dict[str, Any]],
    github_classic_gems: Optional[List[Dict[str, Any]]],
    api_key: str,
    blacklist_summary: str = ""
) -> tuple:
    """
    调用主力云端 API (默认 gpt-6.2-sol + medium 思考)，使用 /v1/responses 协议。
    返回 (content, engine_label)。
    内置多节点智能切换（本机优先香港直连 ca.memofun.net，云端 Actions 优先 CloudFlare 全球节点 codex-api.aizex.net）、
    最多 3 次指数退避重试，并在重试失败时自动降级非流式单次抓取。
    """
    model = (os.getenv("CODEX_MODEL") or os.getenv("AIZEX_MODEL") or "gpt-6.2-sol").strip()
    effort = (os.getenv("CODEX_EFFORT") or os.getenv("AIZEX_EFFORT") or "medium").strip().lower()
    engine_label = format_gpt_label(model, effort)

    is_github_actions = (os.getenv("GITHUB_ACTIONS") or "").strip().lower() == "true"
    endpoints = []
    explicit_codex_url = (os.getenv("CODEX_BASE_URL") or "").strip().rstrip("/")
    explicit_aizex_url = (os.getenv("AIZEX_BASE_URL") or "").strip().rstrip("/")

    if explicit_codex_url:
        endpoints.append(explicit_codex_url)
    if is_github_actions:
        # 云端美国服务器优先走 CloudFlare 全球节点，香港节点兜底
        for ep in ["https://codex-api.aizex.net/v1", "https://ca.memofun.net/v1", explicit_aizex_url]:
            if ep and ep not in endpoints:
                endpoints.append(ep)
    else:
        # 国内本机优先走推荐香港节点 ca.memofun.net，CloudFlare 节点兜底
        for ep in [explicit_aizex_url, "https://ca.memofun.net/v1", "https://codex-api.aizex.net/v1"]:
            if ep and ep not in endpoints:
                endpoints.append(ep)

    prompt = build_digest_prompt(global_news, ai_news, github_projects, github_classic_gems, blacklist_summary=blacklist_summary)

    base_payload = {
        "model": model,
        "input": prompt,
        "instructions": SYSTEM_PERSONA,
        "reasoning": {"effort": effort},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    max_retries = 3
    last_err = None

    for attempt in range(1, max_retries + 1):
        use_stream = (attempt < 3)
        payload = dict(base_payload)
        payload["stream"] = use_stream

        base_url = endpoints[(attempt - 1) % len(endpoints)]
        endpoint = f"{base_url}/responses"

        stream_desc = "流式 SSE" if use_stream else "非流式完整拉取"
        print(f"  🚀 [尝试 {attempt}/{max_retries}] 正在向云端 API 发送请求 (模型: {model}, thinking={effort}, {stream_desc}, 节点: {base_url}) ...")
        t0 = time.time()

        req_headers = dict(headers)
        req_headers["Accept"] = "text/event-stream" if use_stream else "application/json"

        try:
            try:
                resp = requests.post(endpoint, headers=req_headers, json=payload, timeout=(15, 300), stream=use_stream)
            except requests.exceptions.ProxyError:
                resp = requests.post(endpoint, headers=req_headers, json=payload,
                                     proxies={"http": None, "https": None}, timeout=(15, 300), stream=use_stream)

            if resp.status_code == 400 and "tools" in payload:
                payload.pop("tools", None)
                resp = requests.post(endpoint, headers=req_headers, json=payload, timeout=(15, 300), stream=use_stream)

            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            if not use_stream:
                data = resp.json()
                text_parts = []
                for item in data.get("output", []):
                    if item.get("type") == "message":
                        for c in item.get("content", []):
                            if c.get("type") == "output_text":
                                text_parts.append(c.get("text", ""))
                content = "".join(text_parts).strip()
            else:
                delta_parts = []
                text_parts = []
                data = {}
                stream_error = None
                for raw in resp.iter_lines(decode_unicode=False):
                    if not raw or not raw.startswith(b"data:"):
                        continue
                    chunk = raw[5:].strip()
                    if not chunk or chunk == b"[DONE]":
                        continue
                    try:
                        ev = json.loads(chunk.decode("utf-8"))
                    except Exception:
                        continue
                    ev_type = ev.get("type", "")
                    if ev_type == "response.output_text.delta":
                        delta_parts.append(ev.get("delta", ""))
                    elif ev_type in ("response.completed", "response.incomplete"):
                        data = ev.get("response", {})
                    elif ev_type in ("response.failed", "error"):
                        err_detail = str(ev)
                        stream_error = f"流式返回错误: {err_detail[:250]}"
                        break

                for item in data.get("output", []):
                    if item.get("type") == "message":
                        for c in item.get("content", []):
                            if c.get("type") == "output_text":
                                text_parts.append(c.get("text", ""))

                content = ("".join(text_parts) or "".join(delta_parts)).strip()

                if stream_error:
                    if len(content) >= 1200 and "板块三" in content:
                        print(f"  ⚠️ 流式中途收到结束提示但正文已完整接收 ({len(content)} 字符)，正常采纳。")
                        return content, engine_label
                    raise RuntimeError(stream_error)

            if len(content) < 800:
                raise RuntimeError(f"返回内容过短 ({len(content)} 字符)")

            print(f"  🎉 {engine_label} 生成成功！耗时: {time.time() - t0:.2f}s, 字符数: {len(content)}")
            return content, engine_label

        except Exception as e:
            last_err = e
            err_str = str(e)
            print(f"  ⚠️ [尝试 {attempt}/{max_retries} 失败] {err_str[:160]}")
            if attempt < max_retries:
                backoff = attempt * 3
                print(f"  🔄 触发自动指数退避自愈，等待 {backoff}s 后执行下一次重试...")
                time.sleep(backoff)

    raise RuntimeError(f"云端主力引擎 ({engine_label}) 在 {max_retries} 次尝试后仍未成功: {last_err}")


# 向下兼容别名
generate_digest_with_aizex = generate_digest_with_gpt_sol
generate_digest_with_codex = generate_digest_with_gpt_sol


def extract_engine_label(text: str) -> tuple:
    """
    检查 Markdown 文本开头是否包含 <!-- ENGINE_LABEL: ... --> 元数据注释。
    返回 (clean_text, engine_label)。
    """
    default_model = (os.getenv("CODEX_MODEL") or os.getenv("AIZEX_MODEL") or "gpt-6.2-sol").strip()
    default_effort = (os.getenv("CODEX_EFFORT") or os.getenv("AIZEX_EFFORT") or "medium").strip().lower()
    default_label = format_gpt_label(default_model, default_effort)

    if not text:
        return "", "本地多源数据底座"
    m = re.match(r"^\s*<!--\s*ENGINE_LABEL:\s*([^\n>]+?)\s*-->\s*\n?", text, re.IGNORECASE)
    if m:
        label = m.group(1).strip()
        clean = text[m.end():]
        return clean, label

    if "【大模型接口提示】" in text or "本地多源数据底座为您完整生成" in text:
        return text, "本地多源数据底座"
    if "Gemini" in text[:300]:
        return text, "Gemini 4 Hydrogen"
    return text, default_label


def tag_content_with_engine(content: str, label: str) -> str:
    """在 Markdown 开头打入元数据注释，并在顶部导语处明确标注实际执笔模型"""
    meta_tag = f"<!-- ENGINE_LABEL: {label} -->\n"
    pattern = r"(> 💡 \*\*今日(?:早报)?速览\*\*：[^\n]+)"
    if re.search(pattern, content):
        if "执笔模型" not in content[:600]:
            content = re.sub(pattern, rf"\1（执笔模型：**{label}**）", content, count=1)
    return meta_tag + content


def generate_digest(
    global_news: List[Dict[str, str]],
    ai_news: List[Dict[str, str]],
    github_projects: List[Dict[str, Any]],
    github_classic_gems: Optional[List[Dict[str, Any]]] = None,
    blacklist_summary: str = ""
) -> str:
    """
    高可用自愈容灾调度架构：
    1. 第一级（可选本地网关）：仅本机且配置 LOCAL_LLM_API_KEY 时生效；云端或未配 Key 静默跳过
    2. 第二级（主力云端）：GPT-6.2-sol（多节点智能轮换 + 3 次重试自愈 + CODEX/AIZEX 双 Key 互备）
    3. 终极兜底：本地多源数据底座应急早报生成
    """
    is_github_actions = (os.getenv("GITHUB_ACTIONS") or "").strip().lower() == "true"
    local_key  = (os.getenv("LOCAL_LLM_API_KEY") or "").strip()
    codex_key  = (os.getenv("CODEX_API_KEY") or "").strip()
    aizex_key  = (os.getenv("AIZEX_API_KEY") or "").strip()

    errors = []

    def try_local() -> Optional[tuple]:
        if is_github_actions or not local_key:
            return None
        local_model = (os.getenv("LOCAL_LLM_MODEL") or "gemini-4-hydrogen-xhigh").strip()
        print(f"\n🧠 调度大模型引擎: 本地网关 ({local_model})...")
        try:
            content = generate_digest_with_local(global_news, ai_news, github_projects, github_classic_gems, local_key, blacklist_summary=blacklist_summary)
            return content, local_model
        except Exception as e:
            err = f"本地网关失败: {e}"
            errors.append(err)
            print(f"  [降级触发] {err}，自动切换至云端主力引擎...")
            return None

    def try_gpt_sol() -> Optional[tuple]:
        candidate_keys = []
        for k in [codex_key, aizex_key]:
            if k and k not in candidate_keys:
                candidate_keys.append(k)

        if not candidate_keys:
            errors.append("主力云端: 未配置 CODEX_API_KEY 或 AIZEX_API_KEY")
            return None

        target_model = (os.getenv("CODEX_MODEL") or os.getenv("AIZEX_MODEL") or "gpt-6.2-sol").strip()
        target_effort = (os.getenv("CODEX_EFFORT") or os.getenv("AIZEX_EFFORT") or "medium").strip().lower()
        target_label = format_gpt_label(target_model, target_effort)
        print(f"\n🧠 调度主力云端引擎: {target_label}...")

        for idx, key in enumerate(candidate_keys, 1):
            key_mask = key[:6] + "..." + key[-4:] if len(key) > 12 else "***"
            if len(candidate_keys) > 1:
                print(f"  🔑 使用凭据 [{idx}/{len(candidate_keys)}]: {key_mask}")
            try:
                return generate_digest_with_gpt_sol(global_news, ai_news, github_projects, github_classic_gems, key, blacklist_summary=blacklist_summary)
            except Exception as e:
                err = f"{target_label} (凭据 {key_mask}) 失败: {e}"
                errors.append(err)
                print(f"  [降级触发] {err}")
                if idx < len(candidate_keys):
                    print("  🔄 尝试切换下一个有效 API Key 凭据...")
        return None

    order = [try_local, try_gpt_sol]

    for engine_fn in order:
        res = engine_fn()
        if res:
            content, label = res
            return tag_content_with_engine(content, label)

    combined_reason = " | ".join(errors)
    print(f"\n⚠️ [自动容灾] 所有大模型引擎均未响应，启用本地多源数据底座应急早报生成引擎 (原因: {combined_reason[:150]})...")
    fallback_content = generate_fallback_digest(global_news, ai_news, github_projects, github_classic_gems, reason=combined_reason)
    return tag_content_with_engine(fallback_content, "本地多源数据底座")


def generate_fallback_digest(
    global_news: List[Dict[str, str]],
    ai_news: List[Dict[str, str]],
    github_projects: List[Dict[str, Any]],
    github_classic_gems: Optional[List[Dict[str, Any]]] = None,
    reason: str = ""
) -> str:
    """当所有大模型 API 遭遇网络故障或限流时，依托本地采集的高质量多源数据底座生成应急早报"""
    now_bj = report_now()
    date_str = now_bj.strftime("%Y年%m月%d日")
    weekday_map = {0: "星期一", 1: "星期二", 2: "星期三", 3: "星期四", 4: "星期五", 5: "星期六", 6: "星期日"}
    weekday_str = weekday_map[now_bj.weekday()]

    if github_classic_gems is None:
        github_classic_gems = []

    lines = [
        f"# 🌅 每日 AI & 全球资讯早报 | {date_str} {weekday_str}",
        "",
        "> 💡 **今日早报速览**：多源实时资讯已采集就绪，为您精选全球重大时事、AI 御三家与英伟达/特斯拉/华为/苹果四巨头动向，以及 GitHub 热门开源黑马与经典宝藏神器。",
        ""
    ]

    if reason:
        lines.extend([
            f"> ⚠️ **【大模型接口提示】**：云端大模型接口返回异常 (`{reason[:180]}`)。今日早报已由本地多源数据底座为您完整生成送达！",
            ""
        ])

    lines.extend([
        "## 🌍 板块一：全球时事（重深度、前因后果与多方影响）",
        ""
    ])
    for i, item in enumerate(global_news[:5], 1):
        lines.append(f"### {i}. [{item['title']}]({item['link']})")
        lines.append(f"- **核心事件**：{item['summary']}")
        lines.append(f"- **权威来源**：[🔗 来源：{item['source']}]({item['link']})")
        lines.append("")

    lines.extend([
        "## 🤖 板块二：AI 核心御三家 & 科技四巨头深度追踪",
        "",
        "### 🧠 AI 大模型与工具御三家（GPT/Codex、Gemini、Claude）",
        ""
    ])
    ai_trio_items = [it for it in ai_news if any(k in it['title'].lower() for k in ['openai', 'gpt', 'codex', 'gemini', 'google', 'claude', 'anthropic'])]
    if not ai_trio_items:
        ai_trio_items = ai_news[:3]
    for i, item in enumerate(ai_trio_items[:3], 1):
        lines.append(f"- **{item['title']}**")
        if item.get("summary"):
            lines.append(f"  - 动态脉络：{item['summary']}")
        lines.append(f"  - 详情链接：[🔗 查看原文]({item['link']})")
        lines.append("")

    lines.extend([
        "### ⚡ 科技四巨头（英伟达 / 特斯拉 / 华为 / 苹果）",
        ""
    ])
    nv_tsla_items = [it for it in ai_news if any(k in it['title'].lower() for k in ['nvidia', '英伟达', 'huang', '黄仁勋', 'blackwell', 'cuda', 'tesla', '特斯拉', 'fsd', 'optimus', 'musk', '马斯克', 'huawei', '华为', '鸿蒙', 'harmonyos', '麒麟', 'kirin', 'mate', 'pura', 'freebuds', 'apple', '苹果', 'iphone', 'ipad', 'macbook', 'airpods', 'ios'])]
    if not nv_tsla_items:
        nv_tsla_items = ai_news[3:6] if len(ai_news) > 3 else ai_news
    for i, item in enumerate(nv_tsla_items[:4], 1):
        lines.append(f"- **{item['title']}**")
        if item.get("summary"):
            lines.append(f"  - 深度剖析：{item['summary']}")
        lines.append(f"  - 权威来源：[🔗 查看原文]({item['link']})")
        lines.append("")

    lines.extend([
        "## 🔥 板块三：GitHub 开源项目寻宝（今日飙升 + 每日宝藏推荐）",
        "",
        "### 📈 今日飙升热榜（新锐精选）",
        ""
    ])
    for i, repo in enumerate(github_projects[:3], 1):
        stars_info = f"🌟 今日 {repo['stars_today']} / 总 Star {repo['total_stars']}"
        lines.append(f"### {i}. [{repo['name']}]({repo['url']}) {stars_info} | {repo['language']}")
        lines.append(f"- **💡 这项目到底是干啥的？**：{repo['description']}")
        lines.append(f"- **🎯 解决痛点与好玩在哪？**：近期爆发式增长，开箱即用，提升日常工作流与开发生产力。")
        lines.append("")

    lines.extend([
        "### 💎 每日宝藏 / 殿堂级项目推荐（固定精选 2 个）",
        ""
    ])
    classic_to_show = github_classic_gems[:2] if len(github_classic_gems) >= 2 else LOCAL_CLASSIC_GEMS_FALLBACK[:2]
    for i, gem in enumerate(classic_to_show, 1):
        lines.append(f"### 🌟 宝藏推荐 {i}：[{gem['name']}]({gem['url']}) 🏆 总 Stars: {gem.get('total_stars', '高分神作')} | {gem.get('language', 'Universal')}")
        lines.append(f"- **💡 这是个什么神器？**：{gem.get('description', '')}")
        lines.append(f"- **🎯 为什么强烈推荐 / 你能拿它干啥？**：经久不衰的殿堂级高星开源利器，功能强大、稳定可靠，日常装机必备。")
        lines.append("")

    return "\n".join(lines)


# ==================== 多渠道推送模块 (0元免费/多通道共存) ====================
def markdown_to_html(md_text: str, title: str, engine_label: Optional[str] = None) -> str:
    """将 Markdown 早报转换为高颜值移动端自适应 HTML 邮件卡片模板"""
    clean_text, extracted_label = extract_engine_label(md_text)
    active_label = engine_label or extracted_label
    if "本地多源数据底座" in active_label:
        sub_title = f"由 {active_label} 聚合生成 · 自动化每日送达"
    else:
        sub_title = f"由 {active_label} 深度提炼 · 自动化每日送达"

    html_lines = []
    list_tag: Optional[str] = None

    def close_list():
        nonlocal list_tag
        if list_tag:
            html_lines.append(f'</{list_tag}>')
            list_tag = None

    def format_inline(text: str) -> str:
        text = re.sub(
            r'\[(.*?)\]\((.*?)\)',
            r'<a href="\2" target="_blank" style="color:#1d63ed;text-decoration:none;font-weight:500;">\1</a>',
            text
        )
        text = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color:#0f172a;font-weight:600;">\1</strong>', text)
        text = re.sub(
            r'`(.*?)`',
            r'<code style="background:#f1f5f9;color:#0f172a;padding:2px 5px;border-radius:4px;font-family:Consolas,Monaco,monospace;font-size:88%;">\1</code>',
            text
        )
        return text

    for line in clean_text.splitlines():
        line_s = line.strip()
        if not line_s:
            close_list()
            continue

        if line_s.startswith('---'):
            close_list()
            html_lines.append('<hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0;">')
            continue

        if line_s.startswith('# '):
            close_list()
            h_text = format_inline(line_s[2:].strip())
            html_lines.append(f'<h1 style="margin:20px 0 12px;color:#0f172a;font-size:20px;font-weight:700;text-align:center;">{h_text}</h1>')
            continue

        if line_s.startswith('## '):
            close_list()
            h_text = format_inline(line_s[3:].strip())
            html_lines.append(f'<h2 style="margin:24px 0 12px;color:#1e293b;font-size:17px;font-weight:700;border-bottom:2px solid #e2e8f0;padding-bottom:6px;">{h_text}</h2>')
            continue

        if line_s.startswith('### '):
            close_list()
            h_text = format_inline(line_s[4:].strip())
            html_lines.append(f'<h3 style="margin:18px 0 8px;color:#2563eb;font-size:15px;font-weight:600;border-left:3px solid #2563eb;padding-left:8px;">{h_text}</h3>')
            continue

        if line_s.startswith('#### '):
            close_list()
            h_text = format_inline(line_s[5:].strip())
            html_lines.append(f'<h4 style="margin:14px 0 6px;color:#334155;font-size:14px;font-weight:600;">{h_text}</h4>')
            continue

        if line_s.startswith('> '):
            close_list()
            q_text = format_inline(line_s[2:].strip())
            html_lines.append(f'<blockquote style="margin:10px 0;padding:10px 14px;background:#f8fafc;border-left:4px solid #3b82f6;border-radius:0 6px 6px 0;color:#475569;font-size:14px;line-height:1.65;">{q_text}</blockquote>')
            continue

        if line_s.startswith('- ') or line_s.startswith('* '):
            if list_tag != 'ul':
                close_list()
                html_lines.append('<ul style="margin:8px 0;padding-left:20px;line-height:1.7;color:#334155;font-size:14px;">')
                list_tag = 'ul'
            li_text = format_inline(line_s[2:].strip())
            html_lines.append(f'<li style="margin:5px 0;">{li_text}</li>')
            continue

        ol_m = re.match(r'^(\d+)\.\s+(.*)$', line_s)
        if ol_m:
            if list_tag != 'ol':
                close_list()
                html_lines.append('<ol style="margin:8px 0;padding-left:20px;line-height:1.7;color:#334155;font-size:14px;">')
                list_tag = 'ol'
            li_text = format_inline(ol_m.group(2).strip())
            html_lines.append(f'<li style="margin:5px 0;">{li_text}</li>')
            continue

        close_list()
        p_text = format_inline(line_s)
        html_lines.append(f'<p style="margin:8px 0;line-height:1.72;color:#334155;font-size:14.5px;">{p_text}</p>')

    close_list()

    body_html = '\n'.join(html_lines)
    full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
</head>
<body style="margin:0;padding:12px;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei','Segoe UI',Roboto,sans-serif;-webkit-font-smoothing:antialiased;">
<div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,0.06);overflow:hidden;border:1px solid #e2e8f0;">
  <div style="background:linear-gradient(135deg, #1e3a8a 0%, #2563eb 50%, #3b82f6 100%);padding:22px 24px;color:#ffffff;">
    <div style="font-size:12px;letter-spacing:1px;opacity:0.9;text-transform:uppercase;font-weight:600;">DAILY INTELLIGENCE DIGEST</div>
    <h1 style="margin:6px 0 0;font-size:20px;font-weight:700;color:#ffffff;line-height:1.3;">🌅 {title}</h1>
    <div style="font-size:13px;opacity:0.85;margin-top:6px;">{sub_title}</div>
  </div>
  <div style="padding:20px 24px;">
    {body_html}
  </div>
  <div style="background:#f8fafc;padding:16px 24px;text-align:center;font-size:12px;color:#64748b;border-top:1px solid #e2e8f0;line-height:1.6;">
    每日 AI & 全球资讯早报 · 邮件自动推送 · 微信「QQ邮箱提醒」实时直达<br>
    <span style="color:#94a3b8;">无需第三方服务费 · 微信原生秒级卡片提醒</span>
  </div>
</div>
</body>
</html>"""
    return full_html


def resolve_smtp_config(sender: str) -> tuple:
    """自动根据邮箱地址后缀解析 SMTP 服务器、端口及 SSL 模式"""
    custom_server = os.getenv("SMTP_SERVER", "").strip()
    if custom_server:
        if ":" in custom_server:
            host, port_s = custom_server.split(":", 1)
            port = int(port_s)
        else:
            host = custom_server
            port = 465
        use_ssl = (port == 465)
        return host, port, use_ssl

    domain = sender.split("@")[-1].lower() if "@" in sender else ""
    if domain in ["qq.com", "foxmail.com"]:
        return "smtp.qq.com", 465, True
    elif domain == "163.com":
        return "smtp.163.com", 465, True
    elif domain == "126.com":
        return "smtp.126.com", 465, True
    elif domain == "gmail.com":
        return "smtp.gmail.com", 465, True
    elif domain in ["outlook.com", "hotmail.com"]:
        return "smtp.office365.com", 587, False
    else:
        return f"smtp.{domain}", 465, True


def send_via_email(title: str, content: str) -> bool:
    """
    【渠道 A：强烈推荐 0元免认证】通过邮件直推
    微信启用自带的「QQ邮箱提醒」插件后，收到邮件时微信会实时弹出消息卡片，免任何第三方实名费！
    """
    sender = os.getenv("EMAIL_SENDER", "").strip()
    password = os.getenv("EMAIL_PASSWORD", "").strip()
    if not sender or not password:
        return False

    receiver = os.getenv("EMAIL_RECEIVER", "").strip() or sender
    host, port, use_ssl = resolve_smtp_config(sender)

    clean_content, engine_label = extract_engine_label(content)

    print(f"\n📧 [渠道 A] 正在通过邮件推送早报 (服务器: {host}:{port}, 发件: {sender} -> 收件: {receiver}, 执笔模型: {engine_label}) ...")
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = formataddr((str(Header("每日 AI & 全球早报", "utf-8")), sender))
        msg["To"] = receiver

        # 兼容纯文本与精美 HTML 双格式 (剥离元数据注释，准确渲染执笔模型名称)
        html_body = markdown_to_html(clean_content, title, engine_label=engine_label)
        msg.attach(MIMEText(clean_content, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()

        server.login(sender, password)
        server.sendmail(sender, [receiver], msg.as_string())
        server.quit()
        print(f"  ✅ 邮件推送成功！若收件箱为 QQ 邮箱且已开启微信插件，微信将实时弹出通知卡片。")
        return True
    except Exception as e:
        print(f"  ❌ 邮件推送失败: {e}")
        print("     [排错指引] 若使用 QQ/163 邮箱，请确认密码填入的是在邮箱设置中生成的「POP3/SMTP 专属授权码」，而非普通邮箱登录密码。")
        return False


def dispatch_notifications(content: str) -> bool:
    """分发早报到 QQ 邮箱，成功后持久化去重与推送标记，并保存本地 preview_digest.md"""
    clean_content, engine_label = extract_engine_label(content)
    now_bj = datetime.now(BEIJING_TZ)
    today_str = now_bj.strftime("%Y-%m-%d")
    title = f"每日 AI & 全球资讯早报 | {today_str}"

    # 1. 保存本地 preview_digest.md 预览
    preview_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview_digest.md")
    try:
        with open(preview_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"\n📄 最新早报已同步保存至本地预览文件: {preview_path} (执笔模型: {engine_label})")
    except Exception as e:
        print(f"  [警告] 保存本地 preview_digest.md 失败: {e}")

    # 2. 检查 QQ 邮箱推送配置
    email_enabled = bool(os.getenv("EMAIL_SENDER") and os.getenv("EMAIL_PASSWORD"))
    if not email_enabled:
        print("\n⚠️ [提示] 未配置 EMAIL_SENDER 或 EMAIL_PASSWORD，跳过邮件推送。")
        if (os.getenv("GITHUB_ACTIONS") or "").strip().lower() == "true":
            print("   -> 云端运行缺少邮箱 Secret，视为推送失败。")
            return False
        record_history_seen(today_str, clean_content, mark_sent=False)
        print("   -> 您可直接打开 preview_digest.md 查看生成的早报。")
        return True

    sent_ok = send_via_email(title, content)
    if sent_ok:
        record_history_seen(today_str, clean_content, mark_sent=True)
    return sent_ok


# ==================== 主流程执行入口 ====================
def main():
    print("=" * 60)
    print("🚀 每日 AI & 全球资讯早报生成系统 启动")
    now_bj = datetime.now(BEIJING_TZ)
    today_str = now_bj.strftime("%Y-%m-%d")
    print(f"⏰ 当前北京时间: {now_bj.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    prebuild = (os.getenv("PREBUILD_MODE") or "").strip() == "1"
    force_send = (os.getenv("FORCE_SEND") or "").strip().lower() in ("1", "true", "yes")

    if not prebuild:
        # 幂等防重保护：若当天已成功推送过邮件且未指定 FORCE_SEND，直接跳过，避免多调度器重复发信与重复扣费
        sent_ts = get_sent_timestamp(today_str)
        if sent_ts and not force_send:
            print(f"\n✅ 今日 ({today_str}) 早报已于 {sent_ts} 成功推送过，自动跳过重复执行（如需强制重发可设 FORCE_SEND=1）。")
            return

        # 仅当存在预先准备的今日稿件且尚未发送时，直接推送预生成稿件
        if not force_send:
            prebuilt = load_prebuilt_digest(today_str)
            if prebuilt:
                print(f"\n📦 发现今日待发送预生成早报 ({len(prebuilt)} 字符)，直接推送，跳过大模型。")
                if not dispatch_notifications(prebuilt):
                    print("\n❌ 早报推送失败，以非零状态退出，避免任务被误判为成功。")
                    sys.exit(1)
                print("\n🎉 今日早报全流程顺利执行完毕！")
                return

    target_date_str = report_now().strftime("%Y-%m-%d")

    # 预先扫描跨天历史记录（排查前 5 天已报道事件与开源项目）
    history_info = scan_recent_history(days=5, exclude_date=target_date_str if prebuild else None)
    seen_urls = history_info["seen_urls"]
    seen_repos = history_info["seen_repos"]
    seen_titles = history_info["seen_titles"]
    blacklist_summary = history_info["blacklist_summary"]
    print(f"\n🔍 [跨天去重] 已扫描近 5 天历史数据: 提取到 {len(seen_repos)} 个已推仓库, {len(seen_urls)} 个已发链接, {len(seen_titles)} 条历史标题")

    # 1. 数据采集（应用跨天排重）
    global_news = collect_global_news(seen_urls=seen_urls, seen_titles=seen_titles)
    ai_news = collect_ai_news(seen_urls=seen_urls, seen_titles=seen_titles)
    github_trending = collect_github_trending(seen_repos=seen_repos)
    trending_names = {p.get("name", "").lower() for p in github_trending}
    # 经典宝藏排除 今日 trending + 近 5 天历史推荐的 repo
    exclude_classic_names = trending_names | seen_repos
    github_classic_gems = fetch_github_classic_gems(exclude_names=exclude_classic_names)
    github_trending = enrich_github_projects(github_trending[:8], "今日飙升")
    github_classic_gems = enrich_github_projects(github_classic_gems[:6], "每日宝藏")
    total_scraped = len(global_news) + len(ai_news) + len(github_trending) + len(github_classic_gems)
    print(f"\n📊 数据采集完成，总共获得 {total_scraped} 条全网最新热点与经典宝藏数据底座。")

    # 2. 大模型智能提炼 (支持 local / aizex / 本地应急兜底)
    print("\n🧠 启动大模型智能提炼与分析引擎...")
    digest_content = generate_digest(
        global_news, ai_news, github_trending, github_classic_gems, blacklist_summary=blacklist_summary
    )

    if prebuild:
        target = target_date_str
        if "【大模型接口提示】" in digest_content or "大模型接口提示" in digest_content[:600]:
            print("\n❌ 预生成失败：大模型未返回有效内容，不落盘（避免云端误用兜底稿）。")
            sys.exit(1)
        os.makedirs(PREBUILT_DIR, exist_ok=True)
        path = os.path.join(PREBUILT_DIR, f"{target}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(digest_content)
        print(f"\n📦 已预生成 {target} 早报: {path} ({len(digest_content)} 字符)")

        # 同步写入 preview_digest.md 供本地随时查看最新预览
        preview_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview_digest.md")
        try:
            with open(preview_path, "w", encoding="utf-8") as pf:
                pf.write(digest_content)
            print(f"📄 已同步更新本地预览文件: {preview_path}")
        except Exception as e:
            print(f"  [警告] 保存本地 preview_digest.md 失败: {e}")

        # 持久化更新历史去重库
        record_history_seen(target, digest_content)
        return

    # 3. 多渠道推送分发与本地落地
    if not dispatch_notifications(digest_content):
        # 不归档：否则重跑时会读到这份稿子，误以为当天已发送
        print("\n❌ 早报推送失败，跳过归档并以非零状态退出。")
        sys.exit(1)
    try:
        os.makedirs(PREBUILT_DIR, exist_ok=True)
        today_path = os.path.join(PREBUILT_DIR, f"{target_date_str}.md")
        with open(today_path, "w", encoding="utf-8") as f:
            f.write(digest_content)
        print(f"📦 已将今日早报归档至本地 prebuilt 目录: {today_path}")
    except Exception as e:
        print(f"  [警告] 归档 prebuilt 失败: {e}")

    print("\n🎉 今日早报全流程顺利执行完毕！")


if __name__ == "__main__":
    main()

