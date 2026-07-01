# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp", "argparse", "PyYAML"]
# ///
import os
import io
import sys
import csv
import yaml
import asyncio
import aiohttp

# Force UTF-8 stdout on Windows (avoid GBK encode errors for Unicode chars in product titles)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import argparse

INVITE_CODE = os.getenv("MAISHOU_INVITE_CODE") or "6110440"
HEADERS = {
    aiohttp.hdrs.ACCEPT: "application/json",
    aiohttp.hdrs.REFERER: "https://hnbc018.kuaizhan.com/",
    aiohttp.hdrs.USER_AGENT: "Mozilla/5.0 AppleWebKit/537 Chrome/143 Safari/537",
}
SESSION: aiohttp.ClientSession | None = None

async def search(keyword, source=0, func=None, top_links=3, **kwargs):
    resp = await SESSION.post(
        "https://appapi.maishou88.com/api/v1/homepage/searchList",
        headers={
            **HEADERS,
            aiohttp.hdrs.USER_AGENT: "MaiShouApp/3.7.7 (iPhone; iOS 26.3; Scale/3.00)",
            "openid": "564bdce0fa408fc9e1d5d42fd022ef0b",
            "version": "3.7.7.2",
        },
        data={
            "isCoupon": 0,
            "keyword": str(keyword),
            "openid": "564bdce0fa408fc9e1d5d42fd022ef0b",
            "order": "desc",
            "page": 1,
            "pddListId": "",
            "sort": "",
            "sourceType": str(source),
            "user_id": "",
            **kwargs,
        },
    )
    data = await resp.json(encoding="utf-8-sig") or {}
    rows = data.pop("data", [])
    if not rows:
        return data.get("message") or await resp.text()
    idx = 0
    rows = [
        {
            "idx": idx,
            "goodsId": v.get("goodsId"),
            "source": v.get("sourceType"),
            "title": v.get("title"),
            "shopName": v.get("shopName"),
            "originalPrice": v.get("originalPrice"),
            "actualPrice": v.get("actualPrice"),
            "couponPrice": v.get("couponPrice"),
            "monthSales": v.get("monthSales"),
            "picUrl": v.get("picUrl") or "",
        }
        for v in rows
        if (idx := idx + 1)
    ]

    # Fetch purchase links for top N items (by index order, not price)
    top_links = int(top_links)
    if top_links > 0:
        top_items = rows[:top_links]
        link_tasks = [
            _get_link(item["goodsId"], item["source"])
            for item in top_items
            if item.get("goodsId")
        ]
        links = await asyncio.gather(*link_tasks, return_exceptions=True)
        link_map = {}
        for item, link in zip(top_items, links):
            if isinstance(link, str) and link:
                link_map[item["goodsId"]] = link
        for row in rows:
            row["link"] = link_map.get(row["goodsId"], "")

    # Format output: pre-formatted for direct inclusion in <detail> block.
    # LLM should copy this content as-is into <detail>...</detail> without reformatting.
    display_rows = rows[:5]
    parts = []
    for item in display_rows:
        line = f"{item['idx']}. {item.get('title','')} | {item.get('shopName','')} | 实付{item.get('actualPrice','')}元 券{item.get('couponPrice','')}"
        if item.get("link"):
            line += f" <link>{item['link']}</link>"
        if item.get("picUrl"):
            line += f" <pic>{item['picUrl']}</pic>"
        parts.append(line)
    # Add metadata section for LLM internal use (not for display)
    parts.append("\n---internal---")
    for item in display_rows:
        parts.append(f"idx={item['idx']} goodsId={item.get('goodsId','')} source={item.get('source','')}")
    return "\n".join(parts)


async def _get_link(goods_id: str, source_type) -> str:
    """Fetch purchase link for a single item via getTargetUrl API."""
    try:
        params = {
            "goodsId": str(goods_id),
            "sourceType": str(source_type),
            "inviteCode": INVITE_CODE,
            "supplierCode": "",
            "activityId": "",
            "isShare": "1",
            "token": "",
        }
        resp = await SESSION.post(
            "https://msapi.maishou88.com/api/v1/share/getTargetUrl",
            json={**params, "isDirectDetail": 0},
        )
        data = await resp.json(encoding="utf-8-sig") or {}
        info = data.get("data") or {}
        return info.get("appUrl") or info.get("schemaUrl") or ""
    except Exception:
        return ""

async def detail(id, source, func=None, **kwargs):
    params = {
        "goodsId": str(id),
        "sourceType": str(source),
        "inviteCode": INVITE_CODE,
        "supplierCode": "",
        "activityId": "",
        "isShare": "1",
        "token": "",
    }
    resp = await SESSION.post(
        "https://appapi.maishou88.com/api/v3/goods/detail",
        json={
            **params,
            "keyword": "",
            "usageScene": 5,
        },
    )
    data = await resp.json(encoding="utf-8-sig") or {}
    detail = data.get("data") or {}

    resp = await SESSION.post(
        "https://msapi.maishou88.com/api/v1/share/getTargetUrl",
        json={
            **params,
            "isDirectDetail": 0,
        },
    )
    data = await resp.json(encoding="utf-8-sig") or {}
    info = data.get("data") or {}
    if not info:
        return [data.get("message"), await resp.text(), resp.request_info]
    info = {
        "商品标题": detail.pop("title", ""),
        "购买链接": info.get("appUrl") or info.get("schemaUrl"),
        "复制口令": info.get("kl"),
        "商品详情": detail,
    }
    return yaml.dump(info, allow_unicode=True, sort_keys=False)

async def main():
    global SESSION
    async with aiohttp.ClientSession(headers=HEADERS) as SESSION:
        parser = argparse.ArgumentParser()
        parsers = parser.add_subparsers()

        search_parser = parsers.add_parser("search")
        search_parser.add_argument("--keyword", help="关键词")
        search_parser.add_argument("--source", default="0", help="来源 0:全部 1:淘宝 2:京东 3:拼多多")
        search_parser.add_argument("--page", type=int, default=1, help="分页")
        search_parser.add_argument("--top-links", dest="top_links", type=int, default=5, help="获取前N个最便宜商品的购买链接")
        search_parser.set_defaults(func=search)

        detail_parser = parsers.add_parser("detail")
        detail_parser.add_argument("--id", help="商品ID")
        detail_parser.add_argument("--source", default="1", help="来源 1:淘宝 2:京东 3:拼多多")
        detail_parser.set_defaults(func=detail)

        args = parser.parse_args()
        if hasattr(args, "func"):
            print(await args.func(**vars(args)))
        else:
            parser.print_help()

if __name__ == "__main__":
    asyncio.run(main())
