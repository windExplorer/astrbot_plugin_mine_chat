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

createApp(App).use(router).mount("#app");
