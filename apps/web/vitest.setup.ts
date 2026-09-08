// Shared vitest setup. Must load the React 19 antd patch BEFORE any antd
// component renders (same rule as src/components/ChatPage.tsx) and polyfill
// the jsdom APIs antd expects from a real browser. Node-environment tests
// skip both because there is no DOM to patch.
import "@testing-library/jest-dom/vitest";

export {};

if (typeof window !== "undefined") {
  await import("@ant-design/v5-patch-for-react-19");

  // RTL waitFor defaults to 1000ms, which starves under full-suite CPU
  // contention (async fetch + antd render chains). Raise the async-util
  // budget only; every assertion still has to become true.
  const { configure } = await import("@testing-library/dom");
  configure({ asyncUtilTimeout: 5000 });

  if (!window.matchMedia) {
    window.matchMedia = (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    });
  }
}
