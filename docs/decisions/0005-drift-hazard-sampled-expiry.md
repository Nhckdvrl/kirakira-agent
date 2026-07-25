# 0005 Drift 用采样到期驱动,不用轮询判阈

- 状态:accepted
- 关联:`kirakira_agent/drift/drive.py`、`DriftRunner._hazard_due`、`drift.db:drift_schedule`

## 背景

原本 Drift 用固定 `min_interval_hours` 门控:满 N 小时就允许跑一轮。体感是"定时打卡",
而不是"闲下来了才去做点事"。Reference 有一套 hazard 模型,把触发变成连续量。

## 决定

移植 Reference `plugins/wake_proactive/drift_drive.py` 的两部分:

- `advance_drift_drive()`:把速率(空闲驱动 × 内容/近期 Drift/重复三项抑制)对时间积分,
  按 12 小时半衰期衰减,给出 hazard 与可读理由;
- `sample_drift_delay_hours()`:**从 hazard 分布采样下一次到期时刻**,存进
  `drift_schedule(session_key, timer_anchor, next_attempt_at)`,到期才尝试。

`min_interval_hours` 保留为硬下限(安全阀),hazard 在其之上决定"什么时候算闲下来了"。

## 理由

采样到期而不是轮询判阈,是因为轮询会把"检查得越频繁越容易触发"这种采样假象混进来——
同一套 hazard,tick 间隔从 40 分钟改成 10 分钟就会显著更常触发。Reference 的注释写得很直白:
到期事件只负责开启一次判别。

`timer_anchor` 取 `(last_user_at, last_drift_at)`:用户又说话或刚跑过 Drift 时锚点变化,
按新的空闲状态重新采样,而不是抱着旧到期时刻不放。

## 没有空闲基准时的处理

session 里一条用户消息都没有时,"空闲多久"无从计算。此时**不额外设卡**,交回 `min_interval`
判断。反过来做(没基准就不跑)会让全新部署永远不触发 Drift。

## 影响

到期时刻落盘,进程重启不会重新推迟。跑过一轮后清掉排程,下一轮按新的空闲状态重采样。

## 验收

- 速率随空闲增长,三项抑制都能压低;越界输入被 clamp;
- 抑制越强采样到期越晚;`random_draw` 越大越晚;退化输入不空转;
- 首次观察只采样不跑;到期后触发并清排程;用户再说话触发重采样;
- 到期时刻跨重启保持;
- 无用户消息时不设卡。
