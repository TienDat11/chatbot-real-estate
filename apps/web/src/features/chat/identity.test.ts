import { describe, expect, it } from "vitest";
import {
  DEVICE_ID_KEY,
  PROJECT_KEY_STORAGE,
  SESSION_KEY,
  getDeviceId,
  getSessionId,
  getStoredProjectKey,
  storeProjectKey,
  type StorageLike,
} from "@/features/chat/identity";

// Story 10.1-FE: device_id is the persistent anonymous identity, created once
// and kept across visits in localStorage; session_id stays per-tab. The helpers
// are pure over an injected Storage so these contracts are testable in node.
function memoryStorage(initial: Record<string, string> = {}): StorageLike & { data: Record<string, string> } {
  const data: Record<string, string> = { ...initial };
  return {
    data,
    getItem(key: string) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null;
    },
    setItem(key: string, value: string) {
      data[key] = value;
    },
  };
}

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe("getDeviceId", () => {
  it("mints a UUID v4 on the first visit and stores it", () => {
    const storage = memoryStorage();
    const id = getDeviceId(storage);
    expect(id).toMatch(UUID_V4);
    expect(storage.data[DEVICE_ID_KEY]).toBe(id);
  });

  it("keeps the same id across visits (localStorage mock persists)", () => {
    const storage = memoryStorage();
    const firstVisit = getDeviceId(storage);
    // Simulate a reload: the same storage object is still available.
    const secondVisit = getDeviceId(storage);
    expect(secondVisit).toBe(firstVisit);
  });

  it("returns a pre-seeded id without re-minting", () => {
    const seeded = "123e4567-e89b-42d3-a456-426614174000";
    const storage = memoryStorage({ [DEVICE_ID_KEY]: seeded });
    expect(getDeviceId(storage)).toBe(seeded);
  });
});

describe("getSessionId", () => {
  it("is independent from the device id and stays per-tab", () => {
    const storage = memoryStorage();
    const deviceId = getDeviceId(storage);
    const sessionId = getSessionId(storage);
    expect(sessionId).toMatch(UUID_V4);
    expect(sessionId).not.toBe(deviceId);
  });

  it("reuses the stored session id unless a new one is forced", () => {
    const storage = memoryStorage();
    const first = getSessionId(storage);
    expect(getSessionId(storage)).toBe(first);
    const forced = getSessionId(storage, true);
    expect(forced).not.toBe(first);
    expect(storage.data[SESSION_KEY]).toBe(forced);
  });
});

describe("project key persistence (story 10.3)", () => {
  it("stores and reads back the chosen project key", () => {
    const storage = memoryStorage();
    expect(getStoredProjectKey(storage)).toBeNull();
    storeProjectKey(storage, "soleil");
    expect(getStoredProjectKey(storage)).toBe("soleil");
    expect(storage.data[PROJECT_KEY_STORAGE]).toBe("soleil");
  });

  it("returns null when no project was ever chosen", () => {
    expect(getStoredProjectKey(memoryStorage())).toBeNull();
  });
});
