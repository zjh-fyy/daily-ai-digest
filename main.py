#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日 AI & 全球资讯早报 (Daily AI & Global Digest)
--------------------------------------------------
自动化多源数据抓取 -> 大模型智能提炼 (Gemini 3.8 Flash / GPT-6.1-sol High) -> 多渠道推送

核心功能板块：
1. 🌍 全球时事（前因后果、多方视角、背景脉络与权威来源）
2. 🤖 AI 核心御三家 & 科技四巨头深度追踪（OpenAI/Codex, Google/Gemini, Anthropic/Claude + NVIDIA/Tesla/华为/苹果）
3. 🔥 GitHub 开源项目寻宝（今日飙升热榜 + 每日宝藏/殿堂级神器精选）
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


def record_history_seen(date_str: str, content: str):
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
        entries[date_str] = {
            "urls": sorted(list(urls)),
            "repos": sorted(list(repos)),
            "titles": titles[:15],
            "updated_at": datetime.now(BEIJING_TZ).isoformat()
        }

        cutoff_str = (report_now() - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
        entries = {k: v for k, v in entries.items() if k >= cutoff_str}
        history_data["entries"] = entries

        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        print(f"📁 已更新历史去重记录库: {HISTORY_FILE} (当天记录 {len(repos)} 个仓库, {len(urls)} 个链接)")
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


def http_get(url: str, params: Optional[dict] = None) -> Optional[requests.Response]:
    """统一的网络请求封装，附带标准浏览器 User-Agent 和超时控制"""
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            return resp
        else:
            print(f"  [警告] 请求失败 ({resp.status_code}): {url}")
            return None
    except Exception as e:
        print(f"  [异常] 网络请求错误: {url} -> {e}")
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

            summary = clean_html_text(raw_content, max_len=500)
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
    """采集全球重大时事政经资讯（多渠道融合 + 跨天排重）"""
    print("\n[1/3] 正在采集【全球重大时事】资讯...")
    sources = [
        ("http://feeds.bbci.co.uk/news/world/rss.xml", "BBC World News", 6),
        ("https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "Google News (中文热点)", 8),
        ("https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en", "Google News (US Top Stories)", 6),
    ]

    all_news = []
    current_seen_titles = set()

    for url, name, limit in sources:
        feed_items = fetch_rss_feed(url, name, max_items=limit)
        for item in feed_items:
            # 1. 过滤已在最近历史中出现过的 URL
            if seen_urls and item["link"] in seen_urls:
                continue

            # 2. 跨天及当日高相似度新闻标题去重
            if seen_titles and is_title_duplicate(item["title"], seen_titles):
                continue

            title_key = item["title"][:25].lower()
            if title_key not in current_seen_titles:
                current_seen_titles.add(title_key)
                all_news.append(item)

    print(f"  => 全球时事汇总去重后共 {len(all_news)} 条数据底座")
    return all_news


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

    # 1. OpenAI 官方新闻 RSS
    openai_feed = fetch_rss_feed("https://openai.com/news/rss.xml", "OpenAI Official News", max_items=5)
    for it in openai_feed:
        add_filtered_item(it)

    # 2. Google News 专项靶向检索（聚焦 AI 御三家 + 英伟达/特斯拉/华为/苹果）
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
        # 华为 (Huawei) 鸿蒙、昇腾与终端
        ('("Huawei" OR "HarmonyOS" OR "Ascend") when:2d', "Huawei / 华为 (Global)", "en-US", "US:en"),
        ('("华为" OR "鸿蒙" OR "昇腾" OR "余承东") when:2d', "华为 / 鸿蒙 / 昇腾 (中文热点)", "zh-CN", "CN:zh-Hans"),
        # 苹果 (Apple) 设备、芯片与 Apple Intelligence
        ('("Apple Inc" OR "iPhone" OR "Apple Intelligence" OR "Tim Cook" OR "macOS") when:2d', "Apple / 苹果 (Global)", "en-US", "US:en"),
        ('("苹果公司" OR "iPhone" OR "库克" OR "Apple Intelligence") when:2d', "苹果 / iPhone (中文热点)", "zh-CN", "CN:zh-Hans"),
    ]

    for q, label, hl, ceid in targeted_queries:
        encoded_q = urllib.parse.quote(q)
        search_rss_url = f"https://news.google.com/rss/search?q={encoded_q}&hl={hl}&gl=US&ceid={ceid}"
        feed_items = fetch_rss_feed(search_rss_url, label, max_items=4)
        for it in feed_items:
            add_filtered_item(it)

    # 3. Hacker News 高分 AI/科技深度讨论帖（基于稳定 Algolia HN API）
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

            for row in rows[:16]:
                title_elem = row.select_one("h2 a")
                if not title_elem:
                    continue

                repo_path = title_elem.get("href", "").strip().lstrip("/")
                repo_url = f"https://github.com/{repo_path}"
                name = re.sub(r"\s+", "", title_elem.text)

                # 跨天去重：若该仓库在前 5 天已推荐过，坚决跳过
                if repo_path.lower() in normalized_seen_repos or name.lower() in normalized_seen_repos:
                    print(f"  [跨天去重] 跳过近 5 天已推荐项目: {name}")
                    continue

                desc_elem = row.select_one("p")
                description = desc_elem.text.strip() if desc_elem else "暂无项目描述"

                lang_elem = row.select_one("span[itemprop='programmingLanguage']")
                language = lang_elem.text.strip() if lang_elem else "Unknown"

                # 今日新增 Star
                stars_today_elem = row.select_one("span.d-inline-block.float-sm-right")
                stars_today = stars_today_elem.text.strip() if stars_today_elem else ""
                if not stars_today:
                    match = re.search(r"(\d[\d,]*\s+stars?\s+today)", row.text)
                    stars_today = match.group(1) if match else "N/A"

                # 总 Star
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
                print(f"  => 成功提取 GitHub Trending 前 {len(projects)} 个项目 (已过滤重复)")
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


# ==================== 每日经典宝藏项目池 (30+ 维度轮换与兜底) ====================
CLASSIC_QUERY_POOL = [
    {"topic": "生产力与效率工具", "query": "stars:8000..150000 topic:productivity", "sort": "stars"},
    {"topic": "自动化运维与工作流", "query": "stars:5000..90000 topic:automation", "sort": "stars"},
    {"topic": "桌面端装机必备", "query": "stars:8000..120000 desktop in:readme,description", "sort": "stars"},
    {"topic": "音视频处理与剪辑神器", "query": "stars:6000..100000 video in:description", "sort": "stars"},
    {"topic": "音频处理与语音黑科技", "query": "stars:5000..80000 audio in:description", "sort": "stars"},
    {"topic": "Windows 效率与系统增强", "query": "stars:4000..70000 windows tool in:description,readme", "sort": "stars"},
    {"topic": "极客终端与命令行神器", "query": "stars:8000..120000 cli in:description,readme", "sort": "stars"},
    {"topic": "自托管与私人私有云", "query": "stars:8000..120000 topic:self-hosted", "sort": "stars"},
    {"topic": "OCR 文字识别与文档解析", "query": "stars:4000..60000 ocr in:description,readme", "sort": "stars"},
    {"topic": "Markdown 与知识管理工具", "query": "stars:6000..90000 markdown in:description", "sort": "stars"},
    {"topic": "浏览器增强与效率扩展", "query": "stars:5000..80000 browser extension in:description", "sort": "stars"},
    {"topic": "轻量游戏引擎与互动创意", "query": "stars:5000..80000 game engine in:description", "sort": "stars"},
    {"topic": "AI 赋能生产力桌面端", "query": "stars:10000..150000 ai tool in:readme", "sort": "stars"},
    {"topic": "网络安全与隐私守护", "query": "stars:6000..90000 security tool in:description", "sort": "stars"},
    {"topic": "UI/UX 设计与绘图神器", "query": "stars:6000..90000 design tool in:description", "sort": "stars"},
    {"topic": "系统资源监控与性能分析", "query": "stars:7000..100000 system monitor in:description", "sort": "stars"},
    {"topic": "高速下载与传输利器", "query": "stars:6000..90000 download in:description,readme", "sort": "stars"},
    {"topic": "开发者效率瑞士军刀", "query": "stars:8000..120000 developer-tools in:description,readme", "sort": "stars"},
    {"topic": "跨平台硬件与外设交互", "query": "stars:4000..60000 hardware in:description", "sort": "stars"},
    {"topic": "现代终端仿真器与 Shell", "query": "stars:7000..100000 terminal in:description,readme", "sort": "stars"},
    {"topic": "数据备份与数据恢复", "query": "stars:5000..70000 backup tool in:description", "sort": "stars"},
    {"topic": "PDF 处理与格式转换神兵", "query": "stars:6000..90000 pdf in:description", "sort": "stars"},
    {"topic": "本地文件搜索与秒级定位", "query": "stars:8000..120000 search in:description,readme", "sort": "stars"},
    {"topic": "网络调试与网络分析抓包", "query": "stars:5000..80000 network in:description", "sort": "stars"},
    {"topic": "双链笔记与个人知识库", "query": "stars:7000..100000 note-taking in:description", "sort": "stars"},
    {"topic": "新一代文件管理器", "query": "stars:6000..90000 file-manager in:description", "sort": "stars"},
    {"topic": "剪贴板历史与生产力增强", "query": "stars:4000..60000 clipboard in:description", "sort": "stars"},
    {"topic": "截图与屏幕录制神器", "query": "stars:6000..90000 screenshot in:description", "sort": "stars"},
    {"topic": "数据库可视化与轻量客户端", "query": "stars:6000..90000 database client GUI in:description", "sort": "stars"},
    {"topic": "Git 版本控制图形化神器", "query": "stars:7000..90000 git GUI in:description", "sort": "stars"},
    {"topic": "局域网跨设备高速互传", "query": "stars:6000..80000 local transfer in:description,readme", "sort": "stars"},
    {"topic": "密码管理与安全保险箱", "query": "stars:7000..100000 password manager in:description", "sort": "stars"},
]

LOCAL_CLASSIC_GEMS_FALLBACK = [
    {"name": "localsend/localsend", "url": "https://github.com/localsend/localsend", "description": "开源跨平台局域网文件互传神器，免配置平替 AirDrop，手机电脑互传秒级直达。", "language": "Dart", "total_stars": "56000+", "topic": "跨平台局域网传输"},
    {"name": "rustdesk/rustdesk", "url": "https://github.com/rustdesk/rustdesk", "description": "开源全功能远程桌面平替神器，支持自建中继服务器，安全极速无限制。", "language": "Rust", "total_stars": "78000+", "topic": "远程桌面协作"},
    {"name": "yt-dlp/yt-dlp", "url": "https://github.com/yt-dlp/yt-dlp", "description": "全网最强大的音视频无损下载利器，支持全球上千家视频音频网站无水印解析下载。", "language": "Python", "total_stars": "89000+", "topic": "全网音视频提取"},
    {"name": "excalidraw/excalidraw", "url": "https://github.com/excalidraw/excalidraw", "description": "手绘风白板绘图神器，支持多人协同、架构图手绘设计，纯净无广告。", "language": "TypeScript", "total_stars": "85000+", "topic": "手绘风白板设计"},
    {"name": "obsproject/obs-studio", "url": "https://github.com/obsproject/obs-studio", "description": "全球顶尖的免费开源屏幕录制与直播推流工具，性能强大、插件生态极其繁荣。", "language": "C/C++", "total_stars": "62000+", "topic": "高清录屏与直播"},
    {"name": "calibre-ebook/calibre", "url": "https://github.com/calibre-ebook/calibre", "description": "殿堂级电子书格式转换与书库管理工具，阅读排版与格式互转的一站式神器。", "language": "Python", "total_stars": "19000+", "topic": "电子书全能管理"},
    {"name": "syncthing/syncthing", "url": "https://github.com/syncthing/syncthing", "description": "去中心化的连续文件同步工具，在多台电脑手机间安全同步文件夹，不经过第三方服务器。", "language": "Go", "total_stars": "67000+", "topic": "去中心化多端同步"},
    {"name": "AppFlowy-IO/AppFlowy", "url": "https://github.com/AppFlowy-IO/AppFlowy", "description": "开源 Notion 平替神器，数据 100% 本地优先存储，隐私无忧，排版美观。", "language": "Flutter/Rust", "total_stars": "57000+", "topic": "本地知识工作台"},
    {"name": "logseq/logseq", "url": "https://github.com/logseq/logseq", "description": "隐私优先的双链大纲笔记与知识图谱工具，纯本地 Markdown 存储，思维发散绝配。", "language": "Clojure", "total_stars": "33000+", "topic": "双链大纲知识库"},
    {"name": "gorhill/uBlock", "url": "https://github.com/gorhill/uBlock", "description": "最高效的轻量级浏览器广告与隐私追踪拦截插件，超低内存占用，还网页一片清净。", "language": "JavaScript", "total_stars": "47000+", "topic": "网页纯净拦截"},
    {"name": "keepassxreboot/keepassxc", "url": "https://github.com/keepassxreboot/keepassxc", "description": "开源本地加密密码管理器，无需依赖任何云端即可实现极高安全级别的密码与密钥管理。", "language": "C++", "total_stars": "24000+", "topic": "本地密码保险箱"},
    {"name": "ShareX/ShareX", "url": "https://github.com/ShareX/ShareX", "description": "Windows 平台天花板级别的免费开源截图、录屏、OCR 与工作流自动化利器。", "language": "C#", "total_stars": "29000+", "topic": "全能截图与自动化"},
    {"name": "HandBrake/HandBrake", "url": "https://github.com/HandBrake/HandBrake", "description": "开源跨平台的高性能视频格式转换与体积无损压缩工具，压制视频必备。", "language": "C", "total_stars": "16000+", "topic": "视频压制转换"},
    {"name": "Genymobile/scrcpy", "url": "https://github.com/Genymobile/scrcpy", "description": "通过 USB 或无线将安卓手机屏幕超低延迟投屏到电脑并用键鼠控制，免 Root 神器。", "language": "C", "total_stars": "110000+", "topic": "安卓电脑同屏控制"},
    {"name": "jgraph/drawio", "url": "https://github.com/jgraph/drawio", "description": "功能比肩 Visio 的专业流程图、UML 图与架构图绘制工具，完全免费无限制。", "language": "JavaScript", "total_stars": "43000+", "topic": "专业流程图架构图"},
    {"name": "astral-sh/ruff", "url": "https://github.com/astral-sh/ruff", "description": "基于 Rust 编写的百倍速极速 Python Linter 与代码格式化工具，颠覆传统工具链。", "language": "Rust", "total_stars": "36000+", "topic": "新一代极速开发工具"},
    {"name": "tiangolo/fastapi", "url": "https://github.com/tiangolo/fastapi", "description": "现代高性能 Python Web 框架，基于标准类型提示，自动生成交互式 API 文档。", "language": "Python", "total_stars": "81000+", "topic": "现代化高并发后端"},
    {"name": "dbeaver/dbeaver", "url": "https://github.com/dbeaver/dbeaver", "description": "全功能开源通用数据库管理工具，支持 MySQL、PostgreSQL、SQLite 等近百种数据源。", "language": "Java", "total_stars": "40000+", "topic": "通用数据库管理"},
    {"name": "motrixapp/motrix", "url": "https://github.com/motrixapp/motrix", "description": "高颜值全功能下载管理器，支持 HTTP、FTP、BT、磁力链接等多种协议，跨平台美观高效。", "language": "TypeScript", "total_stars": "45000+", "topic": "全能跨平台下载"},
    {"name": "veler/DevToys", "url": "https://github.com/veler/DevToys", "description": "开发者的瑞士军刀桌面端，包含 JSON 格式化、正则测试、哈希计算等数十种日常工具。", "language": "C#", "total_stars": "24000+", "topic": "离线开发百宝箱"},
    {"name": "AUTOMATIC1111/stable-diffusion-webui", "url": "https://github.com/AUTOMATIC1111/stable-diffusion-webui", "description": "经典 Stable Diffusion AI 绘画 Web 交互界面，功能极为完整丰富，生态插件无数。", "language": "Python", "total_stars": "145000+", "topic": "AI 本地图像创作"},
    {"name": "ggerganov/llama.cpp", "url": "https://github.com/ggerganov/llama.cpp", "description": "纯 C/C++ 实现的大语言模型推理引擎，让轻薄本和普通电脑也能流畅本地运行开源大模型。", "language": "C++", "total_stars": "73000+", "topic": "大模型本地轻量推理"}
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


# ==================== 大模型提炼核心模块 ====================
def build_digest_prompt(
    global_news: List[Dict[str, str]],
    ai_news: List[Dict[str, str]],
    github_projects: List[Dict[str, Any]],
    github_classic_gems: Optional[List[Dict[str, Any]]] = None,
    blacklist_summary: str = ""
) -> str:
    """构建供大模型 (本地 Gemini 3.8 Flash / aizex gpt-6.1-terra) 提炼的统一标准 Prompt"""
    now_bj = report_now()
    date_str = now_bj.strftime("%Y年%m月%d日")
    weekday_map = {0: "星期一", 1: "星期二", 2: "星期三", 3: "星期四", 4: "星期五", 5: "星期六", 6: "星期日"}
    weekday_str = weekday_map[now_bj.weekday()]

    if github_classic_gems is None:
        github_classic_gems = []

    context_data = {
        "report_date": f"{date_str} {weekday_str} (北京时间)",
        "raw_global_news": global_news[:14],
        "raw_ai_news": ai_news[:80],
        "raw_github_trending": github_projects[:10],
        "raw_github_classic_gems": github_classic_gems[:8]
    }
    context_json = json.dumps(context_data, ensure_ascii=False, indent=2)

    blacklist_section = ""
    if blacklist_summary and blacklist_summary.strip() != "暂无近期历史黑名单":
        blacklist_section = f"""
### 🚫 近期已报道事件与开源项目黑名单（严禁炒冷饭）：
{blacklist_summary}

【防重复铁律】：
1. 严禁对黑名单中的同一事件换个说法重复报道（除非有重大新进展，需明确写出“最新进展”）；
2. 严禁重复推荐黑名单中出现的任何 GitHub 项目，确保今日推荐的开源项目 100% 崭新！
"""

    prompt = f"""
你是一位殿堂级科技主编与资深国际政治经济观察家。请基于我为你采集的【原始数据底座】，为微信移动端读者精编一份高质量、兼具深度与可读性的早报。

### 📅 今日日期
{date_str} {weekday_str}

### 📦 原始数据底座 (JSON)
{context_json}
{blacklist_section}
---

### 📝 结构与撰写准则（必须严格执行）：

#### 🌅 标题：`每日 AI & 全球资讯早报 | {date_str} {weekday_str}`
简练导语（30~50字，点出今日最具震撼力的 1~2 个看点）。
- **【严禁废话与免责声明】**：严禁输出诸如“编读说明”、“本报依据所提供的数据底座编选”、“未进行实时联网复核”等任何元说明或免责声明！导语之后直接进入【🌍 板块一：全球时事】。

#### 🌍 板块一：全球时事（重深度、前因后果与多方影响）
- 精选过去 24 小时最关键的 **4~5 条**全球重大政经、科技或地缘热点。
- 绝不是简单一句话新闻列表！每条内容必须包含：
  - **核心事件**：事实准确，时间、地点、主体明确。
  - **前因后果与脉络**：为什么会发生？背后有什么历史背景、经济博弈或政策动机？
  - **多方视角与深层影响**：各方表态如何？对全球经济、产业链格局或普通大众有何长远影响？
  - **权威来源**：在末尾必须附带可点击的 Markdown 链接，如 `[🔗 来源：BBC](链接)`。

#### 🤖 板块二：AI 核心御三家 & 科技四巨头深度追踪
- **【彻底移除原福利专栏】**：坚决不输出任何“今日福利/优惠活动/白嫖羊毛”内容，全力聚焦前沿科技攻防与产业风云！
- **【宁缺毋滥与反假大空准则】**：
  若当日某细分公司（如特斯拉/马斯克或某家御三家）在提供的数据底座中无实质性重大新进展，**严禁使用官网主页链接（如 https://www.tesla.com、https://www.nvidia.com）或假大空的套话套词强行凑数**！可直接将该名额让渡给其他有重大突破的 AI 实验室、前沿开源模型、芯片算力架构动态或具身智能进展，或者如实写明今日无重大动向并聚焦其他核心看点。
- 必须严格分为以下两个子部分输出：
  1. **🧠 AI 大模型与工具御三家（GPT/Codex、Gemini/Antigravity、Claude）**：
     - 提炼 OpenAI、Google、Anthropic 三家最新的模型发布、能力升级、API 迭代、IDE/Agent 生态及核心战略进展。
  2. **⚡ 科技四巨头（英伟达 NVIDIA / 特斯拉 Tesla / 华为 Huawei / 苹果 Apple）**：
     - **英伟达 (NVIDIA)**：老黄最新动向、AI 算力芯片（Blackwell/Rubin）、CUDA 生态、网络互联架构与市场催化剂。
     - **特斯拉 (Tesla)**：FSD 自动驾驶落地、Optimus 仿生机器人、Robotaxi 车队、工程突破与战略动作。
     - **华为 (Huawei)**：鸿蒙 HarmonyOS 生态、昇腾 AI 芯片与算力、Mate/Pura 等终端新品、问界等智能汽车业务与重大战略动作。
     - **苹果 (Apple)**：iPhone/Mac 等新品、自研芯片、Apple Intelligence 与 Siri 的 AI 进展、iOS 系统更新与重大战略动作。
     - 每条均附带可点击的真实来源链接，如 `[🔗 查看原文](链接)`。

#### 🔥 板块三：GitHub 开源项目寻宝（今日飙升 + 每日宝藏推荐）
- **【严禁推荐黑名单中的 GitHub 项目】**：确保今日推荐的所有开源项目与最近 {HISTORY_KEEP_DAYS} 天绝不重复！
- **【读者画像】**：读者是完全不懂编程的普通人。默认他不知道什么是终端、命令行、API、框架、SDK、MCP、Agent、仓库、部署等概念。
  - 正文中如必须出现技术名词，第一次出现时紧跟括号用一句大白话解释（例：“MCP（可以理解成让 AI 能操作其他软件的通用插头）”）。
  - 严禁用一个术语去解释另一个术语；严禁只翻译 GitHub 英文简介。
  - 若某项目纯粹是给程序员用的开发工具，也要讲清“程序员拿它干嘛、对普通人间接意味着什么”，并在门槛里如实标注“普通人基本用不上”。
- 必须严格分为以下两个子部分输出：
  1. **📈 今日飙升热榜（固定精选 3 个近期暴涨的新锐项目）**：
     - 从 `raw_github_trending` 中挑选最惊艳的 3 个项目，优先挑普通人能直接用上或能看懂意义的项目。
     - 格式规范（每个项目 250~350 字）：
       `### 序号. [项目名](链接) 🌟 今日 +新增Stars / 总Stars | 语言`
       - **📌 一句话说清**：不含任何术语，像跟爸妈解释一样说它是个什么东西。
       - **💡 打个比方**：用一个生活中的类比讲清它的原理或作用。
       - **🎬 具体能怎么用**：举 1~2 个真实使用场景，写出“谁、在什么情况下、用它做了什么、得到什么结果”。
       - **🚪 上手门槛**：从以下档位中选一个并补一句说明——「下载安装即用」/「需要跟着教程敲几行命令」/「需要会编程，仅开发者适用」；如它能平替某个收费软件，在此写明。
  2. **💎 每日宝藏 / 殿堂级项目推荐（固定精选 2 个，类别不限）**：
     - 从 `raw_github_classic_gems` 中精选 2 个最实用、普通人最可能用上的宝藏项目进行深度推荐！
     - 格式规范（每个项目 300~400 字）：
       `### 🌟 宝藏推荐 序号：[项目名](链接) 🏆 总Stars | 语言`
       - **📌 一句话说清**：不含任何术语，说它是个什么东西。
       - **💡 打个比方**：用生活类比讲透它的本质功能。
       - **🎬 具体能怎么用**：举 2 个真实使用场景，越具体越好。
       - **🆚 能替代什么**：它能平替哪些收费/常见软件，比它们好在哪、差在哪。
       - **🚪 上手门槛**：同上三档之一，并说明去哪下载（如“项目页右侧 Releases 下载安装包”）。

---

### 📱 微信端排版与格式规范：
1. 语言：通俗、生动、凝练的简体中文。
2. 格式：清晰优雅的 Markdown 格式（使用 `#`, `##`, `###`, `**加粗**`, `- 列表`, `>` 引用块）。
3. 避免在微信中渲染效果差的复杂 HTML 语法或多列宽表格，以引用卡片和列表为主，确保手机小屏沉浸阅读。
4. 【完整性铁律】：必须完整输出全部三大板块（板块一：全球时事、板块二：AI 御三家 + 英伟达/特斯拉/华为/苹果、板块三：GitHub 飙升热榜 + 每日宝藏推荐），切勿写得冗长导致后半部分被截断！板块一、二每条控制在 150~200 字精准提炼；板块三按上面规定的字数写足，宁可少选一个项目也要把每个项目讲透。
5. 【纯净内容准则】：坚决禁止输出任何编者说明、数据底座局限性或未联网说明，导语之后直接切入板块一正文。
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
    """调用本机 Antigravity 网关 (OpenAI 兼容, Gemini 3.8 Flash)，仅在本机开机时可用"""
    base_url = (os.getenv("LOCAL_LLM_BASE_URL") or "http://127.0.0.1:8317/v1").strip().rstrip("/")
    model = (os.getenv("LOCAL_LLM_MODEL") or "gemini-3.8-flash-high").strip()
    prompt = build_digest_prompt(global_news, ai_news, github_projects, github_classic_gems, blacklist_summary=blacklist_summary)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": (
                "你是一位殿堂级科技主编与资深国际政治经济观察家。请基于提供的数据底座，"
                "为移动端读者精编一份结构严谨、前因后果清晰、大白话解读开源项目、兼具深度与可读性的每日 AI & 全球资讯早报。"
                "【完整性铁律】：必须按顺序完整输出三大板块：1.全球时事；2.AI 核心御三家 & 科技四巨头（英伟达/特斯拉/华为/苹果）深度追踪；"
                "3.GitHub 开源项目寻宝（今日飙升 + 每日宝藏推荐）。严禁截断、严禁输出任何免责声明。"
            )},
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
) -> str:
    """
    调用用户主力云端 API (gpt-6.1-sol + high 深度思考)，使用 /v1/responses 协议。
    内置多节点智能切换、最多 3 次指数退避重试 (解决 upstream_stream_incomplete 等流式网络中断)，
    并在重试失败时自动降级非流式单次抓取，确保高可靠送达。
    """
    model = (os.getenv("CODEX_MODEL") or os.getenv("AIZEX_MODEL") or "gpt-6.1-sol").strip()
    effort = (os.getenv("CODEX_EFFORT") or os.getenv("AIZEX_EFFORT") or "high").strip().lower()

    # 动态端点候选池 (国内优先香港直连，云端 Actions 优先全球加速)
    is_github_actions = (os.getenv("GITHUB_ACTIONS") or "").strip().lower() == "true"
    endpoints = []
    custom_url = (os.getenv("AIZEX_BASE_URL") or os.getenv("CODEX_BASE_URL") or "").strip().rstrip("/")
    if custom_url:
        endpoints.append(custom_url)
    if is_github_actions:
        for ep in ["https://codex-api.aizex.net/v1", "https://ca.memofun.net/v1"]:
            if ep not in endpoints:
                endpoints.append(ep)
    else:
        for ep in ["https://ca.memofun.net/v1", "https://codex-api.aizex.net/v1"]:
            if ep not in endpoints:
                endpoints.append(ep)

    prompt = build_digest_prompt(global_news, ai_news, github_projects, github_classic_gems, blacklist_summary=blacklist_summary)

    base_payload = {
        "model": model,
        "input": prompt,
        "instructions": (
            "你是一位殿堂级科技主编与资深国际政治经济观察家。请基于提供的数据底座，"
            "为移动端读者精编一份结构严谨、前因后果清晰、大白话解读开源项目、兼具深度与可读性的每日 AI & 全球资讯早报。"
            "请务必完整输出三大板块，切勿遗漏。严禁输出任何关于未联网或数据底座局限性的免责声明。"
        ),
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
        # 前 2 次尝试 SSE 流式；若连续流式中断，第 3 次自动降级非流式 (stream: False) 彻底避开 SSE 截断风险
        use_stream = (attempt < 3)
        payload = dict(base_payload)
        payload["stream"] = use_stream

        # 端点轮换
        base_url = endpoints[(attempt - 1) % len(endpoints)]
        endpoint = f"{base_url}/responses"

        stream_desc = "流式 SSE" if use_stream else "非流式完整拉取"
        print(f"  🚀 [尝试 {attempt}/{max_retries}] 正在向云端 API 发送请求 (模型: {model}, thinking={effort}, {stream_desc}, 节点: {base_url}) ...")
        t0 = time.time()

        req_headers = dict(headers)
        if use_stream:
            req_headers["Accept"] = "text/event-stream"
        else:
            req_headers["Accept"] = "application/json"

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
                # 非流式直接解析 JSON
                data = resp.json()
                text_parts = []
                for item in data.get("output", []):
                    if item.get("type") == "message":
                        for c in item.get("content", []):
                            if c.get("type") == "output_text":
                                text_parts.append(c.get("text", ""))
                content = "".join(text_parts).strip()
            else:
                # 流式解析 SSE 事件
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
                    # 如果流式报错，但已经截获到完整高质量正文，且长度达标，则宽容使用
                    if len(content) >= 1200 and "板块三" in content:
                        print(f"  ⚠️ 流式中途收到结束提示但正文已完整接收 ({len(content)} 字符)，正常采纳。")
                        return content
                    raise RuntimeError(stream_error)

            if len(content) < 800:
                raise RuntimeError(f"返回内容过短 ({len(content)} 字符)")

            print(f"  🎉 {model} 生成成功！耗时: {time.time() - t0:.2f}s, 字符数: {len(content)}")
            return content

        except Exception as e:
            last_err = e
            err_str = str(e)
            print(f"  ⚠️ [尝试 {attempt}/{max_retries} 失败] {err_str[:160]}")
            if attempt < max_retries:
                backoff = attempt * 3
                print(f"  🔄 触发自动指数退避自愈，等待 {backoff}s 后执行下一次重试...")
                time.sleep(backoff)

    raise RuntimeError(f"云端主力引擎 ({model}) 在 {max_retries} 次尝试后仍未成功: {last_err}")


# 向下兼容别名
generate_digest_with_aizex = generate_digest_with_gpt_sol
generate_digest_with_codex = generate_digest_with_gpt_sol


def extract_engine_label(text: str) -> tuple:
    """
    检查 Markdown 文本开头是否包含 <!-- ENGINE_LABEL: ... --> 元数据注释。
    返回 (clean_text, engine_label)。
    确保跨端（如本地夜间预构建落盘后次日由云端读取推送）时，能 100% 精确还原实际执笔模型。
    """
    if not text:
        return "", "本地多源数据底座"
    m = re.match(r"^\s*<!--\s*ENGINE_LABEL:\s*([^\n>]+?)\s*-->\s*\n?", text, re.IGNORECASE)
    if m:
        label = m.group(1).strip()
        clean = text[m.end():]
        return clean, label

    # 启发式兜底判断
    if "【大模型接口提示】" in text or "本地多源数据底座为您完整生成" in text:
        return text, "本地多源数据底座"
    if "Gemini" in text[:300]:
        return text, "Gemini 3.8 Flash"
    return text, "GPT-6.1-sol (High)"


def tag_content_with_engine(content: str, label: str) -> str:
    """在 Markdown 开头打入元数据注释，并在顶部导语处明确标注实际执笔模型"""
    meta_tag = f"<!-- ENGINE_LABEL: {label} -->\n"
    pattern = r"(> 💡 \*\*今日早报速览\*\*：[^\n]+)"
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
    纯粹二级高可用自愈容灾调度架构：
    1. 第一级（本地优先）：本地网关 (Gemini 3.8 Flash，仅本机且配置 LOCAL_LLM_API_KEY 时生效；云端 Actions 或未配 Key 静默跳过)
    2. 第二级（云端/备援）：GPT-6.1-sol (High) (gpt-6.1-sol + high 深度思考，多节点轮换 + 3 次重试自愈 + CODEX/AIZEX 双 Key 互备)
    3. 终极自愈兜底：本地多源数据底座应急早报生成
    """
    is_github_actions = (os.getenv("GITHUB_ACTIONS") or "").strip().lower() == "true"
    local_key  = (os.getenv("LOCAL_LLM_API_KEY") or "").strip()
    codex_key  = (os.getenv("CODEX_API_KEY") or "").strip()
    aizex_key  = (os.getenv("AIZEX_API_KEY") or "").strip()

    errors = []

    def try_local() -> Optional[tuple]:
        # 云端 Actions 环境或未配置本地 Key，直接静默跳过，绝不追加任何错误信息
        if is_github_actions or not local_key:
            return None
        print("\n🧠 调度大模型引擎: 本地网关 (Gemini 3.8 Flash)...")
        try:
            content = generate_digest_with_local(global_news, ai_news, github_projects, github_classic_gems, local_key, blacklist_summary=blacklist_summary)
            return content, "Gemini 3.8 Flash"
        except Exception as e:
            err = f"本地网关失败: {e}"
            errors.append(err)
            print(f"  [降级触发] {err}，自动切换至云端主力引擎...")
            return None

    def try_gpt_sol() -> Optional[tuple]:
        # 搜集有效凭据列表 (CODEX_API_KEY / AIZEX_API_KEY)
        candidate_keys = []
        for k in [codex_key, aizex_key]:
            if k and k not in candidate_keys:
                candidate_keys.append(k)

        if not candidate_keys:
            errors.append("主力云端: 未配置 CODEX_API_KEY 或 AIZEX_API_KEY")
            return None

        print("\n🧠 调度主力云端引擎: GPT-6.1-sol (High)...")

        for idx, key in enumerate(candidate_keys, 1):
            key_mask = key[:6] + "..." + key[-4:] if len(key) > 12 else "***"
            if len(candidate_keys) > 1:
                print(f"  🔑 使用凭据 [{idx}/{len(candidate_keys)}]: {key_mask}")
            try:
                content = generate_digest_with_gpt_sol(global_news, ai_news, github_projects, github_classic_gems, key, blacklist_summary=blacklist_summary)
                return content, "GPT-6.1-sol (High)"
            except Exception as e:
                err = f"GPT-6.1-sol (凭据 {key_mask}) 失败: {e}"
                errors.append(err)
                print(f"  [降级触发] {err}")
                if idx < len(candidate_keys):
                    print("  🔄 尝试切换下一个有效 API Key 凭据...")
        return None

    # 调度序列：本地环境优先尝试本地网关，云端 Actions 优先尝试云端 GPT-6.1-sol (High)
    if not is_github_actions and local_key:
        order = [try_local, try_gpt_sol]
    else:
        order = [try_gpt_sol]

    for engine_fn in order:
        res = engine_fn()
        if res:
            content, label = res
            return tag_content_with_engine(content, label)

    # 终极兜底：本地高质量数据底座应急早报生成
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

    if "403" in reason or "denied" in reason.lower():
        lines.extend([
            "> ⚠️ **【Gemini 权限提示】**：当前 Google Gemini API Key 返回 `403 PERMISSION_DENIED (Your project has been denied access)`。这是由于 Google 自动化安全系统对新账号或新项目的拦截。解决方案：在 [Google AI Studio](https://aistudio.google.com/) 重新点击「Create API key」选择新建一个 Project 生成新 Key 替换即可恢复大模型云端增强。今日早报已由本地多源数据底座为您完整生成送达！",
            ""
        ])
    elif reason:
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
    nv_tsla_items = [it for it in ai_news if any(k in it['title'].lower() for k in ['nvidia', '英伟达', 'huang', '黄仁勋', 'blackwell', 'cuda', 'tesla', '特斯拉', 'fsd', 'optimus', 'musk', '马斯克', 'huawei', '华为', '鸿蒙', 'harmonyos', '昇腾', 'apple', '苹果', 'iphone', '库克'])]
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
    active_label = engine_label or extracted_label or "GPT-6.1-sol (High)"
    if "本地多源数据底座" in active_label:
        sub_title = f"由 {active_label} 聚合生成 · 自动化每日送达"
    else:
        sub_title = f"由 {active_label} 深度提炼 · 自动化每日送达"

    html_lines = []
    in_list = False

    def format_inline(text: str) -> str:
        # 超链接转换 [text](url)
        text = re.sub(
            r'\[(.*?)\]\((.*?)\)',
            r'<a href="\2" target="_blank" style="color:#1d63ed;text-decoration:none;font-weight:500;">\1</a>',
            text
        )
        # 加粗转换 **text**
        text = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color:#0f172a;font-weight:600;">\1</strong>', text)
        # 行内代码 `code`
        text = re.sub(
            r'`(.*?)`',
            r'<code style="background:#f1f5f9;color:#0f172a;padding:2px 5px;border-radius:4px;font-family:Consolas,Monaco,monospace;font-size:88%;">\1</code>',
            text
        )
        return text

    for line in clean_text.splitlines():
        line_s = line.strip()
        if not line_s:
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            continue

        if line_s.startswith('---'):
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            html_lines.append('<hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0;">')
            continue

        if line_s.startswith('# '):
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            h_text = format_inline(line_s[2:].strip())
            html_lines.append(f'<h1 style="margin:20px 0 12px;color:#0f172a;font-size:20px;font-weight:700;text-align:center;">{h_text}</h1>')
            continue

        if line_s.startswith('## '):
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            h_text = format_inline(line_s[3:].strip())
            html_lines.append(f'<h2 style="margin:24px 0 12px;color:#1e293b;font-size:17px;font-weight:700;border-bottom:2px solid #e2e8f0;padding-bottom:6px;">{h_text}</h2>')
            continue

        if line_s.startswith('### '):
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            h_text = format_inline(line_s[4:].strip())
            html_lines.append(f'<h3 style="margin:18px 0 8px;color:#2563eb;font-size:15px;font-weight:600;border-left:3px solid #2563eb;padding-left:8px;">{h_text}</h3>')
            continue

        if line_s.startswith('#### '):
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            h_text = format_inline(line_s[5:].strip())
            html_lines.append(f'<h4 style="margin:14px 0 6px;color:#334155;font-size:14px;font-weight:600;">{h_text}</h4>')
            continue

        if line_s.startswith('> '):
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            q_text = format_inline(line_s[2:].strip())
            html_lines.append(f'<blockquote style="margin:10px 0;padding:10px 14px;background:#f8fafc;border-left:4px solid #3b82f6;border-radius:0 6px 6px 0;color:#475569;font-size:14px;line-height:1.6;">{q_text}</blockquote>')
            continue

        if line_s.startswith('- ') or line_s.startswith('* '):
            if not in_list:
                html_lines.append('<ul style="margin:8px 0;padding-left:20px;line-height:1.65;color:#334155;">')
                in_list = True
            li_text = format_inline(line_s[2:].strip())
            html_lines.append(f'<li style="margin:4px 0;">{li_text}</li>')
            continue

        if in_list:
            html_lines.append('</ul>')
            in_list = False

        p_text = format_inline(line_s)
        html_lines.append(f'<p style="margin:8px 0;line-height:1.65;color:#334155;font-size:14px;">{p_text}</p>')

    if in_list:
        html_lines.append('</ul>')

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
    """分发早报到 QQ 邮箱，并保存本地 preview_digest.md 预览与归档"""
    clean_content, engine_label = extract_engine_label(content)
    now_bj = datetime.now(BEIJING_TZ)
    title = f"每日 AI & 全球资讯早报 | {now_bj.strftime('%Y-%m-%d')}"

    # 1. 无论是否配置外部推送，均先将最新早报保存为本地 Markdown 文件供预览与归档
    preview_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview_digest.md")
    try:
        with open(preview_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"\n📄 最新早报已同步保存至本地预览文件: {preview_path} (执笔模型: {engine_label})")
    except Exception as e:
        print(f"  [警告] 保存本地 preview_digest.md 失败: {e}")

    # 持久化去重记录
    record_history_seen(now_bj.strftime("%Y-%m-%d"), clean_content)

    # 2. 检查 QQ 邮箱推送配置
    email_enabled = bool(os.getenv("EMAIL_SENDER") and os.getenv("EMAIL_PASSWORD"))
    if not email_enabled:
        print("\n⚠️ [提示] 未配置 EMAIL_SENDER 或 EMAIL_PASSWORD，跳过邮件推送。")
        print("   -> 您可直接打开 preview_digest.md 查看生成的早报。")
        return True

    return send_via_email(title, content)


# ==================== 主流程执行入口 ====================
def main():
    print("=" * 60)
    print("🚀 每日 AI & 全球资讯早报生成系统 启动")
    now_bj = datetime.now(BEIJING_TZ)
    print(f"⏰ 当前北京时间: {now_bj.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 预生成模式：本机夜间生成明天的早报并落盘，不推送；云端定时任务读取后发送
    prebuild = (os.getenv("PREBUILD_MODE") or "").strip() == "1"
    if not prebuild:
        prebuilt = load_prebuilt_digest(now_bj.strftime("%Y-%m-%d"))
        if prebuilt:
            print(f"\n📦 发现今日预生成早报 ({len(prebuilt)} 字符)，直接推送，跳过大模型。")
            dispatch_notifications(prebuilt)
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
    dispatch_notifications(digest_content)
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

