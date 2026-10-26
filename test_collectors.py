#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据抓取功能全面测试脚本 (Test Data Collectors)
覆盖：
1. 全球重大时事资讯采集
2. AI 御三家 (OpenAI/Google/Claude) & 算力具身智能双巨头 (英伟达 NVIDIA & 特斯拉 Tesla) 专项采集
3. GitHub Trending 今日飙升榜单
4. GitHub 每日经典高分宝藏项目池 (30+ 维度确定性轮换与本地兜底)
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from main import collect_global_news, collect_ai_news, collect_github_trending, fetch_github_classic_gems

def run_tests():
    print("=================== 1. 测试【全球重大时事】资讯抓取 ===================")
    global_items = collect_global_news()
    print(f"全球时事成功抓取数量: {len(global_items)}")
    if global_items:
        print("样例 1:")
        print(f"  标题: {global_items[0]['title']}")
        print(f"  来源: {global_items[0]['source']}")
        print(f"  链接: {global_items[0]['link']}")
        print(f"  摘要: {global_items[0]['summary'][:100]}...")

    print("\n=================== 2. 测试【AI 御三家 & 英伟达/特斯拉】资讯抓取 ===================")
    ai_items = collect_ai_news()
    print(f"AI 生态资讯成功抓取数量: {len(ai_items)}")
    if ai_items:
        print("样例 1:")
        print(f"  标题: {ai_items[0]['title']}")
        print(f"  来源: {ai_items[0]['source']}")
        print(f"  链接: {ai_items[0]['link']}")

    # 统计英伟达/特斯拉专项命中情况
    nv_tsla_matches = [it for it in ai_items if any(k in it['title'].lower() for k in ['nvidia', '英伟达', 'huang', '黄仁勋', 'blackwell', 'cuda', 'tesla', '特斯拉', 'fsd', 'optimus', 'musk', '马斯克'])]
    print(f"  -> 其中包含英伟达/特斯拉专项动态共 {len(nv_tsla_matches)} 条")

    print("\n=================== 3. 测试【GitHub Trending 今日飙升】抓取 ===================")
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

    print("\n=================== 4. 测试【GitHub 每日经典宝藏项目池】轮换抓取 ===================")
    exclude_set = {t['name'].lower() for t in trending_items}
    classic_gems = fetch_github_classic_gems(exclude_names=exclude_set)
    print(f"经典宝藏项目候选数量: {len(classic_gems)}")
    if classic_gems:
        print("样例 1 (精选宝藏):")
        print(f"  项目: {classic_gems[0]['name']}")
        print(f"  链接: {classic_gems[0]['url']}")
        print(f"  总 Stars: {classic_gems[0]['total_stars']}")
        print(f"  语言: {classic_gems[0]['language']}")
        print(f"  探索主题: {classic_gems[0].get('topic_category')}")
        print(f"  描述: {classic_gems[0]['description']}")

    print("\n=================== 自动化测试总结 ===================")
    assert len(global_items) > 0, "全球时事抓取数据为空！"
    assert len(ai_items) > 0, "AI 生态资讯抓取数据为空！"
    assert len(trending_items) > 0, "GitHub Trending 抓取数据为空！"
    assert len(classic_gems) >= 6, f"经典宝藏项目数量不足 6 个 (实际: {len(classic_gems)})！"
    print("🎉 全部 4 大核心数据采集与宝藏池轮换模块均 100% 验证通过！")

if __name__ == "__main__":
    run_tests()
