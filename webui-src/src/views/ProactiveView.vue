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
  NPagination,
  NSelect,
  NSpace,
  NSpin,
  NSwitch,
  NTag,
  useDialog,
  useMessage,
} from "naive-ui";

import {
  apiLogs,
  apiProactiveNow,
  apiProactiveState,
  apiProactiveToggle,
  reasonText,
  type LogRow,
  type ProactiveStatus,
} from "../api";

const message = useMessage();
const dialog = useDialog();

const loading = ref(false);
const triggering = ref(false);
const status = ref<ProactiveStatus | null>(null);
const logs = ref<LogRow[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = 20;
const decisionFilter = ref<string | null>(null);

const decisionOptions = [
  { label: "全部", value: "" },
  { label: "已发送", value: "send" },
  { label: "未发送", value: "skip" },
  { label: "出错", value: "error" },
  { label: "配图", value: "img" },
  { label: "表情", value: "sticker" },
];

async function loadStatus() {
  try {
    status.value = await apiProactiveState();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function loadLogs() {
  loading.value = true;
  try {
    const res = await apiLogs({
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
      decision: decisionFilter.value || undefined,
    });
    logs.value = res.items;
    total.value = res.total;
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

async function toggle(enabled: boolean) {
  const personaId = status.value?.persona_id;
  if (!personaId) return;
  try {
    await apiProactiveToggle(personaId, enabled);
    message.success(enabled ? "已开启主动消息。" : "已关闭主动消息。");
    await loadStatus();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

function confirmNow() {
  const personaId = status.value?.persona_id;
  if (!personaId) return;
  dialog.warning({
    title: "立即主动一次",
    content: "会真的调用模型并往投递窗口发消息（仍受静默时段、睡眠、每日上限等限制）。确定继续？",
    positiveText: "继续",
    negativeText: "取消",
    onPositiveClick: async () => {
      triggering.value = true;
      try {
        const res = await apiProactiveNow(personaId);
        if (res.sent) message.success("已发出。");
        else message.warning(`这次没有发出：${reasonText(res.reason)}`);
        await loadStatus();
        page.value = 1;
        await loadLogs();
      } catch (e: any) {
        message.error(e?.message || String(e));
      } finally {
        triggering.value = false;
      }
    },
  });
}

onMounted(async () => {
  await loadStatus();
  await loadLogs();
});

const decisionTag = (decision: string) => {
  if (decision === "img" || decision === "sticker") {
    const label = decision === "img" ? "配图" : "表情";
    return h(NTag, { size: "small", type: "info", bordered: false }, { default: () => label });
  }
  const type = decision === "send" ? "success" : decision === "error" ? "error" : "warning";
  const label = decision === "send" ? "已发送" : decision === "error" ? "出错" : "未发送";
  return h(NTag, { size: "small", type, bordered: false }, { default: () => label });
};

const columns = [
  { title: "时间", key: "ts_text", width: 140 },
  { title: "结果", key: "decision", width: 90, render: (row: LogRow) => decisionTag(row.decision) },
  { title: "原因", key: "reason", width: 160, render: (row: LogRow) => reasonText(row.reason) },
  { title: "窗口", key: "umo", width: 220, ellipsis: { tooltip: true } },
  { title: "内容", key: "content", ellipsis: { tooltip: true }, render: (row: LogRow) => row.content || "—" },
];

const summary = computed(() => {
  const text = status.value?.last_message || "";
  return text.length > 200 ? text.slice(0, 200) + "…" : text;
});
</script>

<template>
  <div>
    <h2 class="page-title">主动消息</h2>
    <p class="page-desc">什么时候会主动找你、为什么没有发，都在这里。</p>

    <n-card size="small" class="section" title="运行状态">
      <template #header-extra>
        <n-space align="center">
          <n-switch
            :value="status?.enabled ?? false"
            size="small"
            @update:value="toggle"
          />
          <n-button size="tiny" quaternary :loading="triggering" @click="confirmNow">立即一次</n-button>
        </n-space>
      </template>
      <n-empty v-if="!status" description="还没有人格档案" size="small" />
      <template v-else>
        <n-descriptions :column="2" label-placement="left" size="small">
          <n-descriptions-item label="角色">
            {{ status.persona_name || status.persona_id }}
          </n-descriptions-item>
          <n-descriptions-item label="投递窗口">
            {{ status.primary_umo || "（未绑定，先在私聊里说一句话）" }}
          </n-descriptions-item>
          <n-descriptions-item label="下次候选">{{ status.next_at_text }}</n-descriptions-item>
          <n-descriptions-item label="免打扰">{{ status.quiet_hours }}</n-descriptions-item>
          <n-descriptions-item label="今日已发">
            {{ status.sent_today }} / {{ status.daily_limit || "不限" }}
          </n-descriptions-item>
          <n-descriptions-item label="连续未回复">
            {{ status.unanswered }} / {{ status.max_unanswered || "不限" }}
          </n-descriptions-item>
        </n-descriptions>
        <n-alert v-if="summary" type="default" :bordered="false" class="last-msg">
          上一次主动发的是：{{ summary }}
        </n-alert>
      </template>
    </n-card>

    <n-card size="small" title="裁决记录">
      <template #header-extra>
        <n-space align="center">
          <n-select
            v-model:value="decisionFilter"
            :options="decisionOptions"
            size="small"
            style="width: 110px"
            @update:value="() => { page = 1; loadLogs(); }"
          />
          <n-button size="tiny" quaternary @click="loadLogs">刷新</n-button>
        </n-space>
      </template>
      <n-spin :show="loading">
        <n-empty v-if="!logs.length" description="还没有记录" size="small" />
        <template v-else>
          <n-data-table
            :columns="columns"
            :data="logs"
            :bordered="false"
            size="small"
            :row-key="(row: LogRow) => row.id"
          />
          <div class="pager">
            <n-pagination
              v-model:page="page"
              :page-size="pageSize"
              :item-count="total"
              size="small"
              @update:page="loadLogs"
            />
          </div>
        </template>
      </n-spin>
    </n-card>
  </div>
</template>

<style scoped>
.last-msg {
  margin-top: 12px;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
