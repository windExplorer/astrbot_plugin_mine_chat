<script setup lang="ts">
import { computed, h, onMounted, ref } from "vue";
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NDescriptions,
  NDescriptionsItem,
  NEmpty,
  NGrid,
  NGi,
  NSpin,
  NTag,
  useMessage,
} from "naive-ui";

import { apiOverview, apiProactiveNow, reasonText, type LogRow, type ModelStatus, type OverviewPayload } from "../api";
import { useEvents } from "../useEvents";

const message = useMessage();
const loading = ref(false);
const triggering = ref(false);
const data = ref<OverviewPayload | null>(null);

async function load() {
  loading.value = true;
  try {
    data.value = await apiOverview();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

async function triggerNow() {
  const personaId = data.value?.persona_id || "";
  if (!personaId) return;
  triggering.value = true;
  try {
    const res = await apiProactiveNow(personaId);
    if (res.sent) message.success("已发出。");
    else message.warning(`这次没有发出：${reasonText(res.reason)}`);
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    triggering.value = false;
  }
}

onMounted(load);
// 后端主动消息/日程生成异步发生：长轮询事件流，变化即刻刷新
useEvents(load);

const plan = computed(() => data.value?.plan || ({} as any));

/** 模型来源标签：让"用的不是我设置的"能当场核对。 */
const modelSourceTag = (m?: ModelStatus) => {
  if (!m) return { type: "default" as const, label: "未知" };
  if (m.source === "explicit") return { type: "success" as const, label: "配置指定" };
  if (m.source === "system") return { type: "info" as const, label: "跟随系统" };
  return { type: "error" as const, label: "无可用模型" };
};
const modelLabel = (m?: ModelStatus) => {
  if (!m || !m.provider_id) return "—";
  return m.model ? `${m.provider_id} · ${m.model}` : m.provider_id;
};
const systemModel = computed(() => data.value?.model_status?.system);
const scheduleModel = computed(() => data.value?.model_status?.schedule);
const proactiveModel = computed(() => data.value?.model_status?.proactive);

/** 第一个被拦截的闸门 = 现在没发的原因 */
const blockedGate = computed(() => {
  const gates = proactive.value?.gates || [];
  return gates.find((g: any) => g.blocked) || null;
});
const proactive = computed(() => data.value?.proactive || ({} as any));
const hasPlan = computed(() => Array.isArray(plan.value?.items) && plan.value.items.length > 0);

const decisionTag = (decision: string) => {
  if (decision === "img" || decision === "sticker") {
    const label = decision === "img" ? "配图" : "表情";
    return h(NTag, { size: "small", type: "info", bordered: false }, { default: () => label });
  }
  const type = decision === "send" ? "success" : decision === "error" ? "error" : "warning";
  const label = decision === "send" ? "已发送" : decision === "error" ? "出错" : "未发送";
  return h(NTag, { size: "small", type, bordered: false }, { default: () => label });
};

const logColumns = [
  { title: "时间", key: "ts_text", width: 130 },
  { title: "结果", key: "decision", width: 90, render: (row: LogRow) => decisionTag(row.decision) },
  {
    title: "原因",
    key: "reason",
    width: 150,
    render: (row: LogRow) => reasonText(row.reason),
  },
  {
    title: "内容",
    key: "content",
    ellipsis: { tooltip: true },
    render: (row: LogRow) => row.content || "—",
  },
];
</script>

<template>
  <div>
    <h2 class="page-title">总览</h2>
    <p class="page-desc">角色现在在做什么、下一次什么时候会主动找你。</p>

    <n-alert v-if="data && !data.enabled" type="warning" class="section" :bordered="false">
      插件当前处于关闭状态（配置项「启用插件」为关）。
    </n-alert>
    <n-alert
      v-if="data && data.enabled && data.configured === false"
      type="warning"
      class="section"
      :bordered="false"
    >
      插件尚未启用：{{ data.setup_hint }}
    </n-alert>

    <n-spin :show="loading">
      <n-grid :cols="2" :x-gap="16" :y-gap="16" class="section" responsive="screen" item-responsive>
        <n-gi span="2 1000:1">
          <n-card size="small" title="此刻的生活">
            <template #header-extra>
              <n-button size="tiny" quaternary :disabled="!hasPlan" @click="triggerNow" :loading="triggering">
                立即主动
              </n-button>
            </template>
            <n-empty v-if="!hasPlan" description="今天还没有日程" size="small" />
            <template v-else>
              <div v-if="plan.now?.current" class="now-line">
                <n-tag size="small" type="success" :bordered="false">现在</n-tag>
                <n-tag size="small" :bordered="false">{{ plan.now.current.time_text }}</n-tag>
                <span class="now-activity">{{ plan.now.current.activity }}</span>
                <n-tag v-if="plan.now.current.mood" size="tiny" type="info" :bordered="false">
                  {{ plan.now.current.mood }}
                </n-tag>
              </div>
              <div v-for="u in plan.now?.upcoming || []" :key="u.time_text" class="now-line next-line">
                <n-tag size="small" type="warning" :bordered="false">之后</n-tag>
                <n-tag size="small" :bordered="false">{{ u.time_text }}</n-tag>
                <span class="now-activity">{{ u.activity }}</span>
                <n-tag v-if="u.mood" size="tiny" type="info" :bordered="false">{{ u.mood }}</n-tag>
              </div>
              <div v-if="!plan.now?.current" class="now-line">
                <n-tag size="small" :bordered="false">此刻</n-tag>
                <span class="now-activity">（现在没有安排）</span>
              </div>
              <n-descriptions :column="1" label-placement="left" size="small">
                <n-descriptions-item label="日期">{{ plan.plan_date }}</n-descriptions-item>
                <n-descriptions-item label="角色">
                  {{ data?.persona_name || data?.persona_id || "—" }}
                </n-descriptions-item>
                <n-descriptions-item label="来源">
                  {{ plan.source || "—" }}
                  <span v-if="plan.quality !== null && plan.quality !== undefined">
                    （质量 {{ Math.round(plan.quality) }}）
                  </span>
                </n-descriptions-item>
                <n-descriptions-item label="条目数">{{ plan.items?.length || 0 }}</n-descriptions-item>
              </n-descriptions>
            </template>
          </n-card>
        </n-gi>

        <n-gi span="2 1000:1">
          <n-card size="small" title="主动消息">
            <n-descriptions :column="1" label-placement="left" size="small">
              <n-descriptions-item label="状态">
                <n-tag size="small" :type="proactive.enabled ? 'success' : 'default'" :bordered="false">
                  {{ proactive.enabled ? "开启" : "关闭" }}
                </n-tag>
              </n-descriptions-item>
              <n-descriptions-item label="投递窗口">
                {{ proactive.primary_umo || "（未绑定，先在私聊里说一句话）" }}
              </n-descriptions-item>
              <n-descriptions-item label="下次候选">
                {{ proactive.next_at_text || "—" }}
              </n-descriptions-item>
              <n-descriptions-item label="今日已发">
                {{ proactive.sent_today ?? 0 }} / {{ proactive.daily_limit || "不限" }}
              </n-descriptions-item>
              <n-descriptions-item label="连续未回复">
                {{ proactive.unanswered ?? 0 }} / {{ proactive.max_unanswered || "不限" }}
              </n-descriptions-item>
              <n-descriptions-item label="免打扰">{{ proactive.quiet_hours || "—" }}</n-descriptions-item>
            </n-descriptions>
            <template v-if="proactive.gates?.length">
              <n-alert
                v-if="blockedGate"
                type="warning"
                :bordered="false"
                style="margin-top: 8px"
              >
                现在没发的原因：{{ blockedGate.name }}{{ blockedGate.detail ? "（" + blockedGate.detail + "）" : "" }}
              </n-alert>
              <div class="gate-list">
                <div v-for="gate in proactive.gates" :key="gate.key" class="gate-row">
                  <n-tag size="small" :type="gate.blocked ? 'error' : 'success'" :bordered="false">
                    {{ gate.blocked ? "拦截" : "通过" }}
                  </n-tag>
                  <span class="gate-name">{{ gate.name }}</span>
                  <span v-if="gate.detail" class="gate-detail">{{ gate.detail }}</span>
                </div>
              </div>
            </template>
          </n-card>
        </n-gi>
      </n-grid>

      <n-card size="small" title="对话模型" class="section">
        <p class="model-hint">
          「系统默认」是 AstrBot 系统配置面板里选的全局模型；下面两行是插件<strong>实际调用</strong>的模型。
          两者不同通常是因为该会话单独设了模型（或落在了别的配置档案），不是插件选错。
          想让本插件固定用一个模型，到「配置 → 模型 → 默认模型」选即可。
        </p>
        <n-descriptions :column="1" label-placement="left" size="small">
          <n-descriptions-item label="系统默认（面板）">
            <n-tag size="small" type="default" :bordered="false">全局</n-tag>
            <span class="model-value">{{ modelLabel(systemModel) }}</span>
          </n-descriptions-item>
          <n-descriptions-item label="日程生成">
            <n-tag size="small" :type="modelSourceTag(scheduleModel).type" :bordered="false">
              {{ modelSourceTag(scheduleModel).label }}
            </n-tag>
            <span class="model-value">{{ modelLabel(scheduleModel) }}</span>
          </n-descriptions-item>
          <n-descriptions-item label="主动消息">
            <n-tag size="small" :type="modelSourceTag(proactiveModel).type" :bordered="false">
              {{ modelSourceTag(proactiveModel).label }}
            </n-tag>
            <span class="model-value">{{ modelLabel(proactiveModel) }}</span>
          </n-descriptions-item>
        </n-descriptions>
      </n-card>

      <n-card size="small" title="最近裁决" class="section">
        <template #header-extra>
          <n-button size="tiny" quaternary @click="load">刷新</n-button>
        </template>
        <n-empty v-if="!data?.recent_logs?.length" description="还没有记录" size="small" />
        <n-data-table
          v-else
          :columns="logColumns"
          :data="data.recent_logs"
          :bordered="false"
          size="small"
          :row-key="(row: LogRow) => row.id"
        />
      </n-card>
    </n-spin>
  </div>
</template>

<style scoped>
.now-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
  font-size: 14px;
  line-height: 1.6;
}
.now-activity {
  flex: 1;
  min-width: 0;
}
.next-line {
  opacity: 0.65;
  font-size: 13px;
  margin-bottom: 6px;
}
.gate-list {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.gate-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.gate-name {
  font-weight: 500;
}
.gate-detail {
  opacity: 0.6;
}
.model-hint {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.6;
  opacity: 0.7;
}
.model-value {
  margin-left: 8px;
  font-size: 13px;
}
</style>
