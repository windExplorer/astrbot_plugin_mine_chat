import { createApp } from "vue";
import { createRouter, createWebHashHistory } from "vue-router";

import App from "./App.vue";
import OverviewView from "./views/OverviewView.vue";
import PlanView from "./views/PlanView.vue";
import WorldView from "./views/WorldView.vue";
import ProactiveView from "./views/ProactiveView.vue";
import ScopeView from "./views/ScopeView.vue";
import ConfigView from "./views/ConfigView.vue";

// hash 路由：AstrBot 静态资源按真实文件路径解析，history 模式刷新会 404。
// 必须保持同步导入 —— vite.config 里 inlineDynamicImports 是有意为之
// （多 chunk 会被 AstrBot 的 asset_token 重写搞成 401 白屏）。
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/overview" },
    { path: "/overview", name: "overview", component: OverviewView },
    { path: "/plan", name: "plan", component: PlanView },
    { path: "/world", name: "world", component: WorldView },
    { path: "/proactive", name: "proactive", component: ProactiveView },
    { path: "/scope", name: "scope", component: ScopeView },
    { path: "/config", name: "config", component: ConfigView },
    { path: "/:pathMatch(.*)*", redirect: "/overview" },
  ],
});

// 本页跑在 AstrBot 的 iframe 里，父面板刷新会按原始 src 重建 iframe——
// hash 路由随之丢失、落回总览。把上次页面名记在 sessionStorage（同标签
// 页刷新后可读），加载时无显式 hash 就恢复；沙箱禁用 storage 时静默退化。
const LAST_PAGE_KEY = "mine_chat:last_page";

function readSavedPage(): string {
  try {
    return sessionStorage.getItem(LAST_PAGE_KEY) || "";
  } catch {
    return ""; // sandbox 无 storage 权限：退化为默认总览
  }
}

async function bootstrap() {
  const saved = readSavedPage();
  if (saved && !window.location.hash) {
    try {
      await router.replace({ name: saved });
    } catch {
      /* 存了不存在的页面名（版本更迭）：留在总览 */
    }
  }
  createApp(App).use(router).mount("#app");
}

void bootstrap();
