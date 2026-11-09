# 使用现有 Chrome 登录的本地采集扩展

该扩展在已登录 TikTok 的日常 Chrome 中运行。仅申请 www.tiktok.com 页面和 127.0.0.1:8766 本机接收端的权限，另有任务唤醒和保存扩展自身任务状态的权限。

扩展会打开自己使用的 TikTok 标签页，按本机队列访问搜索页和视频页、滚动评论，读取页面已经返回的视频与评论研究字段。数据写入当前项目的 SQLite。登录资料由 Chrome 管理。

## 安装前

在项目目录执行以下命令，生成只保存在本机的扩展配对配置。

```powershell
.\chrome-run.ps1 --setup
```

## 需要用户在 Chrome 完成的安装

1. 在已经登录 TikTok 的 Chrome 中打开 `chrome://extensions/`。
2. 开启开发者模式，选择“加载已解压的扩展程序”。
3. 选择本项目的 `E:\codex\Project\chengxian\chrome_extension` 文件夹，核对 TikTok 和本机地址的访问权限后安装。
4. 在项目目录执行 `.\chrome-run.ps1`，再点击扩展图标即可开始。扩展也会定时检测本机接收端。

当本机采集服务结束时，扩展停止接收任务。当前版本已在现有 Chrome 登录会话取得真实评论与回复。默认单批最多处理 12 个关键词和 40 个视频任务，最长 30 分钟。页面操作和来源返回仍可能导致空结果或超时，各自留有状态记录。

## 数据与状态

- 数据库为 `data/comments.sqlite`。
- 连接和执行状态为 `data/chrome-bridge-status.json`。
- 搜索覆盖和错误记录见报表包内的 `local_searches.csv`。
- 市场需求、评论明细及报告按整批发布到 `out/auto/reports.zip`。

页面接口变化、登录要求或验证码会影响可采结果。页面需要验证时暂停本次采集。

更新后在 chrome://extensions/ 点击本扩展刷新，确认版本 0.3.4。搜索、详情和评论动作间隔由扩展后台计时。任务在独立的 Chrome 窗口切换活动标签页，窗口创建时不请求焦点。评论入口支持结构标记、可点击的评论标签、无障碍标签和评论气泡图标，并检查评论列表是否出现。每页评论和回复逐批写入本机，只有完整分页得到验证才完成轮次。webRequest 独立上报评论请求是否发出、HTTP 状态和网络失败。403 停止该链接的自动访问，连续拒绝时自动退避，429 按限流规则等待，登录验证保留人工恢复。日志、覆盖和退避截止时间见 报表包内的 chrome_access_events.csv、chrome_comment_coverage.csv 和 data/chrome-bridge-status.json。

发现页和评论页各使用一个扩展标签页，任务开始时激活并静音。发现页保存滚动位置，进程恢复或切换关键词时按数据库深度回放滚动。主页链接缺少日期时先核验视频详情，再判断今年范围。候选和排除原因见 报表包内的 discovery_candidates.csv、excluded_candidates.csv 和 candidate_review_sample.csv。
