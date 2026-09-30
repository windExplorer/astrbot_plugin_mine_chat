// API 封装：通过 AstrBot 官方插件 Page 桥接调用后端。
// 桥接会正确处理路由与 asset_token 鉴权；**不能**用裸 fetch 相对路径（会 CORS 失败）。

import { whenBridgeReady } from "./bridge";

const PAGE_PLUGIN_NAME = "astrbot_plugin_mine_chat";

// 不同 AstrBot 版本对端点前缀的处理略有差异，逐个候选尝试（首个成功即返回）。
function endpointCandidates(routePath: string): string[] {
  const clean = routePath.replace(/^\/+/, "");
  const list = [clean, "/" + clean, `${PAGE_PLUGIN_NAME}/${clean}`, `/${PAGE_PLUGIN_NAME}/${clean}`];
  return [...new Set(list.map((s) => s.replace(/\/+/g, "/")).filter(Boolean))];
}

function isRouteMissing(payload: any): boolean {
  if (!payload || typeof payload !== "object") return false;
  const text =
    String(payload.error || "") + " " + String(payload.message || "") + " " + String(payload.detail || "");
  return /未找到.*路由|route.*not.*found|not.*found.*route|404/i.test(text);
}

function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(label + " 超时（" + ms / 1000 + "s 无响应）")), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

/**
 * 解析后端返回。正常情况下 Dashboard 已经吃掉一层信封（成功时只把 `data` 转发过来），
 * 这里做防御式解包。
 */
function unwrap(payload: any): any {
  if (payload && typeof payload === "object") {
    if (payload.status === "error") throw new Error(payload.message || "请求失败");
    if (payload.status === "ok" && "data" in payload) return payload.data;
    if (Object.prototype.hasOwnProperty.call(payload, "code")) {
      if (payload.code !== 0) throw new Error(payload.message || "请求失败");
      return payload.data;
    }
  }
  return payload;
}

/** 只有「路由不存在」才值得换下一种端点写法重试；业务错误必须立刻抛出。 */
function isRouteMissingError(e: any): boolean {
  if (isRouteMissing(e)) return true;
  const text = String(e?.message || e?.toString?.() || e || "");
  return /未找到.*路由|route.*not.*found|not.*found.*route|404|not found/i.test(text);
}

async function request(path: string, method: "GET" | "POST", body?: unknown, timeoutMs?: number): Promise<any> {
  const br = await whenBridgeReady();
  const url = new URL(path, "https://astrbot-plugin-page.local/");
  const candidates = endpointCandidates(url.pathname);
  const errors: string[] = [];
  const ms = timeoutMs ?? 20000;

  if (method === "GET") {
    const params = Object.fromEntries(url.searchParams.entries());
    for (const c of candidates) {
      try {
        const p = await withTimeout(br.apiGet(c, Object.keys(params).length ? params : undefined), ms, "GET " + c);
        if (isRouteMissing(p)) {
          errors.push(p.message || p.error || "未找到该路由");
          continue;
        }
        return unwrap(p);
      } catch (e: any) {
        if (!isRouteMissingError(e)) throw e;
        errors.push(e?.message || String(e));
      }
    }
    throw new Error(errors[0] || "未找到可用的页面 API 路由");
  }

  let payload = body || {};
  try {
    payload = JSON.parse(JSON.stringify(payload));
  } catch {
    /* ignore */
  }
  for (const c of candidates) {
    try {
      const r = await withTimeout(br.apiPost(c, payload), ms, "POST " + c);
      if (isRouteMissing(r)) {
        errors.push(r.message || r.error || "未找到该路由");
        continue;
      }
      return unwrap(r);
    } catch (e: any) {
      // POST 绝不能因为业务错误被重试多次（会出现重复写库）
      if (!isRouteMissingError(e)) throw e;
      errors.push(e?.message || e?.toString?.() || String(e));
    }
  }
  throw new Error(errors[0] || "未找到可用的页面 API 路由");
}

/** GET，endpoint 形如 "/plan"、"logs?limit=50"。 */
export function apiGet<T = any>(endpoint: string, timeoutMs?: number): Promise<T> {
  return request(endpoint, "GET", undefined, timeoutMs) as Promise<T>;
}

/** POST，body 作为 JSON 负载。 */
export function apiPost<T = any>(endpoint: string, body?: unknown, timeoutMs?: number): Promise<T> {
  return request(endpoint, "POST", body || {}, timeoutMs) as Promise<T>;
}

