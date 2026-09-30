<template>
  <div>
    <h2 class="page-title">世界观设定</h2>
    <p class="page-desc">
      日程与主动消息的「人设地基」：手动设定优先；留空时自动从人格提示词提炼，
      绑定知识库后提炼会以知识库资料为准。
    </p>

    <n-spin :show="loading">
      <n-card size="small" class="section" title="手动设定（最高优先级）">
        <template #header-extra>
          <n-button size="tiny" type="primary" :loading="saving" :disabled="!dirty" @click="saveManual">
            保存
          </n-button>
        </template>
        <n-space vertical size="large">
          <div>
            <div class="field-label">世界观（时代 / 地点 / 世界规则 / 重要他人与环境）</div>
            <n-input
              v-model:value="manualWorld"
              type="textarea"
              placeholder="例：近未来的海滨城市，AI 管家已普及，主角住在灯塔旁的小公寓。"
              :autosize="{ minRows: 3, maxRows: 8 }"
            />
          </div>
          <div>
            <div class="field-label">角色补充设定（性格 / 作息偏好 / 兴趣爱好 / 忌讳）</div>
            <n-input
              v-model:value="manualCharacter"
              type="textarea"
              placeholder="例：内向但话痨，晚上灵感多常熬夜画画，早上赖床，讨厌香菜。"
              :autosize="{ minRows: 3, maxRows: 8 }"
            />
          </div>
          <n-text depth="3" style="font-size: 12px">
            两项都留空时才走自动提取；手动内容会覆盖自动提取结果。
          </n-text>
        </n-space>
      </n-card>

      <n-card size="small" class="section" title="自动提取（从人格提示词 + 知识库）">
        <template #header-extra>
          <n-button size="tiny" :loading="rebuilding" @click="rebuild">
            {{ auto ? "重新提取" : "立即提取" }}
          </n-button>
        </template>
        <n-empty v-if="!auto" description="还没有自动提取结果（生成日程时也会自动进行）" size="small" />
        <template v-else>
          <n-descriptions :column="1" label-placement="left" size="small">
            <n-descriptions-item label="世界观">{{ auto.world || "（无）" }}</n-descriptions-item>
            <n-descriptions-item label="角色补充设定">{{ auto.character || "（无）" }}</n-descriptions-item>
            <n-descriptions-item label="提取时间">{{ tsText(auto.ts) }}</n-descriptions-item>
          </n-descriptions>
          <n-text depth="3" style="font-size: 12px; display: block; margin-top: 8px">
            自动结果缓存 7 天后随下次日程生成自动刷新；修改人格提示词 / 知识库后可点「重新提取」立即生效。
          </n-text>
        </template>
      </n-card>

      <n-card size="small" class="section" title="知识库绑定（作用于该人格）">
        <n-space vertical size="large">
          <n-select
            v-model:value="kbSelected"
            :options="kbOptions"
            placeholder="选择要绑定的知识库"
            clearable
            size="small"
          />
          <n-text depth="3" style="font-size: 12px">
            绑定后，自动提取世界观时会用「人格提示词」作为查询检索该知识库，
            检索到的资料优先于人格提示词；日程本身不受影响。
          </n-text>
          <n-space>
            <n-button size="small" type="primary" :disabled="kbSelected === (data?.kb_name ?? '')" :loading="kbSaving" @click="saveKb">
              保存绑定
            </n-button>
          </n-space>
        </n-space>
      </n-card>
    </n-spin>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useMessage } from "naive-ui";
import {
  NButton,
  NCard,
  NDescriptions,
  NDescriptionsItem,
  NEmpty,
  NInput,
  NSelect,
  NSpace,
  NSpin,
  NText,
} from "naive-ui";
import {
  apiWorld,
  apiWorldKbSave,
  apiWorldRebuild,
  apiWorldSave,
  type WorldPayload,
} from "../api";
import { usePoll } from "../usePoll";

const message = useMessage();

const loading = ref(false);
const saving = ref(false);
const rebuilding = ref(false);
const kbSaving = ref(false);

const data = ref<WorldPayload | null>(null);
const manualWorld = ref("");
const manualCharacter = ref("");
const kbSelected = ref<string | null>(null);

const kbOptions = computed(() => [
  { label: "（不绑定）", value: "" },
  ...(data.value?.kb_options || []).map((name) => ({ label: name, value: name })),
]);

const dirty = computed(
  () =>
    manualWorld.value !== (data.value?.manual_world ?? "") ||
    manualCharacter.value !== (data.value?.manual_character ?? ""),
);

const auto = computed(() => data.value?.auto || null);

function tsText(ts: number): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function load() {
  loading.value = true;
  try {
    data.value = await apiWorld();
    // 手动输入只在首次加载时填入，轮询刷新不覆盖正在编辑的内容
    if (!dirty.value) {
      manualWorld.value = data.value?.manual_world ?? "";
      manualCharacter.value = data.value?.manual_character ?? "";
    }
    kbSelected.value = data.value?.kb_name || "";
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

/** 轮询只刷新自动提取结果与知识库候选，不打扰输入 */
async function loadAuto() {
  try {
    const fresh = await apiWorld();
    data.value = fresh;
    kbSelected.value = fresh.kb_name || "";
  } catch {
    /* 静默：轮询失败不打扰 */
  }
}

async function saveManual() {
  saving.value = true;
  try {
    await apiWorldSave(manualWorld.value.trim(), manualCharacter.value.trim());
    message.success("已保存手动设定。");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    saving.value = false;
  }
}

async function saveKb() {
  kbSaving.value = true;
  try {
    await apiWorldKbSave(kbSelected.value || "");
    message.success(kbSelected.value ? `已绑定知识库：${kbSelected.value}` : "已解绑知识库。");
    await loadAuto();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    kbSaving.value = false;
  }
}

async function rebuild() {
  rebuilding.value = true;
  try {
    const res = await apiWorldRebuild();
    if (res.world || res.character) {
      message.success("已重新提炼世界观/角色设定。");
    } else {
      message.warning("提炼结果为空：人格提示词可能不含设定信息，建议手动填写。");
    }
    await loadAuto();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    rebuilding.value = false;
  }
}

onMounted(load);
usePoll(loadAuto, 20000);
</script>

<style scoped>
.field-label {
  font-size: 13px;
  margin-bottom: 4px;
}
</style>
