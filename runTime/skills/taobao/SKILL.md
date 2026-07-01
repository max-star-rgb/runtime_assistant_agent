---
name: taobao
description: 商品价格全网对比 + 加入购物车。搜索京东/拼多多/抖音/快手的最优价格和优惠券，支持一键加购。Compare prices across Chinese e-commerce platforms and add to cart.
metadata:
  {
    "openclaw":
      {
        "emoji": "🛍️",
        "requires": { "bins": ["uv"] },
        "install":
          [
            {"id": "uv-brew", "kind": "brew", "formula": "uv", "bins": ["uv"], "label": "Install uv (brew)"},
            {"id": "uv-pip", "kind": "pip", "formula": "uv", "bins": ["uv"], "label": "Install uv (pip)"},
            {"id": "pip-aiohttp", "kind": "pip", "formula": "aiohttp", "label": "Install aiohttp (pip)"},
            {"id": "pip-argparse", "kind": "pip", "formula": "argparse", "label": "Install argparse (pip)"},
            {"id": "pip-PyYAML", "kind": "pip", "formula": "PyYAML", "label": "Install PyYAML (pip)"},
          ],
      },
  }
---

# 买手技能
全网比价 + 加入购物车

**重要：所有商品搜索和比价操作必须通过本技能完成，禁止使用 browser 工具打开电商网站搜索。**

```yaml
# 参数解释
source:
  0: 全部
  2: 京东
  3: 拼多多
  4: 抖音
  5: 快手
```

## 搜索商品
```shell
python -m uv run scripts/main.py search --source=0 --keyword='{keyword}'
```

## 商品详情
```shell
python -m uv run scripts/main.py detail --source={source} --id={goodsId}
```

## 加入购物车（京东）
通过浏览器在京东搜索商品并加入购物车。Cookie 已预存。
```
1. browser action=set_cookies_file file_path="D:\cookies\taobao.json"
2. browser action=navigate url="https://search.jd.com/Search?keyword={keyword}&enc=utf-8"
3. browser action=evaluate script="提取商品列表"
4. browser action=navigate url="选中的商品URL"
5. browser action=click selector="#add-to-cart"
```

## 重要规则
- 搜索结果中的 goodsId 是加密 ID，不能直接拼链接。搜索命令已自动获取前5个最便宜商品的购买链接（link 字段）。
- 展示搜索结果时，商品详情列表（含链接和图片）已由系统自动发送给用户。**严禁输出 <detail> 块，严禁输出 <link> 或 <pic> 标签，严禁列出商品列表。** 你只需要用口语总结搜索结果（2-3句话），包含：最便宜的商品完整名称、具体配置（内存+存储）、具体价格、所在平台、店铺名，以及其他价位段的选项概述。不要问用户要链接（链接已经在详情列表里了）。
  示例：最便宜的是淘宝华为官方旗舰店的HUAWEI Mate 70 Pro 12GB+256GB版本，4099元带政府补贴。京东也有4199起，512G版本4599。你看要哪个配置？
- 用户选链接 → 如果搜索结果中该商品已有 link 字段，直接用 <link>URL</link> 发给用户。如果没有 link，必须用 exec 工具（不是 run_skill）执行以下命令获取链接：
  ```
  exec command="python -m uv run scripts/main.py detail --source={source} --id={goodsId}" cwd="skills/taobao"
  ```
  从输出中提取"购买链接"字段，用 <link>URL</link> 发给用户。
- **严禁用 run_skill 获取链接。严禁用 browser 工具打开电商网站获取链接。获取链接只能用 exec + detail 命令。**
- **用户要链接时严禁重新搜索。链接已经在之前的搜索结果中。**
- 用户选加购物车 → 使用 action='add_to_cart'，keyword 用用户选择的那个商品的完整标题（title 字段），不要用原始搜索词。
- **加购物车失败时**：告诉用户没搞定，同时把该商品的购买链接用 <link>URL</link> 发给用户，让用户自己点链接购买。如果没有现成链接，先用 detail 命令获取。示例回复："购物车没加成，给你链接你自己加吧<link>https://u.jd.com/xxx</link>"
- 展示格式：口语说最便宜的价格和平台，详细列表放 <detail> 标签内。
- 禁止用 web_search 或 browser 去电商网站搜索商品进行比价。
- skill 执行期间（从搜索到用户明确说不需要了），每次回复都 end_turn(expects_reply=true)。

## Steps
```yaml
- id: search
  group: search
  action: exec
  command: "python -m uv run scripts/main.py search --source={source} --keyword=\"{keyword}\" --top-links=5"
  params: [keyword, source]
  defaults: {source: "0"}
  timeout: 45

- id: set_cookies
  group: add_to_cart
  action: browser
  browser_action: set_cookies_file
  args: {file_path: "D:\\cookies\\taobao.json"}

- id: search_page
  group: add_to_cart
  action: browser
  browser_action: navigate
  args: {url: "https://search.jd.com/Search?keyword={keyword}&enc=utf-8"}

- id: wait_results
  group: add_to_cart
  action: browser
  browser_action: wait
  args: {selector: "[data-sku]", timeout_s: 15}

- id: extract_and_match
  group: add_to_cart
  action: browser
  browser_action: evaluate
  args:
    script_file: "scripts/extract_match.js"

- id: open_product
  group: add_to_cart
  action: browser
  browser_action: navigate
  args: {url: "${extract_and_match.result.url}"}

- id: add_cart
  group: add_to_cart
  action: browser
  browser_action: click
  args: {selector: "#add-to-cart"}

- id: captcha_retry
  on_error: add_cart
  group: add_to_cart
  action: browser
  browser_action: wait
  args: {timeout_s: 30}
  max_retries: 2
```
