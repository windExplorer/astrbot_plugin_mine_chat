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

import { apiOverview, apiProactiveNow, reasonText, type LogRow, type OverviewPayload } from "../api";
import { usePoll } from "../usePoll";

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
// 后端主动消息/日程生成异步发生，页面可见时每 15 秒静默刷新
usePoll(load, 15000);

const plan = computed(() => data.value?.plan || ({} as any));
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
              <div class="now-line">{{ plan.current_text || "（没有安排）" }}</div>
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
          </n-card>
        </n-gi>
      </n-grid>

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
  margin-bottom: 10px;
  font-size: 15px;
  font-weight: 500;
  line-height: 1.6;
}
</style>
