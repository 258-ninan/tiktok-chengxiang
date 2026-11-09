# TikTok 沉香原料短视频评论采集

Python 3.11+ 与 SQLite，使用标准库。工作目录为本项目根目录。已用 10 条合成视频和阿语评论做离线验收。真实评论覆盖、排序、地区线索及补采收益待授权试采。

项目用于验证沉香原料出口到海外的需求与试单条件，以中东为优先市场，同时收集其他市场的独立采购证据。Chrome 发现使用阿语词和一组英文原料词。视频发布时间从 2026 年 1 月 1 日零点（UTC+4）起，随运行时间滚动到当前。配送地和商家所在地分别核对，搜索语言只用于发现。

## 本地 Chrome 采集

复用日常 Chrome 的入口见 [本地采集扩展安装说明](chrome_extension/README.md)。用户已加载本项目扩展，运行 `chrome-run.ps1` 后，本机接收端会分发搜索和评论任务，并沿用当前数据库。这条路径已取得真实评论和回复，定时任务已恢复。默认每批最多检索 12 个关键词、处理 40 个视频任务，最长 30 分钟。

指定一次连续 4 小时的运行，可执行 `.\chrome-run.ps1 --duration-minutes 240`。该运行每 30 分钟开启下一批，继续轮换关键词和处理可重试、到期任务，直到本次时间窗口结束。默认每日定时任务仍按 30 分钟运行。运行状态含计划结束时间和本轮新增数，结束摘要保存在 `data/runs/`。

手动启动并写日志，在项目目录运行：

```powershell
Set-Location 'E:\codex\Project\chengxian'
.\chrome-run.ps1 --duration-minutes 240 2>&1 | Tee-Object -FilePath '.\data\logs\chrome-manual.log' -Append
```

在另一个 PowerShell 窗口查看状态或持续跟踪日志：

```powershell
Get-Content 'E:\codex\Project\chengxian\data\chrome-bridge-status.json' -Raw
Get-Content 'E:\codex\Project\chengxian\data\logs\chrome-manual.log' -Encoding UTF8 -Tail 25 -Wait
```

在另一个窗口执行 `& 'E:\codex\Project\chengxian\chrome-run.ps1' --stop` 可停止本轮。采集器结束在途任务并重新导出数据，状态显示 `stopped_by_user`。在日志窗口按 Ctrl+C 仅退出日志查看。

Chrome 采集结果先提交到 SQLite，再向扩展返回回执。接收端每 30 秒检查是否有待导出的结果，在任务间隙刷新报告，并在退出时再次导出。状态中的 `exported_at` 记录上次成功导出的时间，`export_error` 记录待处理的导出错误，例如报表 ZIP 被其他程序占用。

更新后在 `chrome://extensions/` 重新加载扩展，确认版本 0.3.4。扩展在采集标签页关联 TikTok 导航与研究接口响应，将状态码和 `Retry-After` 回传本机。页面切换单独记录为 `navigation_changed`。

单个 HTTP 403 记录到 `chrome_access_events.csv`，该链接标为 `access_denied` 并退出自动队列。10 分钟内三个不同任务链接返回 403，Chrome 进入 `access_backoff`，至少等 30 分钟再分配一个未被拒绝的任务。恢复后再遇 403 时退避延长至 1、2、4 小时，并遵守更长的 `Retry-After`。退避和历史拒绝记录保存在 SQLite。HTTP 401 或登录、验证提示仍以 `attention_required` 暂停，手动确认访问恢复后可执行 `.\chrome-run.ps1 --clear-access-pause`。

HTTP 429 暂停整个 Chrome 采集队列直到 `retry_until`。支持 `Retry-After` 的秒数和 HTTP 日期。缺少有效值时，按连续限流次数等待 1、2、4、8、15 分钟，成功收到正常页面结果后重置。429 和登录暂停保留尝试记录，不扣该视频失败重试额度。普通超时仍按现有重试间隔与上限处理。查看 `attempts.csv` 或 `chrome_access_events.csv` 中的 `page_load_timeout`、`extension_unresponsive`、`result_delivery_failed` 及最后收到的阶段。

