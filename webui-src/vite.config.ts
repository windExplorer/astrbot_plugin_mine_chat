import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import cssInjectedByJs from "vite-plugin-css-injected-by-js";
import { fileURLToPath, URL } from "node:url";

// AstrBot 插件页面以静态资源方式从 pages/schedule-console/ 提供。
// 关键约束（踩过的坑，勿改）：
//  1) base 必须是 './'：AstrBot 会重写页面内相对资源引用并追加 asset_token；
//  2) 必须单文件产物（inlineDynamicImports）：跨 chunk import 会被 token 重写搞成 401 白屏；
//  3) cssCodeSplit=false + cssInjectedByJs：只加载一个 js，样式内联进去；
//  4) hash 路由：AstrBot 静态资源按真实文件路径解析，history 模式刷新会 404；
//  5) assetsInlineLimit=300KB：运行期才注入的图片走不了 token 重写，小图必须内联成 data URI。
export default defineConfig({
  plugins: [vue(), cssInjectedByJs()],
  base: "./",
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "../pages/schedule-console",
    emptyOutDir: true,
    chunkSizeWarningLimit: 5000,
    cssCodeSplit: false,
    assetsInlineLimit: 300 * 1024,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
  server: {
    port: 5176,
    proxy: {},
  },
});
