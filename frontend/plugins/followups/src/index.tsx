import type { ConversationTabView } from "@akashic/conversation-tools-v2";
import type { WebHostContextV1, WebUiDisposer } from "@akashic/web-ui-v1";
import "./style.css";

interface Anchor { message_id: string; text: string; recorded_at: string }
interface Spec { watch_id: string; name: string; purpose: string; url: string }
interface Watch { spec: Spec; enabled: boolean }
interface Decision { tool: string; arguments: { reason?: string; message?: string } }
interface Event {
  item_id: string; revision: string; status: string;
  payload: { title: string; content: string; url: string }; decisions: Decision[];
  evidence: { kind: string; text?: string; recorded_at?: string; start?: number; end?: number; truncated?: boolean }[];
  deliveries: { text: string; confirmed_at: string }[]; feedback: { value: string } | null;
}
interface Timeline { watch: Watch; anchors: Anchor[]; cursor: { last_checked: string; last_error: string | null } | null; events: Event[] }

function el<K extends keyof HTMLElementTagNameMap>(tag: K, text = "", cls = ""): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag); node.textContent = text; node.className = cls; return node;
}
function button(label: string, action: () => void): HTMLButtonElement {
  const node = el("button", label); node.type = "button"; node.addEventListener("click", action); return node;
}
function date(value: string): string { return new Date(value).toLocaleString("zh-CN"); }

class FollowupTab {
  readonly root = el("section", "", "followups-root");
  private stopped = false;
  private session: string | null;
  private ticket = 0;
  private selected: string | null = null;
  private pendingId = "w_" + crypto.randomUUID().replaceAll("-", "");
  private timer: number | undefined;
  private active = true;
  constructor(private ctx: WebHostContextV1, session: string | null) { this.session = session; void this.load(); }

