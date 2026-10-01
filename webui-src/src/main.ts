import { createApp } from "vue";
import { createRouter, createWebHashHistory } from "vue-router";

import App from "./App.vue";
import { apiGetLastPage } from "./api";
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

// 本页跑在 AstrBot 的 sandbox iframe 里（无 allow-same-origin）：
// hash 不会反映到父面板地址栏，父面板刷新重建 iframe 后 hash 丢失，
// localStorage/sessionStorage 也因 opaque origin 不可用。
// 因此把上次页面名**存服务端**（meta 表），加载时先问后端恢复——
// 这是沙箱约束下唯一跨刷新可靠的方案（参考官方 bridge 仅有 api/files/sse 能力）。
const VALID_PAGES = new Set(["overview", "plan", "world", "proactive", "scope", "config"]);

async function bootstrap() {
  if (!window.location.hash) {
    try {
      const res = await apiGetLastPage();
      const page = String(res?.page || "");
      if (page && VALID_PAGES.has(page)) {
        await router.replace({ name: page });
      }
    } catch {
      /* 独立打开（无 bridge）或后端不可用：默认总览 */
    }
  }
  createApp(App).use(router).mount("#app");
}

void bootstrap();
