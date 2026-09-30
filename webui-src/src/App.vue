<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  NConfigProvider,
  NMessageProvider,
  NDialogProvider,
  NLayout,
  NLayoutSider,
  NLayoutContent,
  NMenu,
  NTag,
  darkTheme,
} from "naive-ui";
import type { MenuOption } from "naive-ui";

import { onContext, getContext } from "./bridge";
import { PLUGIN_VERSION } from "./version";

const route = useRoute();
const router = useRouter();
const isDark = ref(false);

const menuOptions: MenuOption[] = [
  { label: "总览", key: "overview" },
  { label: "日程", key: "plan" },
  { label: "世界观", key: "world" },
  { label: "主动消息", key: "proactive" },
  { label: "人格与窗口", key: "scope" },
  { label: "配置", key: "config" },
];

const activeKey = ref("overview");

function syncTheme(ctx: { isDark?: boolean } | null) {
  if (ctx && typeof ctx.isDark === "boolean") isDark.value = ctx.isDark;
}

onMounted(() => {
  syncTheme(getContext());
  const off = onContext((ctx) => syncTheme(ctx));
  onUnmounted(() => off());
  activeKey.value = (route.name as string) || "overview";
});

function handleMenu(key: string) {
  activeKey.value = key;
  router.push({ name: key });
}
</script>

<template>
  <n-config-provider :theme="isDark ? darkTheme : null">
    <n-message-provider>
      <n-dialog-provider>
        <n-layout class="shell" has-sider>
          <n-layout-sider
            class="sider"
            :width="188"
            :native-scrollbar="false"
            bordered
            content-style="display:flex;flex-direction:column;height:100%;"
          >
            <div class="brand">
              <div class="brand-title">萌萌日程</div>
              <div class="brand-sub">角色的日常与主动</div>
            </div>
            <n-menu
              class="nav"
              :value="activeKey"
              :options="menuOptions"
              :root-indent="18"
              @update:value="handleMenu"
            />
            <div class="foot">
              <n-tag size="small" :bordered="false">{{ PLUGIN_VERSION }}</n-tag>
            </div>
          </n-layout-sider>

          <n-layout-content class="content" :native-scrollbar="false">
            <div class="page-wrap">
              <router-view />
            </div>
          </n-layout-content>
        </n-layout>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style>
html,
body,
#app {
  margin: 0;
  height: 100%;
}
.shell {
  height: 100vh;
}
.sider {
  display: flex;
  flex-direction: column;
}
.brand {
  padding: 16px 18px 10px;
}
.brand-title {
  font-size: 16px;
  font-weight: 600;
}
.brand-sub {
  margin-top: 2px;
  font-size: 12px;
  opacity: 0.55;
}
.nav {
  flex: 1;
}
.foot {
  padding: 12px 18px 16px;
  opacity: 0.7;
}
.content {
  padding: 20px 24px 40px;
}
.page-wrap {
  max-width: 1000px;
  margin: 0 auto;
}
.page-title {
  margin: 0 0 4px;
  font-size: 19px;
  font-weight: 600;
}
.page-desc {
  margin: 0 0 16px;
  font-size: 13px;
  opacity: 0.6;
}
.section {
  margin-bottom: 18px;
}
</style>
