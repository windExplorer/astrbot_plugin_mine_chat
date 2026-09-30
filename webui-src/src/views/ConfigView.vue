<script setup lang="ts">
import { onMounted, ref } from "vue";
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

    <n-spin :show="loading">
      <n-card
        v-for="group in groups"
        :key="group.name"
        size="small"
        class="section group-card"
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
