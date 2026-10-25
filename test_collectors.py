#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据抓取功能独立测试脚本 (Test Data Collectors)
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from main import collect_global_news, collect_ai_news, collect_github_trending

def run_tests():
    print("=================== 1. 测试全球时事资讯抓取 ===================")
    global_items = collect_global_news()
    print(f"全球时事成功抓取数量: {len(global_items)}")
    if global_items:
        print("样例 1:")
        print(f"  标题: {global_items[0]['title']}")
        print(f"  来源: {global_items[0]['source']}")
        print(f"  链接: {global_items[0]['link']}")
        print(f"  摘要: {global_items[0]['summary'][:100]}...")

    print("\n=================== 2. 测试指定 AI 生态资讯抓取 ===================")
    ai_items = collect_ai_news()
    print(f"AI 生态资讯成功抓取数量: {len(ai_items)}")
    if ai_items:
        print("样例 1:")
        print(f"  标题: {ai_items[0]['title']}")
        print(f"  来源: {ai_items[0]['source']}")
        print(f"  链接: {ai_items[0]['link']}")

    print("\n=================== 3. 测试 GitHub Trending 抓取 ===================")
    trending_items = collect_github_trending()
    print(f"GitHub Trending 抓取数量: {len(trending_items)}")
    if trending_items:
        print("样例 1:")
        print(f"  项目: {trending_items[0]['name']}")
        print(f"  链接: {trending_items[0]['url']}")
        print(f"  描述: {trending_items[0]['description']}")
        print(f"  语言: {trending_items[0]['language']}")
        print(f"  今日新增: {trending_items[0]['stars_today']}")
        print(f"  总 Stars: {trending_items[0]['total_stars']}")

    print("\n=================== 测试总结 ===================")
    assert len(global_items) > 0, "全球时事抓取数据为空！"
    assert len(ai_items) > 0, "AI 生态资讯抓取数据为空！"
    assert len(trending_items) > 0, "GitHub Trending 抓取数据为空！"
    print("🎉 所有三项数据采集模块均 100% 正常运行通过！")

if __name__ == "__main__":
    run_tests()
