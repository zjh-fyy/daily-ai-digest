#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日 AI & 全球资讯早报 (Daily AI & Global Digest)
--------------------------------------------------
自动化多源数据抓取 -> Google Gemini 深度提炼 (带 Google Search Grounding) -> PushPlus 微信推送

核心功能板块：
1. 🌍 全球时事（前因后果、多方视角、背景脉络与权威来源）
2. 🤖 指定 AI 追踪（OpenAI/Codex, Google/Gemini/Antigravity, Anthropic/Claude + 最新福利羊毛专栏）
3. 🔥 GitHub 热门开源（AI/数码/高星爆发，通俗大白话场景化解读）
"""

import os
import sys
import re
import json
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

# 保证在各类控制台（尤其 Windows GBK 默认终端）下输出中文与 Emoji 不发生 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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
                    "source": source_name
                })
        print(f"     ✅ 成功获取 {len(items)} 条资讯 ({source_name})")
    except Exception as e:
        print(f"     ❌ 解析 RSS 失败 ({source_name}): {e}")

    return items


# ==================== 数据采集模块 ====================
def collect_global_news() -> List[Dict[str, str]]:
    """采集全球重大时事政经资讯（多渠道融合）"""
    print("\n[1/3] 正在采集【全球重大时事】资讯...")
    sources = [
        ("http://feeds.bbci.co.uk/news/world/rss.xml", "BBC World News", 6),
        ("https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "Google News (中文热点)", 8),
        ("https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en", "Google News (US Top Stories)", 6),
    ]

    all_news = []
    seen_titles = set()

    for url, name, limit in sources:
        feed_items = fetch_rss_feed(url, name, max_items=limit)
        for item in feed_items:
            # 基础去重（防止相同或高相似标题堆积）
            title_key = item["title"][:25].lower()
            if title_key not in seen_titles:
                seen_titles.add(title_key)
                all_news.append(item)

    print(f"  => 全球时事汇总去重后共 {len(all_news)} 条数据底座")
    return all_news


def collect_ai_news() -> List[Dict[str, str]]:
    """采集指定 AI 核心生态（OpenAI/Google/Anthropic）及福利优惠资讯"""
    print("\n[2/3] 正在采集【指定 AI 追踪 & 优惠福利】资讯...")
    all_ai_items = []
    seen_titles = set()

    # 1. OpenAI 官方新闻
    openai_feed = fetch_rss_feed("https://openai.com/news/rss.xml", "OpenAI Official News", max_items=5)
    for it in openai_feed:
        all_ai_items.append(it)
        seen_titles.add(it["title"][:25].lower())

    # 2. Google News 专项靶向检索（过去 1-2 天关于三大家和福利）
    targeted_queries = [
        ('("OpenAI" OR "Codex" OR "GPT") when:2d', "OpenAI / Codex 检索", "en-US", "US:en"),
        ('("Gemini" OR "Antigravity" OR "Google AI Studio") when:2d', "Google Gemini / Antigravity 检索", "en-US", "US:en"),
        ('("Claude" OR "Anthropic" OR "Claude Code") when:2d', "Anthropic Claude 检索", "en-US", "US:en"),
        ('("AI" AND ("free tier" OR "student discount" OR "credits" OR "免费额度" OR "福利")) when:3d', "AI 福利优惠 (中文检索)", "zh-CN", "CN:zh-Hans"),
        ('("AI" AND ("free credits" OR "student discount" OR "free tier" OR "giveaway")) when:3d', "AI Perks / Credits (Global)", "en-US", "US:en")
    ]

    for q, label, hl, ceid in targeted_queries:
        encoded_q = urllib.parse.quote(q)
        search_rss_url = f"https://news.google.com/rss/search?q={encoded_q}&hl={hl}&gl=US&ceid={ceid}"
        feed_items = fetch_rss_feed(search_rss_url, label, max_items=4)
        for it in feed_items:
            t_key = it["title"][:25].lower()
            if t_key not in seen_titles:
                seen_titles.add(t_key)
                all_ai_items.append(it)

    # 3. Hacker News 高分 AI 深度讨论帖（>= 20 points）
    hn_url = "https://hnrss.org/newest?q=GPT+OR+Claude+OR+Gemini+OR+Codex+OR+Antigravity&points=20"
    hn_items = fetch_rss_feed(hn_url, "Hacker News (Top AI Discussions)", max_items=5)
    for it in hn_items:
        t_key = it["title"][:25].lower()
        if t_key not in seen_titles:
            seen_titles.add(t_key)
            all_ai_items.append(it)

    print(f"  => AI 核心生态汇总去重后共 {len(all_ai_items)} 条数据底座")
    return all_ai_items


def collect_github_trending() -> List[Dict[str, Any]]:
    """抓取今日 GitHub Trending 热门榜单，自带 GitHub Search API 优雅降级"""
    print("\n[3/3] 正在抓取【GitHub Trending 今日热门】...")
    trending_url = "https://github.com/trending?since=daily"
    resp = http_get(trending_url)
    projects = []

    if resp:
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.select("article.Box-row")
            print(f"  -> GitHub Trending HTML 解析成功，共扫描到 {len(rows)} 个项目")

            for row in rows[:12]:
                title_elem = row.select_one("h2 a")
                if not title_elem:
                    continue

                repo_path = title_elem.get("href", "").strip().lstrip("/")
                repo_url = f"https://github.com/{repo_path}"
                name = re.sub(r"\s+", "", title_elem.text)

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
                print(f"  => 成功提取 GitHub Trending 前 {len(projects)} 个项目")
                return projects
        except Exception as e:
            print(f"  ❌ 解析 GitHub Trending HTML 发生异常: {e}")

    # Fallback: 使用 GitHub Search API 获取最近 7 天内新成立的高星热点项目
    print("  [降级触发] 启动 GitHub Search API 备用方案...")
    try:
        now_bj = datetime.now(BEIJING_TZ)
        seven_days_ago = (now_bj - timedelta(days=7)).strftime("%Y-%m-%d")
        api_url = f"https://api.github.com/search/repositories?q=created:>{seven_days_ago}&sort=stars&order=desc&per_page=10"
        api_resp = http_get(api_url)
        if api_resp:
            data = api_resp.json()
            for repo in data.get("items", [])[:8]:
                projects.append({
                    "name": repo.get("full_name", ""),
                    "url": repo.get("html_url", ""),
                    "description": repo.get("description", "暂无描述"),
                    "language": repo.get("language", "Unknown"),
                    "stars_today": f"+{repo.get('stargazers_count', 0)} (近期飙升)",
                    "total_stars": str(repo.get("stargazers_count", 0))
                })
            print(f"  => GitHub Search API 备用方案成功抓取 {len(projects)} 个飙升项目")
    except Exception as e:
        print(f"  ❌ GitHub Search API 降级方案亦失败: {e}")

    return projects


# ==================== Gemini API 提炼模块 ====================
def get_candidate_models(client) -> List[str]:
    """
    动态探测当前 API Key 支持的最新 Gemini 模型并按代际降序排序：
    1. 优先调用 client.models.list() 动态检索可用模型
    2. 过滤非文本生成模型 (embedding, imagen, veo, tts, vision, aqa 等)
    3. 正则提取代际版本号 (如 4.x > 3.8 > 2.5 > 2.0)，同代际优先选择 flash，其次 pro
    4. 即使 models.list() 异常，亦保留静态兜底列表确保可用
    5. 若用户设置了环境变量 GEMINI_MODEL，置顶优先尝试
    """
    discovered: List[str] = []

    # 静态兜底列表（包含最新代际别名及主流稳定模型）
    static_fallback = [
        "gemini-flash-latest",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-2.5-pro",
        "gemini-1.5-flash",
    ]

    try:
        print("  🔍 正在动态探测当前 API Key 支持的最新 Gemini 模型列表...")
        for model in client.models.list():
            raw_name = getattr(model, "name", "") or ""
            # 去除前缀 'models/' 或 'publishers/google/models/'
            name = raw_name.split("/")[-1].strip()

            if not name.startswith("gemini-"):
                continue

            # 过滤非文本生成的专业模型
            excluded_keywords = [
                "embedding", "embed", "imagen", "veo", "tts",
                "vision", "aqa", "deep-research", "live", "whisper", "audio"
            ]
            if any(k in name.lower() for k in excluded_keywords):
                continue

            # 若有 supported_actions，检查是否支持 generateContent
            supported_actions = getattr(model, "supported_actions", None)
            if supported_actions and not any("generatecontent" in str(act).lower() for act in supported_actions):
                continue

            discovered.append(name)

        if discovered:
            def score_model(m_name: str) -> tuple:
                # 提取代际版本号，如 gemini-4-flash -> 4.0, gemini-3.8-flash -> 3.8, gemini-2.5-flash -> 2.5
                match = re.search(r"gemini-(\d+(?:\.\d+)?)", m_name)
                ver = float(match.group(1)) if match else (999.0 if "latest" in m_name else 0.0)

                # 同代际类型权重：flash 正式 > flash 预览/实验 > pro 正式 > pro 预览/实验 > 其他
                if "flash" in m_name:
                    type_score = 4 if ("exp" not in m_name and "preview" not in m_name) else 3
                elif "pro" in m_name:
                    type_score = 2 if ("exp" not in m_name and "preview" not in m_name) else 1
                else:
                    type_score = 0
                return (ver, type_score)

            discovered.sort(key=score_model, reverse=True)
    except Exception as e:
        print(f"  [提示] 动态探测模型列表异常 ({e})，启用静态兜底模型列表")

    # 去重组合：动态发现的模型优先，静态兜底补充
    candidate_models: List[str] = []
    seen = set()
    for m in discovered + static_fallback:
        if m not in seen:
            seen.add(m)
            candidate_models.append(m)

    # 若用户设置了环境变量 GEMINI_MODEL，将其排在第一位优先尝试
    user_model = os.getenv("GEMINI_MODEL", "").strip()
    if user_model:
        if user_model in candidate_models:
            candidate_models.remove(user_model)
        candidate_models.insert(0, user_model)
        print(f"  📌 检测到环境变量 GEMINI_MODEL={user_model}，已置顶为最高优先级候选！")

    print(f"  🔍 已探测到当前 API Key 支持的最新模型候选列表: {candidate_models[:6]}")
    return candidate_models


def build_digest_prompt(
    global_news: List[Dict[str, str]],
    ai_news: List[Dict[str, str]],
    github_projects: List[Dict[str, Any]]
) -> str:
    """构建供大模型 (DeepSeek / Gemini) 提炼的统一标准 Prompt"""
    now_bj = datetime.now(BEIJING_TZ)
    date_str = now_bj.strftime("%Y年%m月%d日")
    weekday_map = {0: "星期一", 1: "星期二", 2: "星期三", 3: "星期四", 4: "星期五", 5: "星期六", 6: "星期日"}
    weekday_str = weekday_map[now_bj.weekday()]

    context_data = {
        "report_date": f"{date_str} {weekday_str} (北京时间)",
        "raw_global_news": global_news[:14],
        "raw_ai_news": ai_news[:14],
        "raw_github_trending": github_projects[:10]
    }
    context_json = json.dumps(context_data, ensure_ascii=False, indent=2)

    prompt = f"""