// ---------------------------------------------------------------- 类型
export interface PlanItem {
  id: number;
  seq: number;
  start_min: number;
  end_min: number;
  start_text: string;
  end_text: string;
  activity: string;
  mood: string;
  message_seed: string;
  confidence: number | null;
}

export interface PlanPayload {
  persona_id: string;
  persona_name?: string;
  plan_date: string;
  source: string | null;
  quality: number | null;
  note: string | null;
  generated_at: number | null;
  items: PlanItem[];
  current_text?: string;
  now_minute?: number;
}

export interface LogRow {
  id: number;
  ts: number;
  ts_text: string;
  decision: "send" | "skip" | "error" | string;
  reason: string;
  umo: string;
  content: string;
}

export interface ProactiveStatus {
  persona_id: string;
  persona_name?: string;
  enabled: boolean;
  primary_umo: string;
  next_at: number | null;
  next_at_text: string;
  sent_today: number;
  daily_limit: number;
  unanswered: number;
  max_unanswered: number;
  last_sent_at: number | null;
  last_message: string;
  last_user_at: number | null;
  quiet_hours: string;
}

export interface BindingRow {
  umo: string;
  persona_id: string;
  kind: string;
  is_primary: boolean;
  enabled: boolean;
  first_seen_at: number;
  last_seen_at: number;
}

export interface SetupState {
  configured: boolean;
  persona_id: string;
  persona_name: string;
  primary_umo: string;
  missing: string;
  hint: string;
}

export interface OverviewPayload {
  version: string;
  enabled: boolean;
  configured: boolean;
  setup_hint: string;
  active_persona: string;
  persona_id: string;
  persona_name: string;
  personas: { persona_id: string; persona_name: string; enabled: boolean }[];
  bindings: { umo: string; kind: string; is_primary: boolean; last_seen_at: number }[];
  plan: PlanPayload | Record<string, never>;
  proactive: ProactiveStatus | Record<string, never>;
  log_summary: Record<string, number>;
  recent_logs: LogRow[];
}

export interface PersonaStored {
  persona_id: string;
  persona_name: string;
  enabled: boolean;
  created_at: number;
}

export interface PersonasPayload {
  stored: PersonaStored[];
  available: { persona_id: string; system_prompt: string }[];
  active_persona: string;
  setup: SetupState;
}

export interface ConfigItem {
  /** 点路径，如 "persona.active"；保存时以它为键。 */
  path: string;
  key: string;
  type: string;
  description: string;
  hint: string;
  default: any;
  value: any;
  /** 特殊控件：model = 从 AstrBot 已加载的对话模型里下拉选择。 */
  widget?: string;
  options?: string[];
  labels?: string[];
  item_options?: string[];
  item_labels?: string[];
}

export interface ProviderOption {
  id: string;
  model: string;
  label: string;
  is_default: boolean;
}

export interface ConfigGroup {
  name: string;
  description: string;
  hint: string;
  items: ConfigItem[];
}

export interface ConfigPayload {
  groups: ConfigGroup[];
  version: string;
}

// ---------------------------------------------------------------- 端点
export function apiOverview(personaId = "") {
  return apiGet<OverviewPayload>(`/overview${personaId ? `?persona_id=${encodeURIComponent(personaId)}` : ""}`);
}

export function apiPlan(personaId: string, planDate = "") {
  const p = new URLSearchParams();
  if (personaId) p.set("persona_id", personaId);
  if (planDate) p.set("date", planDate);
  return apiGet<PlanPayload>(`/plan?${p.toString()}`, 60000);
}

/** 重新生成日程（真打模型，给足超时）。 */
export function apiPlanRefresh(personaId: string, planDate = "") {
  return apiPost<PlanPayload>("/plan/refresh", { persona_id: personaId, date: planDate }, 180000);
}

export function apiPlanUpdate(itemId: number, patch: { activity?: string; mood?: string; message_seed?: string }) {
  return apiPost<{ updated: number }>("/plan/update", { item_id: itemId, ...patch });
}

export function apiPersonas() {
  return apiGet<PersonasPayload>("/personas");
}

/** 把某个人格设为当前人格（写配置 active_persona）。 */
export function apiPersonaActivate(personaId: string) {
  return apiPost<{ active_persona: string; configured: boolean; hint: string }>(
    "/personas/activate",
    { persona_id: personaId },
  );
}

