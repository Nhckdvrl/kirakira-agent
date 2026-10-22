import type { WebHostContextV1 } from "@akashic/web-ui-v1";
import { Check, Field, Select, registerForm } from "../../shared/src/configuration";

export function activate(ctx: WebHostContextV1) {
  return registerForm(ctx, {id: "drift", title: "空闲思考", family: "proactive", familyLabel: "主动交互",
    description: "结合相处记录，接续话题、协助未完事项或探索发现；没有合适理由就保持安静。", switchable: false,
    fields: ({values, change, status}) => <>
      <Check label="自动思考" name="auto_enabled" value={values.auto_enabled} fallback={status.defaults?.auto_enabled} change={change} hint="每次新讨论最多形成一个提案；关闭后停止新规划，已被接受的任务保留执行记录。" />
      <Check label="主动接续聊天" name="allow_conversation" value={values.allow_conversation} fallback={status.defaults?.allow_conversation} change={change} hint="允许围绕已有话题自然问候或接着聊；不凭空猜测你的心情和进度，也不要求每次都给出研究结果。" />
      <Select label="参考对话" name="context_session_id" value={values.context_session_id} options={status.choices?.context_session_id ?? []} change={change} fallbackLabel="请选择一段已有对话" hint="记忆与研究继承这段对话的范围；通知使用主动联系中配置的收件位置。" />
      <Field label="思考范围" name="purpose" value={values.purpose} fallback={status.defaults?.purpose} change={change} />
      <Field label="空闲等待秒数" name="idle_seconds" type="number" min={0} max={86400} value={values.idle_seconds} fallback={status.defaults?.idle_seconds} change={change} hint="对话仍在回复时不启动规划。" />
      <Field label="两次思考最短间隔" name="interval_seconds" type="number" min={60} max={86400} value={values.interval_seconds} fallback={status.defaults?.interval_seconds} change={change} hint="单位为秒。" />
      <Field label="每天最多规划次数" name="daily_budget" type="number" min={1} max={12} value={values.daily_budget} fallback={status.defaults?.daily_budget} change={change} hint="按UTC日计数，包括选择保持安静和失败的规划；后续研究的模型调用另行计量。" />
    </>,
  });
}
