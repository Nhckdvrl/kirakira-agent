import type { WebHostContextV1 } from "@akashic/web-ui-v1";
import { Field, Select, registerForm } from "../../shared/src/configuration";

const axes = [
  ["warmth", "温暖程度"], ["candor", "直率程度"], ["humor", "幽默程度"],
  ["curiosity", "追问习惯"], ["depth", "回答展开程度"], ["emoji", "表情使用"],
] as const;
const behaviors = [["support", "遇到困扰时"], ["suggestions", "闲聊中的建议"]] as const;

export function activate(ctx: WebHostContextV1) {
  return registerForm(ctx, {
    id: "prompt", title: "个性与相处", family: "companion", familyLabel: "相处方式", switchable: false,
    description: "分别选择说话习惯与回应方式；留空沿用人物底稿和已确认的用户偏好。本轮明确需求优先。",
    fields: ({ values, change, status }) => <>
      <p>说话习惯</p>
      {axes.map(([name, label]) => <Select key={name} label={label} name={name} value={values[name]}
        options={status.choices?.[name] ?? []} change={change} fallbackLabel="沿用原设定" />)}
      <p>回应方式：同样温暖，也可以先听你说，或回应后给个办法。</p>
      {behaviors.map(([name, label]) => <Select key={name} label={label} name={name} value={values[name]}
        options={status.choices?.[name] ?? []} change={change} fallbackLabel="沿用原设定" />)}
      <Field label="补充相处偏好" name="style_notes" value={values.style_notes} change={change}
        hint="例如：讨论代码时直接指出问题，分享生活时先接话。最多1000字；任务格式与本轮明确要求优先。" />
      <label className="config-field"><span>喜欢的说话示例</span>
        <textarea name="voice_examples" rows={4} maxLength={1500} value={typeof values.voice_examples === "string" ? values.voice_examples : ""}
          style={{font: "inherit", boxSizing: "border-box", width: "100%", minHeight: 110, padding: "10px 12px", borderRadius: 8,
            border: "1px solid var(--md-sys-color-outline, #8a8d82)", background: "var(--md-sys-color-surface, #fff)", color: "inherit", resize: "vertical"}}
          onChange={event => change("voice_examples", event.target.value)} />
        <small>可选，最多1500字。写几句喜欢的对话，只指导语气与节奏，不作为共同经历或事实记忆。</small>
      </label>
      <p>这是此 Agent 的默认相处方式，保存后在后续请求生效。只想这轮听你说、不提建议或认真办事，可以直接告诉它。</p>
      <p>人物底稿保存在 memory/VEDA.md；组合项不会改写它。建议与追问习惯不决定工具权限、主动联系的频率或是否启用后台任务。</p>
    </>,
  });
}