你是一位殿堂级科技主编与资深国际政治经济观察家。请基于我为你采集的【原始数据底座】，为微信移动端读者精编一份高质量、兼具深度与可读性的早报。

### 📅 今日日期
{date_str} {weekday_str}

### 📦 原始数据底座 (JSON)
{context_json}

---

### 📝 结构与撰写准则（必须严格执行）：

#### 🌅 标题：`每日 AI & 全球资讯早报 | {date_str} {weekday_str}`
简练导语（30~50字，点出今日最具震撼力的 1~2 个看点）。

#### 🌍 板块一：全球时事（重深度、前因后果与多方影响）
- 精选过去 24 小时最关键的 **4~6 条**全球重大政经、科技或地缘热点。
- 绝不是简单一句话新闻列表！每条内容必须包含：
  - **核心事件**：事实准确，时间、地点、主体明确。
  - **前因后果与脉络**：为什么会发生？背后有什么历史背景、经济博弈或政策动机？
  - **多方视角与深层影响**：各方表态如何？对全球经济、产业链格局或普通大众有何长远影响？
  - **权威来源**：在末尾必须附带可点击的 Markdown 链接，如 `[🔗 来源：BBC](链接)`。

#### 🤖 板块二：指定 AI 核心追踪（OpenAI / Google / Anthropic）
- 必须重点聚焦这三巨头：
  1. **OpenAI (GPT / Codex)**：近期重磅更新、模型能力、API 变动或生态战况。
  2. **Google (Gemini / AI Studio / Antigravity)**：Google 最前沿模型发布、IDE/Agent 进展与开发技巧。
  3. **Anthropic (Claude / Claude Code)**：Claude 最新版本能力、代码工具或行业实践。