  setSession(session: string | null): void {
    this.session = session; this.selected = null; this.pendingId = "w_" + crypto.randomUUID().replaceAll("-", "");
    void this.load();
  }
  setActive(active: boolean): void { this.active = active; if (active && this.selected) void this.refreshTimeline(); }
  dispose(): void { this.stopped = true; ++this.ticket; window.clearTimeout(this.timer); }
  private async request<T>(path: string, body?: object): Promise<T> {
    const response = await this.ctx.http.request("/api/dashboard/followups" + path,
      body === undefined ? undefined : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!response.ok) {
      const value = await response.json().catch(() => ({})) as { detail?: string };
      throw new Error(typeof value.detail === "string" ? value.detail : `请求失败（${response.status}）`);
    }
    return await response.json() as T;
  }
  private error(error: unknown): void {
    if (this.stopped) return;
    const old = this.root.querySelector(".followups-error"); old?.remove();
    const message = el("p", error instanceof Error ? error.message : "读取失败", "followups-error");
    message.setAttribute("role", "alert"); this.root.prepend(message);
  }
  private async load(): Promise<void> {
    const ticket = ++this.ticket;
    window.clearTimeout(this.timer);
    if (!this.session) { this.root.replaceChildren(el("p", "先发送一条消息，再从当前对话建立持续跟进。")); return; }
    try {
      const value = await this.request<{ anchors: Anchor[]; watches: Watch[] }>("?session_id=" + encodeURIComponent(this.session));
      if (this.stopped || ticket !== this.ticket) return;
      const heading = el("h2", "让过去的讨论接上新的变化");
      const intro = el("p", "选择一段原始对话，关注订阅源、GitHub 发布或网页变化。首次读取只建立基线；后续更新经过调查，再决定是否联系你。");
      const settings = el("a", "先在主动设置中选择通知位置"); settings.href = "#wake-settings";
      const list = el("div", "", "followups-list");
      for (const watch of value.watches) {
        list.append(button(watch.spec.name + (watch.enabled ? "" : " · 已暂停采集"), () => { this.selected = watch.spec.watch_id; void this.refreshTimeline(); }));
      }
      this.root.replaceChildren(heading, intro, settings, list);
      if (this.selected) await this.refreshTimeline();
      else this.root.append(this.form(value.anchors));
      const add = button("新建跟进", () => { this.selected = null; void this.load(); }); this.root.append(add);
    } catch (error) { if (ticket === this.ticket) this.error(error); }
  }
  private form(anchors: Anchor[]): HTMLFormElement {
    const form = el("form");
    const field = (title: string, placeholder: string, multiline = false) => {
      const label = el("label", title);
      const input = multiline ? el("textarea") : el("input"); input.required = true; input.placeholder = placeholder;
      label.append(input); form.append(label); return input;
    };
    const name = field("跟进名称", "例如：青禾项目的存储升级"); name.maxLength = 80;
    const purpose = field("关注什么", "什么变化值得告诉你？什么情况可以跳过？", true); purpose.maxLength = 1000;
    const url = field("信息源", "RSS / Atom 地址，或 https://github.com/owner/repo");
    const sourceLabel = el("label", "来源类型"); const sourceKind = el("select");
    sourceKind.setAttribute("aria-label", "来源类型");
    for (const [value, label] of [["feed", "RSS / Atom · GitHub 发布"], ["webpage", "普通网页 · 关注正文变化"]]) {
      const option = el("option", label); option.value = value; sourceKind.append(option);
    }
    sourceLabel.append(sourceKind); form.append(sourceLabel);
    const regionLabel = el("label", "关注区域（可选 XPath）"); const region = el("input");
    region.placeholder = "例如：//*[@id='pricing']；留空关注正文"; region.maxLength = 512;
    regionLabel.append(region); regionLabel.hidden = true; form.append(regionLabel);
    sourceKind.addEventListener("change", () => {
      regionLabel.hidden = sourceKind.value !== "webpage";
      url.placeholder = sourceKind.value === "webpage" ? "https://example.com/updates" : "RSS / Atom 地址，或 https://github.com/owner/repo";
    });
    const interval = field("检查间隔（分钟）", "15") as HTMLInputElement;
    interval.type = "number"; interval.min = "1"; interval.max = "1440"; interval.value = "15";
    const modeLabel = el("label", "调查方式"); const mode = el("select");
    mode.setAttribute("aria-label", "调查方式");
    for (const [value, label] of [["each_update", "逐条调查 · 明确跟进的事项"], ["interest", "兴趣筛选后调查 · 宽泛的信息源"]]) {
      const option = el("option", label); option.value = value; mode.append(option);
    }
    modeLabel.append(mode); form.append(modeLabel);
    const group = el("fieldset"); group.append(el("legend", "为什么关注 · 选择原始对话（最多 6 条）"));
    for (const anchor of anchors) {
      const label = el("label", "", "followups-anchor"); const checkbox = el("input"); checkbox.type = "checkbox"; checkbox.value = anchor.message_id;
      label.append(checkbox, el("span", date(anchor.recorded_at) + "\n" + anchor.text)); group.append(label);
    }
    form.append(group);
    const submit = el("button", "开始跟进"); submit.type = "submit"; form.append(submit);
    form.addEventListener("submit", (event) => {
      event.preventDefault(); const session = this.session; if (!session) return;
      const selected = [...group.querySelectorAll<HTMLInputElement>("input:checked")].map(node => node.value);
      if (!selected.length || selected.length > 6) { this.error(new Error("请选择 1 到 6 条原始对话")); return; }
      submit.disabled = true;
      void this.request<Watch>("", { watch_id: this.pendingId, session_id: session, name: name.value, purpose: purpose.value,
        url: url.value, anchors: selected, interval_seconds: Number(interval.value) * 60, review_mode: mode.value,
        source_kind: sourceKind.value, xpath: sourceKind.value === "webpage" && region.value.trim() ? region.value.trim() : null }).then(watch => {
        if (this.session !== session || this.stopped) return;
        this.selected = watch.spec.watch_id; this.pendingId = "w_" + crypto.randomUUID().replaceAll("-", ""); void this.load();
      }).catch(error => this.error(error)).finally(() => { submit.disabled = false; });
    });
    return form;
  }
  private async refreshTimeline(): Promise<void> {
    window.clearTimeout(this.timer);
    const selected = this.selected, ticket = this.ticket;
    if (!selected || !this.active) return;
    try {
      const value = await this.request<Timeline>("/" + selected);
      if (this.stopped || ticket !== this.ticket || selected !== this.selected) return;
      this.root.querySelector(".followups-timeline")?.remove();
      this.root.querySelector("form")?.remove();
      const pane = el("div", "", "followups-timeline"); pane.append(el("h3", value.watch.spec.name), el("p", value.watch.spec.purpose));
      pane.append(button(value.watch.enabled ? "暂停采集" : "恢复采集", () => {
        void this.request("/enabled", { watch_id: selected, enabled: !value.watch.enabled }).then(() => this.load()).catch(error => this.error(error));
      }), button("现在检查", () => {
        void this.request<{ status: string; error?: string }>("/poll", { watch_id: selected }).then(result => {
          if (result.status === "error") this.error(new Error("信息源读取失败：" + result.error));
          return this.refreshTimeline();
        }).catch(error => this.error(error));
      }));
      pane.append(el("p", "暂停只停止后续采集，已进入调查的更新仍可能完成。", "followups-note"));
      const original = el("details"); original.append(el("summary", "最初的关注依据"));
      for (const anchor of value.anchors) original.append(el("blockquote", date(anchor.recorded_at) + "\n" + anchor.text));
      pane.append(original);
      if (value.cursor) pane.append(el("p", `最近检查：${date(value.cursor.last_checked)}${value.cursor.last_error ? " · 读取失败，将重试" : ""}`, "followups-note"));
      if (!value.events.length) pane.append(el("p", "等待新变化。建立基线时的信息不会触发历史通知。"));
      for (const event of value.events) pane.append(this.event(event, selected));
      this.root.append(pane);
      this.timer = window.setTimeout(() => { void this.refreshTimeline(); }, 8000);
    } catch (error) { this.error(error); }
  }
  private event(event: Event, watch: string): HTMLElement {
    const card = el("article"); const link = el("a", event.payload.title || "信息源更新");
    link.href = event.payload.url; link.target = "_blank"; link.rel = "noopener noreferrer"; card.append(link);
    const decision = [...event.decisions].reverse().find(value => value.tool === "skip_content" || value.tool === "share_content");
    const ready = event.status === "ready_for_delivery";
    const status = event.deliveries.length ? "已送达" : decision?.tool === "skip_content" ? "本轮已跳过"
      : ready ? "已决定分享，等待送达" : ["delivered", "settled"].includes(event.status) ? "已结算，尚未找到投递确认" : "等待兴趣筛选或调查";
    card.append(el("p", status, "followups-status"));
    if (decision) card.append(el("p", "本轮调查：" + (decision.arguments.reason || decision.arguments.message || "")));
    const detail = el("details"); detail.append(el("summary", "查看新信息与调查依据"), el("p", event.payload.content));
    for (const evidence of event.evidence) {
      const range = evidence.kind === "source_read" ? `原始消息第 ${evidence.start}—${evidence.end} 字符\n` : "";
      detail.append(el("blockquote", range + (evidence.recorded_at ? date(evidence.recorded_at) + "\n" : "")
        + (evidence.text || "已记录原文读取范围。")
        + (evidence.truncated ? "\n（界面摘录已截短；完整读取保留在调查记录中。）" : "")));
    }
    if (!event.evidence.length) detail.append(el("p", "当前尚无记忆工具的成功读取记录。"));
    card.append(detail);
    for (const delivered of event.deliveries) card.append(el("p", "确认送达：" + date(delivered.confirmed_at), "followups-note"));
    if (event.deliveries.length) {
      for (const [value, label] of [["useful", "有用"], ["irrelevant", "与我无关"]]) {
        card.append(button((event.feedback?.value === value ? "✓ " : "") + label, () => {
          void this.request("/feedback", { watch_id: watch, item_id: event.item_id, revision: event.revision, value })
            .then(() => this.refreshTimeline()).catch(error => this.error(error));
        }));
      }
    }
    return card;
  }
}

export function activate(ctx: WebHostContextV1): WebUiDisposer {
  return ctx.ui.inject("conversation.tools.v2", mount => mount.register({
    id: "followups", label: "跟进", order: 35, hint: "从过去的讨论跟进新的变化", accepts: [],
    icon: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16M4 12h10M4 19h7M17 10v10m-4-4 4 4 4-4"/></svg>',
    render(host, _entryView, props) {
      const view = props as ConversationTabView;
      const tab = new FollowupTab(ctx, view.session); host.replaceChildren(tab.root); tab.setActive(view.active);
      const stops = [view.onSessionChange(session => tab.setSession(session)), view.onActiveChange(active => tab.setActive(active))];
      return () => { stops.forEach(stop => stop()); tab.dispose(); host.replaceChildren(); };
    },
  }));
}