发现阶段保留视频链接、标题、发布时间和计数等文本元数据。新增本地商品称呼及拼写变体，优先检索此前没有搜索记录的词。默认每段20次滚动，保存实际达到的深度，同一页面可继续至80次；页面丢失后从顶部重新滚动以恢复深度。达到滚动预算、连续无新结果或页面不再增长时记录原因并冷却6小时，不表示平台结果已经穷尽。搜索结果的时间倒序仍以实际网页为准，2026年边界是本地筛选条件。

评论页和发现页使用两个扩展自建标签页。每批加入最多6段账号主页访问，以数据库中已有原料候选视频的发布账号为来源，按今年发布时间筛选作品；该来源不等同于人工确认商家身份或所在地。主页中其他账号的推荐视频单独记录为排除，避免误算为该商家作品。

主页接口未返回结构化作品时，记录页面上的视频链接及标题。缺少发布时间的链接先作为候选，按每批最多 12 条详情核验任务读取实际日期和正文，确认年份后才进入评论队列。未核验到元数据时记为 `metadata_missing` 并冷却重试。搜索、详情和评论的动作间隔都由扩展后台计时。扩展 0.3.1 使用独立的 Chrome 窗口，任务标签页保持活动和静音，窗口创建时不请求焦点。系统需要保持唤醒，Chrome 需要持续运行。

Chrome 在页面实际返回每页评论和回复时立即去重入库。轮次只有在一级评论及可见回复的分页终点都验证后才标为完成。确认无评论需要有效的起始空页直接结束，且本次尝试没有观察到任何评论行。超时、页面未加载更多、重启或访问受限时，已经入库的评论保留，下一次从网页可访问的位置重新读取并跳过重复 ID。旧版采样留在库中，已经归档但未完成新版分页验证的视频可补采。每个视频的完成时间和已存条数见报表包内的 `chrome_comment_coverage.csv`。Apify 仍按其单轮样本上限处理。

视频详情默认显示推荐时，扩展优先用 `comment-icon` 或 `action_comment` 定位评论入口，也识别可点击的「评论」标签、无障碍标签和匹配的评论气泡图标。已打开的评论面板可以直接开始读取。状态分别记录入口来源、面板是否打开以及评论响应。暂停全天计划后，可以用 `.\chrome-run.ps1 --probe-video <VIDEO_ID>` 验证一个已入队的视频。单条验证遵守重试时间，默认最多运行 5 分钟，可用 `--duration-minutes` 调整等待窗口，结束后保存结果并退出。

如果当前页一直找不到评论入口，接收端会等待页面渲染。扩展确认找不到入口，或接收端收到缺失状态后持续 40 秒没有进展时，扩展会刷新同一视频一次。这次刷新计入原尝试。刷新仍失败时记录不完整或超时，随后按常规队列重试。刷新只适用于尚未点击入口且未提交评论页的任务。日志中的 `comment entry missing; reloading once` 表示触发了这次刷新。

0.3.4 将评论入口、面板、浏览器网络请求和正文解析分别上报。当前和最近一次任务分别见状态文件的 `pending_comment_diagnostics` 和 `last_comment_diagnostics`。网络统计覆盖扩展已获权限的 TikTok 网页请求，区分一级评论与回复，并记录状态码和网络错误。失败或分页未完成时，诊断也写入 `attempts.csv`。网络层已看到评论请求的任务由常规超时控制。

报表包内的 `discovery_pages.csv` 是新发现流程的完整分页、耗时、状态、独立新见视频及新增任务日志。`discovery_progress.csv` 保存下次续采深度和冷却时间。旧 `local_searches.csv` 仍用于兼容，新版新增量以 `discovery_pages.csv` 为准。

`discovery_candidates.csv` 记录全部解析到的候选及决定，`excluded_candidates.csv` 保留未入主样本的候选和排除/待核对原因，包括不在今年范围、发布时间未知、评论为零、标题无法确认商品。规则无法确认商品的候选不会直接进入原料分析。`candidate_review_sample.csv` 按原因分层选取最多50个独立视频，供核对标题与页面；人工核对结果不可仅由程序标签替代。

审核时将报表包里的 `candidate_review_sample.csv` 提取成独立文件，例如 `reviews.local.csv`。填写 `review_decision` 为 `raw` 或 `exclude`，原料形态 `review_material` 为 `chips`、`crumbs`、`chunks`、`mixed_raw` 之一，`review_evidence` 填核对依据。执行 `.\.venv\Scripts\python.exe discovery.py review --file reviews.local.csv` 可将符合年份及元数据条件的误排原料加入现有队列，保留视频和评论去重规则。`candidate_review_summary.csv` 列出各排除原因中已审核和确认原料数量；未经审核时不会输出准确率。

