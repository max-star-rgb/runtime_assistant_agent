---
name: price-compare-links
description: Cross-platform price comparison for JD, Taobao, and Pinduoduo using web search. Searches each platform, compares results, and provides landing links. Use when the user wants to find the cheapest option across Chinese e-commerce platforms.
license: MIT
enabled: false
---

# Price Compare Links

Search multiple Chinese e-commerce platforms (JD, Taobao, Pinduoduo) for a given product keyword using `web_search`, then present a side-by-side comparison with landing links.

## Usage

1. Receive a product keyword from the user.
2. For each platform, run a web search:

```bash
# JD
web_search query="site:jd.com {keyword}"

# Taobao
web_search query="site:taobao.com {keyword}"

# Pinduoduo
web_search query="site:pinduoduo.com {keyword}"
```

3. Extract product names, prices, and URLs from each result set.
4. Format a comparison table:

| Platform | Product | Price | Link |
|----------|---------|-------|------|
| JD       | …       | ¥…    | …    |
| Taobao   | …       | ¥…    | …    |
| Pinduoduo| …       | ¥…    | …    |

5. Highlight the cheapest option and present the table to the user.

## Notes

- Prices from web search are approximate; actual prices may vary.
- This skill uses the generic `web_search` tool — no external API key needed.
- For more accurate results with real-time prices and coupons, prefer the `taobao` (maishou) skill instead.
