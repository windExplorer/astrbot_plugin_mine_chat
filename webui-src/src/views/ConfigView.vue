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

import { apiConfig, apiConfigSave, type ConfigItem } from "../api";

/**
 * 配置分区表（硬编码）。
 *
 * ⚠️ 给 _conf_schema.json 新增顶层键时，必须同步加进下面某个分区的 keys，
 * 否则它会掉进兜底的「其他」分区里。
 */
const GROUP_META: { name: string; description: string; keys: string[] }[] = [
  {
    name: "基础与作用域",
    description: "总开关、人格解析与主动消息的投递窗口。",
    keys: ["enabled", "persona_override", "primary_umo", "window_auto_bind"],
  },
  {
    name: "日程注入",
    description: "每次回复前，要不要告诉模型「角色此刻在做什么」。",
    keys: [
      "inject_enabled",
      "inject_mode",
      "inject_scopes",
      "inject_max_chars",
      "inject_lookback_minutes",
      "inject_lookahead_minutes",
      "inject_include_seed",
    ],
  },
  {
    name: "日程生成",
    description: "每天怎么排这一天的生活：作息、风格、世界观与避重。",
    keys: [
      "schedule_enabled",
      "schedule_time",
      "schedule_sleep_start",
      "schedule_sleep_end",
      "schedule_allow_night_owl",
      "schedule_item_min",
      "schedule_item_max",
      "schedule_avoid_days",
      "schedule_max_retry",
      "schedule_model",
      "schedule_temperature",
      "schedule_style",
      "schedule_world",
      "schedule_character",
      "schedule_forbidden",
    ],
  },
  {
    name: "主动消息",
    description: "什么时候可以主动找你，以及各种节流与免打扰。",
    keys: [
      "proactive_enabled",
      "proactive_interval_seconds",
      "proactive_daily_limit",
      "proactive_min_interval_minutes",
      "proactive_max_unanswered",
      "proactive_quiet_start",
      "proactive_quiet_end",
      "proactive_user_active_cooldown_minutes",
      "proactive_skip_sleeping",
      "proactive_seed_window_minutes",
      "proactive_jitter_minutes",
      "proactive_max_segments",
      "proactive_segment_delay_seconds",
      "proactive_history_messages",
      "proactive_model",
      "proactive_extra_instruction",
    ],
  },
  {
    name: "提示词与高级",
    description: "完全替换内置提示词模板（留空即用内置版本）。",
    keys: ["prompt_plan_override", "prompt_proactive_override", "log_retention"],
  },
];

const message = useMessage();
const loading = ref(false);
const saving = ref(false);
const items = ref<ConfigItem[]>([]);
const draft = ref<Record<string, any>>({});
const initial = ref<Record<string, any>>({});

async function load() {
  loading.value = true;
  try {
    const res = await apiConfig();
    items.value = res.items;
    const values: Record<string, any> = {};
    for (const item of res.items) values[item.key] = item.value;
    draft.value = { ...values };
    initial.value = { ...values };
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

onMounted(load);

function itemOf(key: string): ConfigItem | undefined {
  return items.value.find((entry) => entry.key === key);
}

function groups(): { name: string; description: string; items: ConfigItem[] }[] {
  const used = new Set<string>();
  const result: { name: string; description: string; items: ConfigItem[] }[] = [];
  for (const group of GROUP_META) {
    const picked: ConfigItem[] = [];
    for (const key of group.keys) {
      const item = itemOf(key);
      if (item && !used.has(key)) {
        picked.push(item);
        used.add(key);
      }
    }
    if (picked.length) result.push({ name: group.name, description: group.description, items: picked });
  }
  const leftovers = items.value.filter((item) => !used.has(item.key));
  if (leftovers.length) {
    result.push({ name: "其他", description: "未归入任何分区的配置项。", items: leftovers });
  }
  return result;
}

function optionsOf(item: ConfigItem): { label: string; value: string }[] {
  const options = item.options || [];
  const labels = item.labels || [];
  return options.map((value, index) => ({
    label: labels[index] || value,
    value,
  }));
}

function listOptionsOf(item: ConfigItem): { label: string; value: string }[] {
  const options = item.item_options || [];
  const labels = item.item_labels || [];
  return options.map((value, index) => ({ label: labels[index] || value, value }));
}

async function save() {
  const changed: Record<string, any> = {};
  for (const item of items.value) {
    const now = draft.value[item.key];
    const before = initial.value[item.key];
    if (JSON.stringify(now) !== JSON.stringify(before)) changed[item.key] = now;
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
    <p class="page-desc">改动会写回 AstrBot 插件配置并立即生效（主动消息的开关可在「主动消息」页临时切换）。</p>

    <n-space class="section">
      <n-button size="small" type="primary" :loading="saving" @click="save">保存改动</n-button>
      <n-button size="small" @click="reset">还原</n-button>
      <n-button size="small" quaternary @click="load">重新读取</n-button>
    </n-space>

    <n-alert v-if="!loading && !items.length" type="warning" :bordered="false">
      读取不到配置项，请确认 _conf_schema.json 存在且插件已正常装载。
    </n-alert>

    <n-spin :show="loading">
      <n-card
        v-for="group in groups()"
        :key="group.name"
        size="small"
        :title="group.name"
        class="section"
      >
        <template #header-extra>
          <span class="group-desc">{{ group.description }}</span>
        </template>
        <div v-for="item in group.items" :key="item.key" class="field">
          <div class="field-head">
            <span class="field-title">{{ item.description }}</span>
            <span class="field-key">{{ item.key }}</span>
          </div>
          <div class="field-control">
            <n-switch v-if="item.type === 'bool'" v-model:value="draft[item.key]" size="small" />
            <n-input-number
              v-else-if="item.type === 'int' || item.type === 'float'"
              v-model:value="draft[item.key]"
              size="small"
              style="width: 190px"
              :precision="item.type === 'float' ? 2 : 0"
            />
            <n-select
              v-else-if="item.type === 'list'"
              v-model:value="draft[item.key]"
              size="small"
              multiple
              style="width: 320px"
              :options="listOptionsOf(item)"
            />
            <n-select
              v-else-if="item.options && item.options.length"
              v-model:value="draft[item.key]"
              size="small"
              style="width: 260px"
              :options="optionsOf(item)"
            />
            <n-input
              v-else-if="item.type === 'text'"
              v-model:value="draft[item.key]"
              size="small"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 8 }"
            />
            <n-input v-else v-model:value="draft[item.key]" size="small" />
          </div>
          <div v-if="item.hint" class="field-hint">{{ item.hint }}</div>
        </div>
        <n-empty v-if="!group.items.length" description="该分区没有配置项" size="small" />
      </n-card>
    </n-spin>
  </div>
</template>

<style scoped>
.group-desc {
  font-size: 12px;
  opacity: 0.55;
}
.field {
  padding: 10px 0;
  border-bottom: 1px solid rgba(128, 128, 128, 0.14);
}
.field:last-child {
  border-bottom: none;
}
.field-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.field-title {
  font-size: 14px;
  font-weight: 500;
}
.field-key {
  font-size: 12px;
  opacity: 0.42;
  font-family: monospace;
}
.field-control {
  margin-top: 8px;
  max-width: 640px;
}
.field-hint {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.6;
  opacity: 0.6;
}
</style>
