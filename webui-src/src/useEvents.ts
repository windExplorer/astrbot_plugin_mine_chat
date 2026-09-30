import { onMounted, onUnmounted } from "vue";

import { apiEvents } from "./api";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * 准实时更新：长轮询后端 /events，任何状态变化（发送主动消息、日程/世界观
 * 生成、裁决日志写入……）挂着的请求立刻返回并触发 onWake 回调。
 *
 * - 空闲时页面只挂一个不占流量的挂起请求，没有任何定时拉取；
 * - 页面不可见时暂停挂请求，切回时立即重建；
 * - 请求失败按 5s 退避重连，恢复后自动继续。
 */
export function useEvents(onWake: () => void | Promise<void>): void {
  let alive = false;
  let since = 0;

  onMounted(() => {
    alive = true;
    void (async () => {
      let retryDelay = 3000;
      while (alive) {
        if (typeof document !== "undefined" && document.hidden) {
          await sleep(2000);
          continue;
        }
        try {
          const res = await apiEvents(since);
          retryDelay = 3000;
          since = res.version ?? since;
          await onWake();
        } catch {
          await sleep(retryDelay);
          retryDelay = Math.min(retryDelay * 2, 15000);
        }
      }
    })();
  });

  onUnmounted(() => {
    alive = false;
  });
}