新采集任务按最多3个首采视频穿插一次发现任务，已经到期的72小时补采仍优先。只有采集成功才推进轮次。需要加载Python修复或中断恢复时，可保存状态文件副本，再以 `chrome-run.ps1 --resume-state data/runs/保存的状态.json` 恢复原计划结束时间和本轮计数基线，避免重新计满4小时。

本地入口为 `local-run.ps1`，使用项目隔离环境中的 Playwright 和本机 Chrome。运行 `local-login.ps1` 在采集专用 Chrome 会话登录一次，会话保存在 `local-browser/`。日常 Chrome 窗口的登录状态与此目录分别管理。已打开的日常 Chrome 需通过浏览器连接工具操作。

```powershell
.\local-login.ps1
.\local-run.ps1
```

数据库仍为 `data/comments.sqlite`。采集状态查看 `data/local-status.json`，导出在 `out/auto/reports.zip`，包括 `market_demand.csv`、`profit_scenarios.csv` 和 `market_analysis.md`。利润输入在 `profit_inputs.csv`，单个情景内金额使用同一种币种，损耗率按采购重量到可售重量折算。费用需要显式填写，已知为零时填写 0。

本地任务优先处理到期评论，随后检索关键词和已知账号，再采集新任务。检索日志 `local_searches.csv` 保留近期与历史结果、实际最早发布时间和新增量。关键词页的实际排序与覆盖以可见结果为准。

定时任务安装脚本为 `install-local-task.ps1`，任务名为 `Chengxian-TikTok-Local`。每次连续运行最多 24 小时，内部每 30 分钟轮换批次配额。任务计划每 15 分钟触发一次，已有实例运行时跳过，新实例沿用 SQLite 队列和发现进度。电脑需接通电源并保持唤醒，Windows 用户保持登录，Chrome 和扩展保持运行。遇到持久化访问暂停时，后续定时启动会跳过。日志按日追加到 `data/logs/chrome-scheduled-YYYYMMDD.log`。如需停止全天采集，先在 Windows 任务计划程序禁用该任务，再用 `chrome-run.ps1 --stop` 结束当前实例。

## 自动发现与采集

