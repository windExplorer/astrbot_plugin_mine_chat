<script setup lang="ts">
import { h, onMounted, ref } from "vue";
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NInput,
  NSelect,
  NSpace,
  NSpin,
  NSwitch,
  NTag,
  useDialog,
  useMessage,
} from "naive-ui";

import {
  apiBindingDelete,
  apiBindingPrimary,
  apiBindingSave,
  apiBindings,
  apiPersonaSave,
  apiPersonas,
  type BindingRow,
  type PersonaStored,
} from "../api";

const message = useMessage();
const dialog = useDialog();

const loading = ref(false);
const stored = ref<PersonaStored[]>([]);
const available = ref<{ persona_id: string; system_prompt: string }[]>([]);
const overrideId = ref("");
const bindings = ref<BindingRow[]>([]);

const newUmo = ref("");
const newPersona = ref<string | null>(null);
const newPrimary = ref(false);

async function load() {
  loading.value = true;
  try {
    const [personaRes, bindingRes] = await Promise.all([apiPersonas(), apiBindings()]);
    stored.value = personaRes.stored;
    available.value = personaRes.available;
    overrideId.value = personaRes.persona_override;
    bindings.value = bindingRes.items;
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

onMounted(load);

async function togglePersona(personaId: string, enabled: boolean) {
  try {
    await apiPersonaSave(personaId, enabled);
    message.success(enabled ? "已启用该人格。" : "已停用该人格。");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function addBinding() {
  if (!newUmo.value.trim() || !newPersona.value) {
    message.warning("请同时填写窗口 umo 与人格。");
    return;
  }
  try {
    await apiBindingSave({
      umo: newUmo.value.trim(),
      persona_id: newPersona.value,
      is_primary: newPrimary.value,
      enabled: true,
    });
    message.success("已保存绑定。");
    newUmo.value = "";
    newPrimary.value = false;
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function setPrimary(row: BindingRow) {
  try {
    await apiBindingPrimary(row.persona_id, row.umo);
    message.success("已设为主窗口（主动消息只发往这里）。");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

function removeBinding(row: BindingRow) {
  dialog.warning({
    title: "删除窗口绑定",
    content: `确定删除 ${row.umo} 的绑定？该窗口之后将不再共享这份日程。`,
    positiveText: "删除",
    negativeText: "取消",
    onPositiveClick: async () => {
      try {
        await apiBindingDelete(row.umo);
        message.success("已删除。");
        await load();
      } catch (e: any) {
        message.error(e?.message || String(e));
      }
    },
  });
}

const personaOptions = () =>
  available.value.map((item) => ({ label: item.persona_id, value: item.persona_id }));

const personaColumns = [
  { title: "人格", key: "persona_id" },
  { title: "展示名", key: "persona_name" },
  {
    title: "启用",
    key: "enabled",
    width: 90,
    render: (row: PersonaStored) =>
      h(NSwitch, {
        value: row.enabled,
        size: "small",
        "onUpdate:value": (value: boolean) => togglePersona(row.persona_id, value),
      }),
  },
];

const bindingColumns = [
  { title: "窗口 (umo)", key: "umo", ellipsis: { tooltip: true } },
  { title: "人格", key: "persona_id", width: 150 },
  {
    title: "类型",
    key: "kind",
    width: 80,
    render: (row: BindingRow) =>
      h(
        NTag,
        { size: "small", type: row.kind === "group" ? "info" : "default", bordered: false },
        { default: () => (row.kind === "group" ? "群聊" : "私聊") },
      ),
  },
  {
    title: "投递目标",
    key: "is_primary",
    width: 100,
    render: (row: BindingRow) =>
      row.is_primary
        ? h(NTag, { size: "small", type: "success", bordered: false }, { default: () => "是" })
        : h(
            NButton,
            { size: "tiny", quaternary: true, onClick: () => setPrimary(row) },
            { default: () => "设为主窗口" },
          ),
  },
  {
    title: "操作",
    key: "actions",
    width: 80,
    render: (row: BindingRow) =>
      h(NButton, { size: "tiny", quaternary: true, type: "error", onClick: () => removeBinding(row) }, {
        default: () => "删除",
      }),
  },
];
</script>

<template>
  <div>
    <h2 class="page-title">人格与窗口</h2>
    <p class="page-desc">
      日程与主动状态按「人格」归档：同一人格的私聊与群聊窗口共享同一份生活，主动消息只发往主窗口。
    </p>

    <n-alert type="info" :bordered="false" class="section">
      当前人格解析顺序：插件强制指定（{{ overrideId || "未设置" }}）→ 窗口绑定人格 → AstrBot 会话规则 → 默认人格。
      窗口绑定会在用户说话时自动登记，也可以在这里手工维护。
    </n-alert>

    <n-spin :show="loading">
      <n-card size="small" title="人格档案" class="section">
        <n-empty v-if="!stored.length" description="还没有人格档案（用户在任一窗口说一句话即可自动登记）" size="small" />
        <n-data-table
          v-else
          :columns="personaColumns"
          :data="stored"
          :bordered="false"
          size="small"
          :row-key="(row: PersonaStored) => row.persona_id"
        />
      </n-card>

      <n-card size="small" title="窗口绑定" class="section">
        <n-space class="section" :wrap="true" align="flex-end">
          <div>
            <div class="field-label">窗口 umo</div>
            <n-input
              v-model:value="newUmo"
              size="small"
              style="width: 340px"
              placeholder="aiocqhttp:FriendMessage:10001"
            />
          </div>
          <div>
            <div class="field-label">人格</div>
            <n-select
              v-model:value="newPersona"
              size="small"
              style="width: 180px"
              :options="personaOptions()"
              placeholder="选择人格"
            />
          </div>
          <n-space align="center" size="small">
            <n-switch v-model:value="newPrimary" size="small" />
            <span class="hint-text">设为主窗口</span>
          </n-space>
          <n-button size="small" type="primary" @click="addBinding">添加 / 更新</n-button>
        </n-space>

        <n-empty v-if="!bindings.length" description="还没有窗口绑定" size="small" />
        <n-data-table
          v-else
          :columns="bindingColumns"
          :data="bindings"
          :bordered="false"
          size="small"
          :row-key="(row: BindingRow) => row.umo"
        />
      </n-card>
    </n-spin>
  </div>
</template>

<style scoped>
.field-label {
  margin-bottom: 6px;
  font-size: 13px;
  opacity: 0.7;
}
.hint-text {
  font-size: 13px;
  opacity: 0.75;
}
</style>
