# 0004 用内容指纹 + 时间窗做跨崩溃投递去重,不做两阶段 outbox

- 状态:accepted
- 关联:`proactive_v2/state.py`(deliveries 表)、`ProactiveLoop._deliver`、
  `agent/turns/result.py`

## 背景

主动链路会等渠道 callback 成功才提交(写 Session、起冷却)。但进程若在**渠道已成功**与
**本地提交完成**之间崩溃,重启后同一条内容会被再发一次。审计把这条列为 P0。

## 决定

照 Reference `proactive_v2/state.py` 的做法:`deliveries(session_key, delivery_key, sent_at)`
表 + 时间窗判重,而不是两阶段 outbox。

`delivery_key` 取消息内容的 sha256——同一内容跨进程重启仍得到同一个 key。

提交顺序借 `TurnResult` 的三类副作用表达:

```text
side_effects          mark_delivery        ← 发送前落地投递意图
      ↓
   dispatch           渠道投递
      ↓
success_side_effects  写 Session / 起冷却
failure_side_effects  unmark_delivery      ← 仅渠道明确失败时撤销
```

## 理由

崩溃走不到任何一组提交副作用,因此**标记保留**,重启后同内容命中去重。渠道明确报告失败
时才撤销标记,所以正常的失败重试不受影响——这两种情况必须区分,否则要么丢重试、要么丢去重。

不做两阶段 outbox 的原因:outbox 要额外维护待发队列、重放器与幂等消费端,而本项目要解决的
只是"同一条内容不要发两次"。内容指纹判重用一张表就覆盖了这个语义,与 Reference 一致。

## 代价(明确承认)

"标记之后、发送之前"崩溃会漏发这一条。取这一侧是因为:对主动推送而言,重复打扰比偶尔漏发
更伤用户体验。这不是 exactly-once,是**至多一次 + 窗口内不重复**。

窗口取 `delivery_cooldown_hours`(与推送冷却同一参数):超窗后同样内容可以再发,
避免"某条内容永久无法再次推送"。

## 验收

- 标记落盘并在重开库后仍能判重(崩溃场景的实质);
- 窗口内判重、超窗放行、按 session 隔离;
- 渠道失败撤销标记后能重试;
- `sent_at` 损坏时按"未投递"处理——宁可重发一次,也不要因脏数据永久静默。
