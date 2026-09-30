// AstrBot 插件 Page 的官方 bridge 访问层（含 sandbox 环境的安全封装）。
//
// ⚠️ 关键环境约束：AstrBot 把插件页塞进 sandbox iframe：
//     <iframe sandbox="allow-scripts allow-forms allow-downloads">
//                                        ^ 没有 allow-same-origin
// 因此 `localStorage` / `sessionStorage` / `document.cookie` 一被访问就抛
// `SecurityError`，而且发生在 Vue mount 阶段时整页白屏。
//
// 所以约定两条：
//   1) 任何持久化都走 storageGet/storageSet（内部 try/catch + 内存兜底）；
//   2) 主题这类环境信息优先读 bridge 的 context（`isDark`），比自己存更正确。

export interface BridgeContext {
  pluginName?: string;
  displayName?: string;
  pageName?: string;
  pageTitle?: string;
  locale?: string;
  isDark?: boolean;
}

export interface AstrBotPageBridge {
  ready?(): Promise<BridgeContext>;
  getContext?(): BridgeContext | null;
  onContext?(handler: (ctx: BridgeContext) => void): () => void;
  apiGet(endpoint: string, params?: Record<string, any>): Promise<any>;
  apiPost(endpoint: string, body?: Record<string, any>): Promise<any>;
}

/** 取 iframe 内的 bridge 实例（由官方 bridge-sdk 注入到 window 上）。 */
export function getBridge(): AstrBotPageBridge | null {
  const w = window as any;
  if (w.AstrBotPluginPage) return w.AstrBotPluginPage as AstrBotPageBridge;
  // 极少数宿主会把页面直接内联在主文档里，此时 bridge 挂在父窗口。
  // sandbox iframe 里访问 window.parent 的属性会抛 SecurityError，必须捕获。
  try {
    if (w.parent && w.parent !== w && w.parent.AstrBotPluginPage) {
      return w.parent.AstrBotPluginPage as AstrBotPageBridge;
    }
  } catch {
    return null;
  }
  return null;
}

function isUsable(b: AstrBotPageBridge | null | undefined): b is AstrBotPageBridge {
  return Boolean(b && typeof b.apiGet === "function" && typeof b.apiPost === "function");
}

/** 当前面板下发的 context（可能为 null：bridge 未就绪或页面被独立打开）。 */
export function getContext(): BridgeContext | null {
  try {
    return getBridge()?.getContext?.() || null;
  } catch {
    return null;
  }
}

/** 订阅 context 变化（主题切换等）；已存在 context 时会立即回调一次。 */
export function onContext(handler: (ctx: BridgeContext) => void): () => void {
  try {
    const off = getBridge()?.onContext?.(handler);
    return typeof off === "function" ? off : () => {};
  } catch {
    return () => {};
  }
}

let _ready: Promise<AstrBotPageBridge> | null = null;

/**
 * 等桥接**真正能干活**（握手完成）再放行——而不只是等对象出现。
 *
 * 坑：`AstrBotPluginPage` 由父页面异步注入，对象刚出现时父页面还没回过
 * `context`、消息监听也可能没挂好，此时发 `apiGet` 要么被静默丢掉、要么超时。
 * 修复方式：等到 `getContext()` 有值（父页面在收到 ready 后回的那一帧就是握手信号）。
 */
export function whenBridgeReady(timeoutMs = 2500): Promise<AstrBotPageBridge> {
  if (_ready) return _ready;
  _ready = (async () => {
    const start = Date.now();
    while (true) {
      const b = getBridge();
      if (isUsable(b) && getContext()) return b;
      if (Date.now() - start > timeoutMs) {
        if (isUsable(b)) return b;
        throw new Error("未检测到 AstrBot 插件 Page 桥接，请从 AstrBot 后台的插件拓展页打开本页面");
      }
      await new Promise((r) => setTimeout(r, 100));
    }
  })().catch((e) => {
    _ready = null;
    throw e;
  });
  return _ready;
}

// ------------------------------------------------------------------ //
// sandbox 安全存储
// ------------------------------------------------------------------ //
const memory = new Map<string, string>();
let lsUsable: boolean | null = null;

function localStorageUsable(): boolean {
  if (lsUsable === null) {
    try {
      window.localStorage.getItem("__probe__");
      lsUsable = true;
    } catch {
      lsUsable = false;
    }
  }
  return lsUsable;
}

/** 读本地存储；sandbox iframe 中不可用时退回内存（本次会话内有效）。 */
export function storageGet(key: string): string | null {
  if (localStorageUsable()) {
    try {
      const v = window.localStorage.getItem(key);
      if (v !== null) return v;
    } catch {
      /* 落到内存 */
    }
  }
  return memory.has(key) ? (memory.get(key) as string) : null;
}

/** 写本地存储；不可用时只写内存，绝不抛异常。 */
export function storageSet(key: string, value: string): void {
  memory.set(key, value);
  if (!localStorageUsable()) return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* 忽略：内存里已经有了 */
  }
}