export function apiPersonaSave(personaId: string, enabled: boolean) {
  return apiPost<{ persona_id: string; enabled: boolean }>("/personas/save", {
    persona_id: personaId,
    enabled,
  });
}

export function apiBindings(personaId = "") {
  return apiGet<{ items: BindingRow[] }>(`/bindings${personaId ? `?persona_id=${encodeURIComponent(personaId)}` : ""}`);
}

export function apiBindingSave(body: {
  umo: string;
  persona_id: string;
  is_primary?: boolean;
  enabled?: boolean;
}) {
  return apiPost<{ umo: string; persona_id: string }>("/bindings/save", body);
}

export function apiBindingDelete(umo: string) {
  return apiPost<{ deleted: string }>("/bindings/delete", { umo });
}

export function apiBindingPrimary(personaId: string, umo: string) {
  return apiPost<{ persona_id: string; primary_umo: string }>("/bindings/primary", {
    persona_id: personaId,
    umo,
  });
}

export function apiProactiveState(personaId = "") {
  return apiGet<ProactiveStatus>(`/proactive/state${personaId ? `?persona_id=${encodeURIComponent(personaId)}` : ""}`);
}

export function apiProactiveToggle(personaId: string, enabled: boolean) {
  return apiPost<{ persona_id: string; enabled: boolean }>("/proactive/toggle", {
    persona_id: personaId,
    enabled,
  });
}

/** 立即触发一次（真打模型 + 真发送，给足超时）。 */
/** 手动「立即主动」：后端会跳过免打扰/睡眠/每日上限等环境闸门，仅保留开关与窗口检查。 */
export function apiProactiveNow(personaId: string) {
  return apiPost<{ sent: boolean; reason: string }>("/proactive/now", { persona_id: personaId }, 180000);
}

export function apiLogs(params: {
  persona_id?: string;
  limit?: number;
  offset?: number;
  decision?: string;
  reason?: string;
} = {}) {
  const p = new URLSearchParams();
  if (params.persona_id) p.set("persona_id", params.persona_id);
  p.set("limit", String(params.limit || 50));
  p.set("offset", String(params.offset || 0));
  if (params.decision) p.set("decision", params.decision);
  if (params.reason) p.set("reason", params.reason);
  return apiGet<{ items: LogRow[]; total: number }>(`/logs?${p.toString()}`);
}

export function apiConfig() {
  return apiGet<ConfigPayload>("/config");
}

/** AstrBot 已加载的对话模型列表（模型选择下拉的数据源）。 */
export function apiProviders() {
  return apiGet<{ items: ProviderOption[]; default_id: string }>("/providers");
}

export function apiConfigSave(values: Record<string, any>) {
  return apiPost<{ changed: string[] }>("/config/save", { values });
}

/** 主动消息拒绝原因的中文说明（与后端 main._reason_text 保持一致）。 */
export const REASON_TEXT: Record<string, string> = {
  ok: "正常发出",
  disabled: "插件或主动消息已关闭",
  quiet_hours: "免打扰时段",
  sleeping: "角色在睡觉",
  daily_limit: "今日次数已用完",
  min_interval: "距上次太近",
  unanswered_limit: "连续未回复达上限",
  user_active: "用户刚说过话",
  no_umo: "没有投递窗口",
  no_seed: "没有可用片段",
  not_due: "未到候选时间",
  llm_error: "模型调用失败",
  empty: "模型没产出内容",
  send_failed: "消息未送达",
  interrupted: "生成期间用户说话了",
  // 配图通道（decision=img）
  img_ok: "这条带了配图",
  img_off: "配图未开启或概率为 0",
  img_dice: "配图掷骰未命中",
  img_limit: "今日配图已达上限",
  img_no_event: "萌绘出图缺用户事件（先聊一句）",
  img_dir_empty: "本地图库为空或不可读",
  img_fail: "配图生成失败",
  // 表情通道（decision=sticker）
  sticker_ok: "这条带了表情",
  sticker_off: "表情未开启或概率为 0",
  sticker_dice: "表情掷骰未命中",
  sticker_limit: "今日表情已达上限",
  sticker_fail: "表情拉取失败（未装 moe_meme 或网络失败）",
  sticker_yield: "表情让位给了配图（一条消息只带一张图）",
};

export function reasonText(reason: string): string {
  return REASON_TEXT[reason] || reason || "-";
}
