// Shared vitest setup. Must load the React 19 antd patch BEFORE any antd
// component renders (same rule as src/components/ChatPage.tsx) and polyfill
// the jsdom APIs antd expects from a real browser. Node-environment tests
// skip both because there is no DOM to patch.
export {};

if (typeof window !== "undefined") {
  await import("@ant-design/v5-patch-for-react-19");

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
