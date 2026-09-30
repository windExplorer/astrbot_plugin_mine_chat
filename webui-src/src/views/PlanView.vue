<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { usePoll } from "../usePoll";
import {
  NAlert,
  NButton,
  NCard,
  NDatePicker,
  NEmpty,
  NInput,
  NModal,
  NSpace,
  NSpin,
  NTag,
  NTimeline,
  NTimelineItem,
  useMessage,
} from "naive-ui";

import { apiPlan, apiPlanRefresh, apiPlanUpdate, type PlanItem, type PlanPayload } from "../api";

const message = useMessage();
const loading = ref(false);
const refreshing = ref(false);
const data = ref<PlanPayload | null>(null);
const selectedDate = ref<number | null>(null);

const editing = ref(false);
const editTarget = ref<PlanItem | null>(null);
const editForm = ref({ activity: "", mood: "", message_seed: "" });

function dateText(ts: number | null): string {
  if (!ts) return "";
  const d = new Date(ts);
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

async function load() {
  loading.value = true;
  try {
    data.value = await apiPlan("", dateText(selectedDate.value));
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

async function refresh() {
  const personaId = data.value?.persona_id || "";
  if (!personaId) {
    message.warning("还没有人格档案，请先在私聊里对角色说一句话。");
    return;
  }
  refreshing.value = true;
  try {
    data.value = await apiPlanRefresh(personaId, dateText(selectedDate.value));
    message.success("已重新生成。");
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    refreshing.value = false;
  }
}

function openEdit(item: PlanItem) {
  editTarget.value = item;
  editForm.value = {
    activity: item.activity || "",
    mood: item.mood || "",
    message_seed: item.message_seed || "",
  };
  editing.value = true;
}

async function submitEdit() {
  if (!editTarget.value) return;
  try {
    await apiPlanUpdate(editTarget.value.id, { ...editForm.value });
    message.success("已保存。");
    editing.value = false;
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

onMounted(load);
// 日程可能在后台被生成/刷新；编辑弹窗打开时跳过轮询，避免打断编辑
usePoll(() => {
  if (!editing.value) return load();
}, 20000);

const nowMinute = computed(() => data.value?.now_minute ?? -1);
const items = computed<PlanItem[]>(() => data.value?.items || []);

const sourceLabel = computed(() => {
  const source = data.value?.source || "";
  if (source === "llm") return "模型生成";
  if (source === "llm_retry") return "模型生成（重试过）";
  if (source === "fallback") return "兜底模板";
  if (source === "manual") return "手动重新生成";
  return source || "未生成";
});

function timelineType(item: PlanItem, index: number): "success" | "info" | "default" {
  if (index === currentIndex.value) return "success";
  return item.message_seed ? "info" : "default";
}

const currentIndex = computed(() =>
  items.value.findIndex((item) => item.start_min <= nowMinute.value && nowMinute.value < item.end_min),
);
</script>

<template>
  <div>
    <h2 class="page-title">日程</h2>
    <p class="page-desc">角色一整天的安排。点任意条目可以直接改写活动、心情与可分享碎片。</p>

    <n-card size="small" class="section">
      <n-space align="center" :wrap="true">
        <n-date-picker v-model:value="selectedDate" type="date" clearable @update:value="load" />
        <n-button size="small" @click="load" :loading="loading">刷新</n-button>
        <n-button size="small" type="primary" @click="refresh" :loading="refreshing">重新生成</n-button>
        <n-tag size="small" :bordered="false">来源：{{ sourceLabel }}</n-tag>
        <n-tag v-if="data?.quality !== null && data?.quality !== undefined" size="small" :bordered="false">
          质量 {{ Math.round(data!.quality!) }}
        </n-tag>
      </n-space>
    </n-card>

    <n-alert v-if="data?.current_text" type="info" :bordered="false" class="section">
      此刻：{{ data.current_text }}
    </n-alert>

    <n-spin :show="loading">
      <n-card size="small" title="时间轴">
        <n-empty v-if="!items.length" description="这一天还没有日程" size="small" />
        <n-timeline v-else>
          <n-timeline-item
            v-for="(item, index) in items"
            :key="item.id"
            :type="timelineType(item, index)"
            :title="`${item.start_text} - ${item.end_text}`"
            :content="item.activity"
          >
            <template #footer>
              <n-space size="small" align="center" :wrap="true">
                <n-tag v-if="item.mood" size="tiny" :bordered="false">{{ item.mood }}</n-tag>
                <n-tag v-if="item.message_seed" size="tiny" type="warning" :bordered="false">
                  碎片：{{ item.message_seed }}
                </n-tag>
                <n-tag v-if="index === currentIndex" size="tiny" type="success" :bordered="false">此刻</n-tag>
                <n-button size="tiny" quaternary @click="openEdit(item)">编辑</n-button>
              </n-space>
            </template>
          </n-timeline-item>
        </n-timeline>
      </n-card>
    </n-spin>

    <n-modal v-model:show="editing" preset="card" style="max-width: 520px" title="编辑日程条目">
      <n-space vertical>
        <div>
          <div class="field-label">活动</div>
          <n-input v-model:value="editForm.activity" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" />
        </div>
        <div>
          <div class="field-label">心情</div>
          <n-input v-model:value="editForm.mood" placeholder="留空表示不写" />
        </div>
        <div>
          <div class="field-label">可分享碎片（主动消息的燃料）</div>
          <n-input
            v-model:value="editForm.message_seed"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 3 }"
            placeholder="留空表示这个时段不适合被打扰"
          />
        </div>
        <n-space justify="end">
          <n-button size="small" @click="editing = false">取消</n-button>
          <n-button size="small" type="primary" @click="submitEdit">保存</n-button>
        </n-space>
      </n-space>
    </n-modal>
  </div>
</template>

<style scoped>
.field-label {
  margin-bottom: 6px;
  font-size: 13px;
  opacity: 0.7;
}
</style>
