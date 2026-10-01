<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  NAlert,
  NButton,
  NCard,
  NEmpty,
  NInput,
  NInputNumber,
  NSelect,
  NSpace,
  NSpin,
  NSwitch,
  useMessage,
} from "naive-ui";

import { apiConfig, apiConfigSave, apiProviders, type ConfigGroup } from "../api";

/**
 * 配置页：完全由后端 `_conf_schema.json` 的分组结构驱动。
 *
 * AstrBot 内置配置页与本页共用同一份 schema——在这里新增配置项只需要改
 * `_conf_schema.json`，不需要再维护前端分区表（v1.0.3 之前的教训）。
 */

const message = useMessage();
const loading = ref(false);
const saving = ref(false);
const groups = ref<ConfigGroup[]>([]);
const draft = ref<Record<string, any>>({});
const initial = ref<Record<string, any>>({});
/** AstrBot 已加载的对话模型（widget=model 的字段用它做下拉）。 */
const providerOptions = ref<{ label: string; value: string }[]>([]);

async function load() {
  loading.value = true;
  try {
    const res = await apiConfig();
    groups.value = res.groups;
    const values: Record<string, any> = {};
    for (const group of res.groups) {
      for (const item of group.items) values[item.path] = item.value;
    }
    draft.value = { ...values };
    initial.value = { ...values };

    const needsProviders = res.groups.some((group) =>
      group.items.some((item) => item.widget === "model"),
    );
    if (needsProviders) {
      try {
        const providers = await apiProviders();
        providerOptions.value = [
          { label: "跟随会话当前模型", value: "" },
          ...providers.items.map((item) => ({
            label: item.label + (item.is_default ? "（AstrBot 默认）" : ""),
            value: item.id,
          })),
        ];
      } catch {
        providerOptions.value = [];
      }
    } else {
      providerOptions.value = [];
    }
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

onMounted(load);

type AnyItem = ConfigGroup["items"][number];

function listOptionsOf(item: AnyItem) {
  const options = item.item_options || [];
  const labels = item.item_labels || [];
  return options.map((value, index) => ({ label: labels[index] || value, value }));
}

function optionsOf(item: AnyItem) {
  const options = item.options || [];
  const labels = item.labels || [];
  return options.map((value, index) => ({ label: labels[index] || value, value }));
}

/** 未保存改动数：悬浮保存条的红点徽标。 */
const dirtyCount = computed(() => {
  let count = 0;
  for (const group of groups.value) {
    for (const item of group.items) {
      if (JSON.stringify(draft.value[item.path]) !== JSON.stringify(initial.value[item.path])) {
        count += 1;
      }
    }
  }
  return count;
});

async function save() {
  const changed: Record<string, any> = {};
  for (const group of groups.value) {
    for (const item of group.items) {
      const now = draft.value[item.path];
      const before = initial.value[item.path];
      if (JSON.stringify(now) !== JSON.stringify(before)) changed[item.path] = now;
    }
  }
  if (!Object.keys(changed).length) {
    message.info("没有改动。");
    return;
  }
  saving.value = true;
  try {
    const res = await apiConfigSave(changed);
    message.success(`已保存 ${res.changed.length} 项。`);
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    saving.value = false;
  }
}

function reset() {
  draft.value = { ...initial.value };
  message.info("已还原为保存前的值。");
}

/** 悬浮分组导航 */
const activeGroup = ref("");

function tintOf(index: number): string {
  // 每个分组一个色相的淡渐变（叠在卡片原底色上，明暗主题都协调）
  const hue = (index * 55 + 208) % 360;
  return `hsla(${hue}, 65%, 55%, 0.10)`;
}

function jumpTo(group: ConfigGroup) {
  activeGroup.value = group.name;
  const el = document.getElementById(`cfg-${group.name}`);
  el?.scrollIntoView({ behavior: "smooth", block: "start" });
}
</script>

<template>
  <div>
    <h2 class="page-title">配置</h2>
    <p class="page-desc">
      与 AstrBot 插件设置页共用同一份配置，两边改动实时互通。主动消息的临时开关在「主动消息」页。
    </p>

    <n-space class="section">
      <n-button size="small" type="primary" :loading="saving" @click="save">保存改动</n-button>
      <n-button size="small" @click="reset">还原</n-button>
      <n-button size="small" quaternary @click="load">重新读取</n-button>
    </n-space>

    <n-alert v-if="!loading && !groups.length" type="warning" :bordered="false">
      读取不到配置项，请确认 _conf_schema.json 存在且插件已正常装载。
    </n-alert>

    <!-- 悬浮保存条：配置分组多、页面长，改到哪里都能随手保存 -->
    <teleport to="body">
      <div class="float-bar">
        <n-badge :value="dirtyCount" :show="dirtyCount > 0" type="warning">
          <n-button
            type="primary"
            size="small"
            :loading="saving"
            :disabled="dirtyCount === 0"
            @click="save"
          >
            保存改动
          </n-button>
        </n-badge>
        <n-button size="tiny" quaternary :disabled="dirtyCount === 0" @click="reset">
          还原
        </n-button>
      </div>

      <!-- 悬浮分组导航：右侧竖排，点击平滑滚动到对应区块 -->
      <nav v-if="groups.length > 1" class="float-nav">
        <button
          v-for="group in groups"
          :key="group.name"
          class="nav-item"
          :class="{ active: activeGroup === group.name }"
          :title="group.description"
          @click="jumpTo(group)"
        >
          {{ group.description }}
        </button>
      </nav>
    </teleport>

    <n-spin :show="loading">
      <n-card
        v-for="(group, gi) in groups"
        :id="`cfg-${group.name}`"
        :key="group.name"
        size="small"
        class="section group-card"
        :style="{ '--tint': tintOf(gi) }"
      >
        <template #header>
          <div class="group-head">
            <span class="group-title">{{ group.description }}</span>
            <span class="group-name">{{ group.name }}</span>
          </div>
        </template>
        <template #header-extra>
          <span class="group-hint">{{ group.hint }}</span>
        </template>

        <div v-for="item in group.items" :key="item.path" class="field">
          <div class="field-head">
            <span class="field-title">{{ item.description }}</span>
            <span class="field-key">{{ item.path }}</span>
          </div>
          <div class="field-control">
            <n-switch v-if="item.type === 'bool'" v-model:value="draft[item.path]" size="small" />
            <n-select
              v-else-if="item.widget === 'model' && providerOptions.length"
              v-model:value="draft[item.path]"
              size="small"
              style="width: 340px"
              filterable
              :options="providerOptions"
              placeholder="跟随会话当前模型"
            />
            <n-input-number
              v-else-if="item.type === 'int' || item.type === 'float'"
              v-model:value="draft[item.path]"
              size="small"
              style="width: 190px"
              :precision="item.type === 'float' ? 2 : 0"
            />
            <n-select
              v-else-if="item.type === 'list'"
              v-model:value="draft[item.path]"
              size="small"
              multiple
              style="width: 320px"
              :options="listOptionsOf(item)"
            />
            <n-select
              v-else-if="item.options && item.options.length"
              v-model:value="draft[item.path]"
              size="small"
              style="width: 260px"
              :options="optionsOf(item)"
            />
            <n-input
              v-else-if="item.type === 'text'"
              v-model:value="draft[item.path]"
              size="small"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 8 }"
            />
            <n-input v-else v-model:value="draft[item.path]" size="small" />
          </div>
          <div v-if="item.hint" class="field-hint">{{ item.hint }}</div>
        </div>
        <n-empty v-if="!group.items.length" description="该分组没有配置项" size="small" />
      </n-card>
    </n-spin>
  </div>
</template>

<style scoped>
.float-bar {
  position: fixed;
  right: 28px;
  bottom: 32px;
  z-index: 1000;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  border-radius: 999px;
  background: rgba(128, 128, 128, 0.14);
  border: 1px solid rgba(128, 128, 128, 0.25);
  backdrop-filter: blur(8px);
  box-shadow: 0 4px 18px rgba(0, 0, 0, 0.18);
}
/* 悬浮分组导航：右侧竖排小标签，窄屏隐藏避免挡内容 */
.float-nav {
  position: fixed;
  right: 18px;
  top: 45%;
  transform: translateY(-50%);
  z-index: 1000;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.nav-item {
  max-width: 96px;
  padding: 5px 10px;
  border: 1px solid rgba(128, 128, 128, 0.22);
  border-radius: 999px;
  background: rgba(128, 128, 128, 0.12);
  backdrop-filter: blur(6px);
  color: inherit;
  font-size: 12px;
  line-height: 1.2;
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
  opacity: 0.75;
  transition: all 0.2s;
}
.nav-item:hover {
  opacity: 1;
  transform: translateX(-3px);
}
.nav-item.active {
  opacity: 1;
  background: rgba(0, 122, 255, 0.18);
  border-color: rgba(0, 122, 255, 0.45);
  font-weight: 600;
}
@media (max-width: 1100px) {
  .float-nav {
    display: none;
  }
}
/* 每个分组一层淡淡的色相渐变，帮助区分区块；底色仍跟随明暗主题 */
.group-card.n-card {
  background: linear-gradient(165deg, var(--tint) 0%, transparent 62%), var(--n-color);
  scroll-margin-top: 12px;
}
.group-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.group-title {
  font-size: 15px;
  font-weight: 600;
}
.group-name {
  font-size: 12px;
  opacity: 0.4;
  font-family: monospace;
}
.group-hint {
  font-size: 12px;
  opacity: 0.55;
  font-weight: 400;
}
.field {
  padding: 12px 0;
  border-bottom: 1px solid rgba(128, 128, 128, 0.12);
}
.field:last-child {
  border-bottom: none;
  padding-bottom: 2px;
}
.field-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.field-title {
  font-size: 14px;
  font-weight: 500;
}
.field-key {
  font-size: 12px;
  opacity: 0.4;
  font-family: monospace;
}
.field-control {
  margin-top: 8px;
}
.field-hint {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.6;
  opacity: 0.6;
}
</style>
