# 日本 AI / Physical AI / 具身智能 公司与招聘信息源清单

> 目标：为 curated-feeds 插件配置（`.kirakira/plugin-data/curated-feeds/config.local.toml`）提供可落地的订阅源。
> 该插件支持 `kind = "rss" / "webpage" / "wordpress" / "yahoo_market"`。
> 生成日期：2026-08-02

## 一、核心头部公司（最值得盯）

### 1. Sakana AI（生成AI独角兽，东京）
- 招聘页：https://sakana.ai/careers/  （Applied Team: https://sakana.ai/applied-careers/）
- LinkedIn Jobs：https://jp.linkedin.com/company/sakana-ai/jobs
- 博客(RSS候选)：https://sakana.ai/blog
- X：@SakanaAILabs
- 现状：2025-2026 持续扩招，新增 Recruiting Coordinator（2026-07-25），Applied Team（金融/防务/制造）在招。

### 2. Preferred Networks（PFN，AI/半导体/机器人）
- 招聘页：https://www.preferred.jp/ja/careers
- 采用Talentio平台：https://open.talentio.com/r/1/c/preferred/homes/4551
- 注：PFN 2027年入社新卒 募集已开始。
- 关注维度：AI半导体、生成AI基盘模型、Preferred Robotics（自律移动机器人）。

### 3. GMO AI&ロボティクス商事（GMO AIR，ヒューマノイド商社/实装）
- 官网：https://ai-robotics.gmo/
- 招聘：该站有「採用情報」栏目，engineer/researcher 募集。
- 关注：Unitree 日本正規代理、ヒューマノイド研究开发据点。

## 二、Physical AI / 人形机器人 创业公司（2025-2026 融资活跃）

| 公司 | 领域 | 近期动态 | 官网/来源 |
|---|---|---|---|
| 株式会社アトム | ヒューマノイドAIロボット | 2026-05 种子30亿日元 | robotstart, xtech.nikkei |
| Zen Intelligence(旧SoftRoid) | 建设现场无人化/Physical AI | 2025-09 A轮15亿日元 | prtimes |
| Telexistence | 零售/物流机器人+PI提携 | 累计约275亿日元 | xtech.nikkei |
| Muso Action | 泛用ロボットワーカー/VLA | 2025-12 种子1亿日元(hiring engine) | ascii.jp |
| Forcesteed Robotics | 人工意识/Physical-AI | - | startupclass.co.jp |
| ugo株式会社 | AIロボ/プラットフォーム | ヒューマノイドugo Pro | ugo.plus / corp.ugo.plus |
| ROBOTS(NTTドコモspinoff) | ロボット统合制御/Physical AI | 2026-07 创业 | docomo news_release |
| Transistor Robotics | AIヒューマノイド/ASH_OS | 细胞培养自动化 | transistorrobotics.com |

## 三、可直接订阅的媒体 RSS 源

1. **ロボスタ（robotstart.info）** — 日本最大级 ロボット×AI 资讯媒体
   - RSS: `https://robotstart.info/rss20/index.rdf`（官网 HTML 声明的真实地址）
2. **日経Robotics / xTECH** — 权威技术向
   - 站点：https://xtech.nikkei.com/atcl/xtt/nxt/
   - RSS：需确认是否公开
3. **PR TIMES 科技/ロボ 分类** — 企业融资新闻主要渠道
   - 可关注 startup 分类 RSS
4. **StartupClass** — AI/ロボティクス startup 数据库：https://startupclass.co.jp/online/companies/categories/3/

## 四、落地建议（config.local.toml feeds 块）

优先推荐加这些（rss / webpage 皆可）：
```toml
# 媒体（rss）
[[feeds]] id="robotstart" name="ロボスタ" kind="rss"
  url="https://robotstart.info/rss20/index.rdf" topic="日本/ロボ・AI" max_items=6

# 头部公司招聘页（webpage，内容变化时触发）
[[feeds]] id="sakana-careers" name="Sakana AI 採用" kind="webpage"
  url="https://sakana.ai/careers/" topic="日本/AI採用"
[[feeds]] id="pfn-careers" name="PFN 採用" kind="webpage"
  url="https://www.preferred.jp/ja/careers" topic="日本/AI採用"