- **🎁 【今日福利与优惠活动】专项子栏目（重点！）**：
  - 检索核实三大家或主流 AI 平台今天是否有最新的免费额度发放、学生认证优惠、API 赠金、限时限免或高性价比平替白嫖方式。
  - **铁律**：如果今日确实无新增的官方福利变动，请如实标明“今日暂无新增官方福利变动”，严禁凭空捏造虚假活动。

#### 🔥 板块三：GitHub 热门开源项目（通俗大白话场景化解读）
- 精选 **5~6 个**项目（优先 AI 应用、数码/生产力工具，以及近期星标暴涨的爆款黑马）。
- 每个项目必须具备以下排版：
  - `### 序号. [项目名](链接) 🌟 今日 +新增Stars / 总Stars | 语言`
  - **💡 这项目到底是干啥的？**：必须用通俗直观的大白话打比方或场景化描述（坚决杜绝生硬技术黑话堆砌，非技术读者也能一眼看懂）。
  - **🎯 解决了什么痛点 / 好玩在哪？**：说明对开发者或普通人的实际用途、好玩点或替代了什么昂贵工具。

---

### 📱 微信端排版与格式规范：
1. 语言：通俗、生动、凝练的简体中文。
2. 格式：清晰优雅的 Markdown 格式（使用 `#`, `##`, `###`, `**加粗**`, `- 列表`, `>` 引用块）。
3. 避免在微信中渲染效果差的复杂 HTML 语法或多列宽表格，以引用卡片和列表为主，确保手机小屏沉浸阅读。
4. 【完整性铁律】：必须完整输出全部三大板块（板块一：全球时事、板块二：指定 AI 核心追踪 + 福利专栏、板块三：GitHub 热门开源），切勿写得冗长导致后半部分被截断！每条要闻/项目控制在 150~200 字，精准提炼。
"""
    return prompt


def generate_digest_with_codex(
    global_news: List[Dict[str, str]],
    ai_news: List[Dict[str, str]],
    github_projects: List[Dict[str, Any]],
    api_key: str
) -> str:
    """
    调用用户专属 Codex API (基于 OpenAI /v1/responses 协议，带自动节点故障转移与深度思考)
    主节点：https://ca.memofun.net/v1 (香港原生高速直连)
    备用节点：https://codex-api.aizex.net/v1 (Cloudflare 全球加速)
    """
    codex_model = (os.getenv("CODEX_MODEL") or "gpt-6.1-sol").strip()
    codex_effort = (os.getenv("CODEX_EFFORT") or "medium").strip().lower()

    valid_efforts = {"low", "medium", "high", "xhigh", "max"}
    if codex_effort not in valid_efforts:
        codex_effort = "medium"

    endpoints = [
        "https://ca.memofun.net/v1",       # 主节点
        "https://codex-api.aizex.net/v1"   # 备用节点
    ]

    prompt = build_digest_prompt(global_news, ai_news, github_projects)

    payload = {
        "model": codex_model,
        "input": prompt,
        "instructions": (
            "你是一位殿堂级科技主编与资深国际政治经济观察家。请基于提供的数据底座，"
            "为移动端读者精编一份结构严谨、前因后果清晰、大白话解读开源项目、兼具深度与可读性的每日 AI & 全球资讯早报。"
            "请务必完整输出三大板块，切勿遗漏。"
        ),
        "reasoning": {
            "effort": codex_effort
        }
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT
    }

    last_error = None
    for b_url in endpoints:
        endpoint = f"{b_url}/responses"
        print(f"  🚀 正在向 Codex API 发送请求 (模型: {codex_model}, 思考深度: {codex_effort}, 节点: {b_url}) ...")
        t0 = time.time()
        try:
            resp = requests.post(endpoint, headers=headers, json=payload, timeout=150)
            elapsed = time.time() - t0
            if resp.status_code == 200:
                data = resp.json()
                output_items = data.get("output", [])
                text_parts = []
                for item in output_items:
                    if item.get("type") == "message":
                        for c in item.get("content", []):
                            if c.get("type") == "output_text":
                                text_parts.append(c.get("text", ""))

                final_text = "".join(text_parts).strip()
                if not final_text:
                    raise RuntimeError("Codex API 返回内容为空")

                usage = data.get("usage", {})
                reasoning_tokens = usage.get("output_tokens_details", {}).get("reasoning_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                input_tokens = usage.get("input_tokens", 0)

                print(
                    f"  🎉 Codex API ({codex_model}) 生成成功！耗时: {elapsed:.2f}s, "
                    f"输入Token: {input_tokens}, 输出Token: {output_tokens} (含思考: {reasoning_tokens}), 字符数: {len(final_text)}"
                )
                return final_text
            else:
                err_msg = f"节点 {b_url} 返回 HTTP {resp.status_code}: {resp.text[:250]}"
                print(f"  [节点告警] {err_msg}，准备尝试备用节点...")
                last_error = RuntimeError(err_msg)
        except Exception as e:
            elapsed = time.time() - t0
            err_msg = f"节点 {b_url} 请求异常 ({elapsed:.2f}s): {e}"
            print(f"  [节点故障] {err_msg}，准备尝试备用节点...")
            last_error = e

    raise last_error or RuntimeError("所有 Codex 节点均未能成功响应")


def generate_digest_with_deepseek(
    global_news: List[Dict[str, str]],
    ai_news: List[Dict[str, str]],
    github_projects: List[Dict[str, Any]],
    api_key: str
) -> str:
    """调用 DeepSeek 官方 API (或 OpenAI 兼容格式接口) 生成高质量早报"""
    base_url = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip().rstrip("/")
    model = (os.getenv("DEEPSEEK_MODEL") or "deepseek-v4-pro").strip()

    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT
    }

    prompt = build_digest_prompt(global_news, ai_news, github_projects)

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是一位殿堂级科技主编与资深国际政治经济观察家。请基于提供的数据底座，为微信移动端读者精编一份结构严谨、前因后果清晰、大白话解读开源项目、兼具深度与可读性的每日 AI & 全球资讯早报。请务必完整输出三大板块，切勿遗漏。"
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.3,
        "max_tokens": 8192
    }

    print(f"  🚀 正在向 DeepSeek API 发送生成请求 (模型: {model}, 接口: {url}) ...")
    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    if resp.status_code == 200:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        print(f"  🎉 DeepSeek 生成成功！模型: {model}, 消耗 Token: {usage.get('total_tokens', 'N/A')}, 字符数: {len(content)}")
        return content
    else:
        err_msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
        raise RuntimeError(err_msg)


def generate_digest_with_gemini(
    global_news: List[Dict[str, str]],
    ai_news: List[Dict[str, str]],
    github_projects: List[Dict[str, Any]]
) -> str:
    """调用 Google Gemini 最新可用模型，配合 Google Search Grounding 深度提炼生成早报"""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("环境变量 GEMINI_API_KEY 未设置！请在 .env 或 GitHub Secrets 中配置。")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    # 动态探测候选模型列表
    candidate_models = get_candidate_models(client)
    prompt = build_digest_prompt(global_news, ai_news, github_projects)

    # 尝试调用策略：
    # 1. 优先按最新候选模型列表开启 Google Search Grounding (联网核实)
    # 2. 降级备用：若联网工具受限或报错，按最新模型顺序关闭 Grounding (依托本地丰富的预抓取数据底座)
    strategies = []
    for m in candidate_models[:5]:
        strategies.append((m, True))
    for m in candidate_models[:5]:
        strategies.append((m, False))

    last_error = ""
    for model_name, use_grounding in strategies:
        mode_desc = f"{model_name} (联网 Grounding: {'开启' if use_grounding else '关闭'})"
        print(f"\n[Gemini 调度] 尝试调用模型: {mode_desc} ...")
        try:
            config = types.GenerateContentConfig(
                temperature=0.7,
            )
            if use_grounding:
                config.tools = [types.Tool(google_search=types.GoogleSearch())]

            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )

            if response and response.text:
                print(f"  🎉 Gemini 提炼生成成功！实际使用模型: {model_name} (联网 Grounding: {'开启' if use_grounding else '关闭'}), 输出字符数: {len(response.text)}")
                return response.text
            else:
                print(f"  [重试] 模型 {model_name} 返回内容为空，尝试下一候选策略...")
        except Exception as e:
            last_error = str(e)
            print(f"  [降级原因] {mode_desc} 执行失败: {e}")
            time.sleep(1)

    raise RuntimeError(f"所有 Gemini 候选模型与调用策略均失败: {last_error[:100]}")


def generate_digest(
    global_news: List[Dict[str, str]],
    ai_news: List[Dict[str, str]],
    github_projects: List[Dict[str, Any]]
) -> str:
    """
    智能大模型生成中枢：
    支持多引擎自适应优先级调度：
    1. 默认 PRIMARY_ENGINE=codex:
       Codex API (gpt-6.1-sol + medium 思考) -> DeepSeek (deepseek-v4-pro) -> Gemini -> 本地应急兜底
    2. 若 PRIMARY_ENGINE=deepseek:
       DeepSeek (deepseek-v4-pro) -> Codex API (gpt-6.1-sol) -> Gemini -> 本地应急兜底
    """
    codex_key = (os.getenv("CODEX_API_KEY") or "").strip()
    deepseek_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    gemini_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    primary_engine = (os.getenv("PRIMARY_ENGINE") or "codex").strip().lower()

    # 智能防呆识别：如果用户将 sk-azx-... 的 Codex Key 误填在其他变量中
    if not codex_key:
        if deepseek_key.startswith("sk-azx-"):
            codex_key = deepseek_key
            deepseek_key = ""
            print("  💡 [智能防呆识别] 检测到 DEEPSEEK_API_KEY 为 Codex 专属 Key，已自动映射为 CODEX_API_KEY！")
        elif gemini_key.startswith("sk-azx-"):
            codex_key = gemini_key
            gemini_key = ""
            print("  💡 [智能防呆识别] 检测到 GEMINI_API_KEY 为 Codex 专属 Key，已自动映射为 CODEX_API_KEY！")

    # 智能防呆识别：如果用户将 sk-... 开头的 DeepSeek Key 填在 GEMINI_API_KEY 中
    if not deepseek_key and gemini_key.startswith("sk-") and not gemini_key.startswith("sk-azx-"):
        deepseek_key = gemini_key
        print("  💡 [智能防呆识别] 检测到 GEMINI_API_KEY 格式为 'sk-...'，自动识别为 DeepSeek API Key！")

    last_error = ""

    def try_codex() -> Optional[str]:
        nonlocal last_error
        if codex_key:
            model_name = (os.getenv("CODEX_MODEL") or "gpt-6.1-sol").strip()
            effort_level = (os.getenv("CODEX_EFFORT") or "medium").strip().lower()
            print(f"\n🧠 调度主力大模型引擎: Codex API ({model_name} + {effort_level} 深度思考)...")
            try:
                return generate_digest_with_codex(global_news, ai_news, github_projects, codex_key)
            except Exception as e:
                last_error = f"Codex API 失败: {e}"
                print(f"  [降级触发] {last_error}，自动切换至备用引擎...")
        return None

    def try_deepseek() -> Optional[str]:
        nonlocal last_error
        if deepseek_key:
            model_name = (os.getenv("DEEPSEEK_MODEL") or "deepseek-v4-pro").strip()
            print(f"\n🧠 调度大模型引擎: DeepSeek ({model_name})...")
            try:
                return generate_digest_with_deepseek(global_news, ai_news, github_projects, deepseek_key)
            except Exception as e:
                last_error = f"DeepSeek API 失败: {e}"
                print(f"  [降级触发] {last_error}，自动切换至下一备用引擎...")
        return None

    def try_gemini() -> Optional[str]:
        nonlocal last_error
        if gemini_key and not gemini_key.startswith("sk-"):
            print("\n🧠 调度备用大模型引擎: Google Gemini (动态模型)...")
            try:
                return generate_digest_with_gemini(global_news, ai_news, github_projects)
            except Exception as e:
                last_error = f"Gemini API 失败: {e}"
                print(f"  [降级触发] {last_error}，启用本地应急提炼引擎...")
        return None

    # 根据 primary_engine 确定调用顺序
    if primary_engine == "deepseek":
        order = [try_deepseek, try_codex, try_gemini]
    else:
        order = [try_codex, try_deepseek, try_gemini]

    for engine_fn in order:
        result = engine_fn()
        if result:
            return result

    # 终极兜底：本地高质量数据底座应急早报生成
    print(f"\n⚠️ [自动容灾] 启用本地多源数据底座应急早报生成引擎 (原因: {last_error[:80] if last_error else '未配置可用大模型 Key'})...")
    return generate_fallback_digest(global_news, ai_news, github_projects, reason=last_error)


def generate_fallback_digest(
    global_news: List[Dict[str, str]],
    ai_news: List[Dict[str, str]],
    github_projects: List[Dict[str, Any]],
    reason: str = ""
) -> str:
    """当 Gemini API 遭遇 Google 云端权限风控或网络熔断时，依托本地采集的高质量多源数据底座生成应急早报"""
    now_bj = datetime.now(BEIJING_TZ)
    date_str = now_bj.strftime("%Y年%m月%d日")
    weekday_map = {0: "星期一", 1: "星期二", 2: "星期三", 3: "星期四", 4: "星期五", 5: "星期六", 6: "星期日"}
    weekday_str = weekday_map[now_bj.weekday()]

    lines = [
        f"# 🌅 每日 AI & 全球资讯早报 | {date_str} {weekday_str}",
        "",
        "> 💡 **今日早报速览**：多源实时资讯已采集就绪，为您精选全球重大时事、AI 巨头最新动向与 GitHub 热门开源黑马项目。",
        ""
    ]

    if "403" in reason or "denied" in reason.lower():
        lines.extend([
            "> ⚠️ **【Gemini 权限提示】**：当前 Google Gemini API Key 返回 `403 PERMISSION_DENIED (Your project has been denied access)`。这是由于 Google 自动化安全系统对新账号或新项目的拦截。解决方案：在 [Google AI Studio](https://aistudio.google.com/) 重新点击「Create API key」选择新建一个 Project 生成新 Key 替换即可恢复大模型云端增强。今日早报已由本地多源数据底座为您完整生成送达！",
            ""
        ])
    elif reason:
        lines.extend([
            f"> ⚠️ **【Gemini 接口提示】**：云端大模型接口返回异常 (`{reason[:100]}`)。今日早报已由本地多源数据底座为您完整生成送达！",
            ""
        ])

    lines.extend([
        "## 🌍 板块一：全球时事（深度精选）",
        ""
    ])
    for i, item in enumerate(global_news[:5], 1):
        lines.append(f"### {i}. [{item['title']}]({item['link']})")
        lines.append(f"- **核心要闻**：{item['summary']}")
        lines.append(f"- **权威来源**：[🔗 来源：{item['source']}]({item['link']})")
        lines.append("")

    lines.extend([
        "## 🤖 板块二：指定 AI 核心追踪（OpenAI / Google / Anthropic）",
        ""
    ])
    for i, item in enumerate(ai_news[:5], 1):
        lines.append(f"### {i}. [{item['title']}]({item['link']})")
        if item.get("summary"):
            lines.append(f"- **动态摘要**：{item['summary']}")
        lines.append(f"- **信息来源**：[🔗 查看原文]({item['link']})")
        lines.append("")

    lines.extend([
        "#### 🎁 【今日福利与优惠活动】",
        "> 📢 今日暂无新增官方重大免费福利变动。日常推荐关注 Google AI Studio 免费层级或 GitHub 学生开发者礼包。",
        "",
        "## 🔥 板块三：GitHub 热门开源项目（通俗大白话解读）",
        ""
    ])
    for i, repo in enumerate(github_projects[:5], 1):
        stars_info = f"🌟 今日 {repo['stars_today']} / 总 Star {repo['total_stars']}"
        lines.append(f"### {i}. [{repo['name']}]({repo['url']}) {stars_info} | {repo['language']}")
        lines.append(f"- **💡 这项目到底是干啥的？**：{repo['description']}")
        lines.append(f"- **🎯 解决痛点与亮点**：开箱即用，帮助开发者与普通用户提升日常效率、降低工具成本。")
        lines.append("")

    return "\n".join(lines)


# ==================== 多渠道推送模块 (0元免费/多通道共存) ====================
def markdown_to_html(md_text: str, title: str) -> str:
    """将 Markdown 早报转换为高颜值移动端自适应 HTML 邮件卡片模板"""
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

    for line in md_text.splitlines():
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
    <div style="font-size:13px;opacity:0.85;margin-top:6px;">由 Google Gemini 深度提炼 · 自动化每日送达</div>
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

    print(f"\n📧 [渠道 A] 正在通过邮件推送早报 (服务器: {host}:{port}, 发件: {sender} -> 收件: {receiver}) ...")
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = formataddr((str(Header("每日 AI & 全球早报", "utf-8")), sender))
        msg["To"] = receiver

        # 兼容纯文本与精美 HTML 双格式
        html_body = markdown_to_html(content, title)
        msg.attach(MIMEText(content, "plain", "utf-8"))
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


def send_via_webhook(title: str, content: str) -> bool:
    """
    【渠道 B：0元免认证】通过企业微信 / 飞书 / 钉钉群机器人 Webhook 推送
    """
    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    if not webhook_url:
        return False

    print(f"\n🤖 [渠道 B] 正在通过自定义 Webhook 机器人推送早报 ...")
    headers = {"Content-Type": "application/json"}

    # 1. 企业微信机器人 (qyapi.weixin.qq.com)
    if "qyapi.weixin.qq.com" in webhook_url:
        safe_content = content if len(content.encode("utf-8")) < 4000 else content[:3500] + "\n\n> ⚠️ *(早报内容较长已自动截断，完整版请查看邮件或本地 preview_digest.md)*"
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": safe_content
            }
        }
    # 2. 飞书自定义机器人 (open.feishu.cn)
    elif "open.feishu.cn" in webhook_url:
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": title
                    },
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content
                    }
                ]
            }
        }
    # 3. 钉钉机器人 (dingtalk.com)
    elif "dingtalk.com" in webhook_url:
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": content
            }
        }
    # 4. 通用 Webhook 格式
    else:
        payload = {
            "title": title,
            "content": content
        }

    try:
        resp = requests.post(webhook_url, json=payload, headers=headers, timeout=20)
        if resp.status_code == 200:
            res_data = resp.json() if resp.text else {}
            errcode = res_data.get("errcode", res_data.get("code", res_data.get("StatusCode", 0)))
            if errcode == 0:
                print("  ✅ Webhook 机器人推送成功！")
                return True
            else:
                print(f"  ❌ Webhook 返回业务错误 (code={errcode}): {res_data}")
                return False
        else:
            print(f"  ❌ Webhook HTTP 请求异常 ({resp.status_code}): {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  ❌ Webhook 推送异常: {e}")
        return False


def send_via_pushplus(title: str, content: str) -> bool:
    """【渠道 C】通过 PushPlus (推送加) 发送到微信服务号"""
    token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    if not token:
        return False

    push_url = "http://www.pushplus.plus/send"
    payload = {
        "token": token,
        "title": title,
        "content": content,
        "template": "markdown"
    }

    print(f"\n📨 [渠道 C] 正在向 PushPlus 发起推送 (微信模板: markdown, 标题: {title}) ...")
    try:
        resp = requests.post(push_url, json=payload, headers={"User-Agent": USER_AGENT}, timeout=20)
        if resp.status_code == 200:
            res_data = resp.json()
            if res_data.get("code") == 200:
                print("  ✅ PushPlus 微信推送成功！响应:", res_data.get("msg", "OK"))
                return True
            else:
                print(f"  ❌ PushPlus 推送返回业务错误 (code={res_data.get('code')}): {res_data.get('msg')}")
                return False
        else:
            print(f"  ❌ PushPlus HTTP 请求异常 (status_code={resp.status_code}): {resp.text}")
            return False
    except Exception as e:
        print(f"  ❌ PushPlus 推送失败 (网络错误): {e}")
        return False


def dispatch_notifications(content: str) -> bool:
    """统一分发早报到所有已配置的推送渠道，并保存本地 preview_digest.md 预览与归档"""
    now_bj = datetime.now(BEIJING_TZ)
    title = f"每日 AI & 全球资讯早报 | {now_bj.strftime('%Y-%m-%d')}"

    # 1. 无论是否配置外部推送，均先将最新早报保存为本地 Markdown 文件供预览与归档
    preview_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview_digest.md")
    try:
        with open(preview_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"\n📄 最新早报已同步保存至本地预览文件: {preview_path}")
    except Exception as e:
        print(f"  [警告] 保存本地 preview_digest.md 失败: {e}")

    # 2. 检查已配置渠道并并发/依序推送
    email_enabled = bool(os.getenv("EMAIL_SENDER") and os.getenv("EMAIL_PASSWORD"))
    webhook_enabled = bool(os.getenv("WEBHOOK_URL"))
    pushplus_enabled = bool(os.getenv("PUSHPLUS_TOKEN"))

    if not (email_enabled or webhook_enabled or pushplus_enabled):
        print("\n⚠️ [提示] 未检测到任何推送配置（EMAIL_SENDER / WEBHOOK_URL / PUSHPLUS_TOKEN）。")
        print("   -> 已跳过网络推送，您可直接打开 preview_digest.md 查看生成的早报。")
        print("   -> 【0元免费方案推荐】：配置 EMAIL_SENDER 和 EMAIL_PASSWORD，微信开启「QQ邮箱提醒」插件即可秒级接收微信弹窗提醒！")
        return True

    success_count = 0
    # 渠道 A：邮件直推 + 微信「QQ邮箱提醒」（强烈推荐，0元免认证）
    if email_enabled:
        if send_via_email(title, content):
            success_count += 1

    # 渠道 B：企业微信 / 飞书群机器人（0元免认证）
    if webhook_enabled:
        if send_via_webhook(title, content):
            success_count += 1

    # 渠道 C：PushPlus 推送加（若用户已实名认证）
    if pushplus_enabled:
        if send_via_pushplus(title, content):
            success_count += 1

    print(f"\n📢 多渠道分发完成：已配置渠道中成功送达 {success_count} 个")
    return success_count > 0


# ==================== 主流程执行入口 ====================
def main():
    print("=" * 60)
    print("🚀 每日 AI & 全球资讯早报生成系统 启动")
    now_bj = datetime.now(BEIJING_TZ)
    print(f"⏰ 当前北京时间: {now_bj.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 1. 数据采集
    global_news = collect_global_news()
    ai_news = collect_ai_news()
    github_trending = collect_github_trending()

    total_scraped = len(global_news) + len(ai_news) + len(github_trending)
    print(f"\n📊 数据采集完成，总共获得 {total_scraped} 条全网最新热点数据。")

    # 2. 大模型智能提炼 (支持 DeepSeek / Gemini / 本地应急兜底)
    print("\n🧠 启动大模型智能提炼与分析引擎...")
    digest_content = generate_digest(global_news, ai_news, github_trending)

    # 3. 多渠道推送分发与本地落地
    dispatch_notifications(digest_content)

    print("\n🎉 今日早报全流程顺利执行完毕！")


if __name__ == "__main__":
    main()