使用 Apify 免费个人账户。注册后在 Billing 确认计划为 Free，再到 Settings → API & Integrations 复制个人 API Token。免费额度用完后，Apify 会暂停运行至下个账期。程序调用 Actor 前也会核验账号计划。普通 TikTok 账号用于网页抽查，自动流程只用 Apify Token。实际使用前确认数据源的授权范围。 [Apify Token 设置](https://docs.apify.com/integrations/api) · [免费计划说明](https://docs.apify.com/account/subscriptions)

若本机已配置 `.apify-token.dpapi`，用以下命令验证账户、执行自动采集或处理补采。加密文件绑定当前 Windows 用户和电脑。启动器仅在运行期间给 Python 设置环境变量。验证账户只调用 Apify 的账户查询接口。

```powershell
.\run-auto.ps1 --check-token
.\run-auto.ps1 --authorized
.\run-auto.ps1 --authorized --skip-discovery
```

在本项目目录打开 PowerShell，输入以下命令。Token 只存在于当前窗口的环境变量中，关闭窗口后需重新输入。下面的写法也适用于 Windows PowerShell 5.1。

```powershell
$apifySecret = Read-Host 'Apify API Token' -AsSecureString
$env:APIFY_TOKEN = ([pscredential]::new('apify', $apifySecret)).GetNetworkCredential().Password
Remove-Variable apifySecret
python collect.py auto --authorized
```

`auto` 在 5 组、共 42 个阿语关键词中轮换，覆盖原料形态、批发供货、沙特与阿联酋地名、燃烧和产地。每次默认搜索 2 组、每词请求 12 条结果，再处理最多 25 个到期评论任务，已到期的补采优先。最多同时运行 3 个 Actor。结果写入 `out/apify/reports.zip`，包含数据库快照生成的中文 `market_analysis.md`。筛选发布日期从 **2026-01-01 00:00（阿联酋 UTC+4）** 到执行时、有评论计数且标题有沉香木料信号的视频。日期可用 `--since` 修改。搜索文案和评论计数只用于当次筛选。视频、封面、图片和头像的下载开关均关闭。SQLite 的视频记录保留链接、去重 ID 和调度及分析所需的字段。商家所在地仍依赖独立证据。标题筛选是启发式，搜索量受每词上限、排序和免费额度影响。 [Clockworks 视频搜索参数](https://apify.com/clockworks/tiktok-scraper/input-schema) · [视频输出字段](https://apify.com/clockworks/tiktok-scraper/output-schema)

扩大单批范围可运行 `.\run-auto.ps1 --authorized --search-batches 4 --limit 25`。重复运行会从下一组关键词继续搜索，沿用同一数据库去重。搜索组数、每词返回上限、评论任务数和并发上限分别用 `--search-batches`、`--per-keyword`、`--limit` 和 `--workers` 调整。任务数包括失败和补采尝试。

用同一个数据库反复运行即可保留去重状态。补采到期或只想清空已有队列时，用以下命令省掉一次搜索请求。

```powershell
python collect.py status
python collect.py auto --authorized --skip-discovery
```

搜索和评论 Actor 每次运行默认设 USD 0.50 的额度上限，这是实测 API 接受的最低上限。实际用量从 Apify 免费额度扣除。搜索采用默认相关度排序，省去排序和日期过滤的付费附加项，实际发布时间在本地筛。全年范围是筛选边界，最终结果是关键词样本。每次命令执行一批，后续补采可在到期后重跑。 [搜索 Actor 计价](https://apify.com/clockworks/tiktok-scraper/pricing)

## 手动选样与导入（可选）

用现有账号手动搜视频并确认页面确实有评论。沙特、阿联酋各选一批，合计 10 条，尽量覆盖不同商家账号、木片、碎料与块料。优先看原料批发商、沉香专卖店和燃烧测试账号，确认商品用于直接熏烧。泛词 `عود`、`بخور` 可用于找账号。人工区分香水、沉香油和调香成品。

| 搜索词 | 含义 |
| --- | --- |
| خشب العود | 沉香木 |
| عود طبيعي | 天然沉香 |
| رقائق العود | 沉香木片 |
| دقة العود | 沉香碎料 |
| تجربة حرق العود | 燃烧测试 |
| عود بالجملة | 沉香批发 |
| مورد عود | 沉香供应商 |
| كيلو عود | 公斤装或公斤报价 |

将种子存为 UTF-8 的 `seeds.local.csv`，格式见 [样例 CSV](examples/seeds.demo.csv)。最少只需 `url` 列，或用 [纯文本](examples/seeds.demo.txt) 每行一个 URL。

| 字段 | 填法 |
| --- | --- |
| `url` | TikTok 的 `https://www.tiktok.com/@账号/video/视频ID`。会移除追踪参数，并保留原链接作为来源。短链接可先在浏览器打开后复制最终地址，也可显式加 `--resolve-short` 解析 TikTok 重定向。 |
| `keywords` | 所有命中词用 `\|` 分隔，也可分多行录入。 |
| `published_at` | 网页核实的发布时间。填带时区的 ISO 8601，缺失可留空。 |
| `discovered_at` | 实际首次发现时间。空值采用导入时间。数据库固定第一次登记的发现时间和补采到期时间。 |
| `material` | 主样本用 `chips`、`crumbs`、`chunks` 或 `mixed_raw`。其余用 `perfume`、`oil`、`blend`、`other` 或 `unknown`，在汇总单列为 `other`。 |
| `merchant_country` | 商家所在地，填 `SA`、`AE`、`other` 或 `unknown`。 |
| `location_evidence` | 已知商家所在地必须附简介、视频或商家页面上的明确证据，建议写链接和原话。 |

默认允许旧视频。若某一批只要指定日期后发布的视频，导入加 `--published-since 2026-11-01T00:00:00+08:00`。这会排除未知发布时间的视频，补采仍按发现时间计算。

## 运行真实试采

TikTok 的其他地区条款限制使用自动脚本收集信息，并将普通服务定位为私人非商业使用。实际接入前须核对账号和采集方适用的条款，确认授权覆盖自动采集和商业市场研究。Apify 条款也要求对处理的数据有访问授权。若已经有可用于该目的的合规导出，可直接用下文的本地 JSON 接口。 [TikTok 其他地区条款](https://www.tiktok.com/legal/page/row/terms-of-service/en) · [Apify 使用条款](https://docs.apify.com/legal/general-terms-and-conditions)

若已手动准备种子，用开头的方式在本机 PowerShell 会话设置 Apify Token 后运行：

```powershell
python collect.py import seeds.local.csv
python collect.py status
python collect.py run --source apify --authorized --limit 1
python collect.py export out/first-check
# 核对第一条后，完成其余待采视频
python collect.py run --source apify --authorized --limit 10
python collect.py export out/round1
```

默认数据库是 `data/comments.sqlite`。任一命令可在子命令前加 `--db 路径` 和 `--config 路径`。每次 `run` 只处理当下已到期的任务，同一视频在一次命令内最多尝试一次。按 `status` 所列 UTC 到期时间再运行同一命令，即可补采。默认从发现时起 72 小时，失败间隔 15 分钟，每轮最多自动尝试 3 次。到期但首轮仍未成功时，先完成首轮，然后在下一次运行执行已到期的第二轮。更改 `window_hours` 只影响新登记视频。

Clockworks 默认单次请求一条视频。一级最多 30 条，每条最多 1 条回复，总量最多 35 条，每次请求的预算参数为 USD 0.50。10 条视频两轮合计最多请求 700 条评论和回复，未计重试。定价页列出的免费层评论事件费率为每千条 USD 1.25，因此按 700 个评论事件估算约 USD 0.88，实际以账户配额、计费事件和账单为准。预算参数按 Actor 运行生效。 [Clockworks 参数](https://apify.com/clockworks/tiktok-comments-scraper/input-schema) · [Clockworks 计价](https://apify.com/clockworks/tiktok-comments-scraper/pricing) · [Apify 运行 API](https://docs.apify.com/api/v2/actors-runs-post)

### 网页与 CSV 对照

1. 每轮采前，用 TikTok 页面记录核对时间、可见评论情况、几条刚发评论的原文和显示时间，并展开部分回复。若界面提供排序切换，记下使用的排序。首轮先核对一条视频，再运行剩余 9 条。
2. 用 `attempts.csv` 的 run 和 dataset ID 查 Apify 控制台，抽查数据集中的 `cid`、`text`、`createTimeISO` 或 `createTime`、`repliesToId` 与 `comments.csv`。对照阿语正文、发布时间、父评论 ID、一级与回复数量。若父评论未被取到，记录 `parent_status=not_captured`。 [Clockworks 输出字段](https://apify.com/clockworks/tiktok-comments-scraper/output-schema)
3. 同一批视频补采前再记下网页可见的新发样本，对照实际抓回了哪些及漏了哪些。公开输入没有最新评论排序选项，列表可能截断或漏返回。有效非空批次表示取得一批可用样本，范围以实际返回为准。 [Clockworks 评论参数](https://apify.com/clockworks/tiktok-comments-scraper/input-schema)
4. 仅在有独立证据明确确认无评论时，用 `python collect.py confirm-empty 视频ID --evidence '核对时间、页面链接、观察说明'`。接口空数组记为 `empty_unverified`。结构异常且带部分可用记录记为 `partial`，只留在尝试日志，重试成功后的批次才计入主表。确认无评论计为一次有效观察。

SQLite 原子提交评论、尝试状态与任务推进。进程异常终止会留下正在执行的尝试，租约到期后标记为 `interrupted` 并按重试间隔恢复。租约长度为 Actor 超时加两分钟。远端已经启动的任务可能独立完成，恢复后重新发起请求可能产生额外计费，数据库仍按评论 ID 去重。运行中确认的 ID 随完成日志导出。耗尽的轮次留在 `exhausted`，查明原因后可临时用配置提高该轮上限并再运行。

## 线索与二轮收益

`export` 在指定目录发布一份 `reports.zip`。每个报表包来自同一次数据库快照，生成完成后才替换上一包。使用资源管理器打开 ZIP，或把整包解压到新目录供脚本读取。升级前的同目录平铺报表是历史副本。

包内 CSV 使用 UTF-8。以公式前缀或前导控制字符开头的文本带有可见的 `[text] ` 前缀。查询 SQLite 可取得原始值。使用 Excel 导入时把视频 ID、评论 ID 和原文字段设为文本，以保留长 ID 和原文。

- `market_leads.csv` 每条评论每个采购主题一行，带阿语正文、视频链接、原料形态和市场证据。价格原文也可在 `comments.csv` 中按父评论 ID 回溯商家回复。
- `market_summary.csv` 按主样本/其他、市场证据、原料形态和采购问题给出评论次数、独立视频和账号数。市场证据分开保留商家所在地和评论明确配送地，语言另列。阿语字符命中仅标 `ar_candidate`。
- `market_demand.csv` 仅汇总原料主样本，应用评论标注和发现审计中的人工排除。
- `round2_new.csv` 按主样本/其他和时间类别列出二轮新增评论、其中有采购问题的独立评论数及涉及的视频和账号数。一条评论即使有多个问题也只计一次。
- `market_analysis.md` 是每次导出的中文验证报告。它把核对过的批发、指定数量、索样、配送和报价证据与关键词候选分开，保留回复链、各市场证据和利润输入缺口。真实本地数据库导出自动应用项目根目录的 `annotations.local.csv`。运行 `python collect.py export out/manual` 可只基于现有数据库生成独立报表包。
- `videos.csv`、`discoveries.csv` 和 `attempts.csv` 分别保留视频状态、来源链接与命中词、各轮采集与失败明细。总量看 `stats.json`。

二轮第一次成功入库的评论，按发布时间分为 `window_new_observed`（发现至截止，含边界）、`older_not_seen`（发现前已发）、`after_window_observed`（截止后发）和 `time_unknown`。只有窗口内且二轮取到的独立采购线索可直接用于评估三天跟踪价值。用真实试采的 `round2_new.csv` 结合账号分布及补采成本决定是否长期保留。

阿语规则先标候选，随后人工核对方言、角色、配送目的地和原文。仅问公斤价标为 `bulk`，明确批发、起订量或供货标为 `wholesale`。商家回复可另记报价。用 `--annotations annotations.local.csv` 套用人工结果。主题用 `|` 分隔，填 `-` 清除候选主题，空白继承原值。填写明确配送地时附原文证据。明确报价用 `quote_amount`、`quote_currency`、`quote_unit`、`quote_by` 保留原样。币种无法判定可填 `unknown`。单位 `أوقية` 保留原文，待克重有依据后再换算。 [标注样例](examples/annotations.demo.csv)

## 离线复现

```powershell
python -m unittest -v
$demoDb = 'data/my-demo.sqlite' # 每次复现选一个新文件
$dataset = 'examples/comments.demo.json'
python collect.py --db $demoDb --now 2026-11-01T09:00:00Z import examples/seeds.demo.csv
python collect.py --db $demoDb --now 2026-11-01T09:00:00Z run --source json --file $dataset
python collect.py --db $demoDb --now 2026-11-01T09:15:00Z run --source json --file $dataset
python collect.py --db $demoDb --now 2026-11-04T08:59:59Z run --source json --file $dataset
python collect.py --db $demoDb --now 2026-11-04T09:01:00Z run --source json --file $dataset
python collect.py --db $demoDb --now 2026-11-04T09:01:00Z export out/demo --annotations examples/annotations.demo.csv
```

JSON 以字符串 `video_id` 为键，下一级为轮次 `"1"`、`"2"`。每轮是按尝试顺序排列的结果列表，单个结果如 `{"items": [...]}`，其中 `items` 为 Clockworks 形状的评论对象。样例和真实数据库分开使用。替换数据源时由 `sources.py` 中的适配器返回统一的 `Result`，沿用调度及 SQLite。

合成样例已归档 10 条视频，共 17 条独立评论（1 条回复）。二轮新增 6 条，5 条带采购问题，其中 2 条为 72 小时观察期内的新发且本轮取到，涉及 2 条视频与 2 个账号。首轮记录超时、部分返回、未验证空返回和错误各 1 次。真实试采和真实补采尚无数据。 [样例线索](examples/demo-output/market_leads.csv) · [样例二轮汇总](examples/demo-output/round2_new.csv) · [样例统计](examples/demo-output/stats.json)

TikTokApi 主分支有 `video.comments()`，搜索只支持用户，可作为获授权后的本地评论备选。 [TikTokApi 评论源码](https://github.com/davidteather/TikTok-Api/blob/main/TikTokApi/api/video.py) · [TikTokApi 搜索源码](https://github.com/davidteather/TikTok-Api/blob/main/TikTokApi/api/search.py)
