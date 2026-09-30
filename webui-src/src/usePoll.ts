import { onMounted, onUnmounted } from "vue";

/**
 * 页面可见时的定时轮询刷新；切走/最小化自动暂停。
 *
 * 背景：主动消息发送、日程/世界观生成都在后端异步发生，用户停在页面时不该
 * 要求手动刷新。各视图传入自己的 load 函数即可；有编辑状态的视图自行加守卫
 * （如 PlanView 在编辑弹窗打开时跳过刷新）。
 */
export function usePoll(fn: () => void | Promise<void>, intervalMs = 15000): void {
  let timer: number | undefined;
  const tick = () => {
    if (typeof document !== "undefined" && document.hidden) return;
    void fn();
  };
  onMounted(() => {
    timer = window.setInterval(tick, intervalMs);
  });
  onUnmounted(() => {
    if (timer !== undefined) window.clearInterval(timer);
    timer = undefined;
  });
}
