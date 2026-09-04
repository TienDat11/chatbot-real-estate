import { describe, expect, it } from "vitest";
import {
  ANON_TOKEN_KEY,
  DEVICE_ID_KEY,
  PROJECT_KEY_STORAGE,
  SESSION_KEY,
  SESSION_KEY_TRAINING,
  getAnonToken,
  getDeviceId,
  getSessionId,
  getSessionIdForScope,
  getStoredProjectKey,
  persistAnonToken,
  sessionKeyForScope,
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

// Session-id namespacing (409 root cause): a training turn must never reuse a
// customer conversation's session id in the same tab, and vice versa.
describe("getSessionIdForScope", () => {
  it("customer scope resolves the legacy key, byte-identical to getSessionId", () => {
    const storage = memoryStorage();
    const viaScope = getSessionIdForScope(storage, "customer");
    expect(viaScope).toMatch(UUID_V4);
    expect(storage.data[SESSION_KEY]).toBe(viaScope);
    // getSessionId and the customer scope share the SAME stored id (no migration).
    expect(getSessionId(storage)).toBe(viaScope);
  });

  it("training scope uses a distinct key and never collides with customer", () => {
    const storage = memoryStorage();
    const customer = getSessionIdForScope(storage, "customer");
    const training = getSessionIdForScope(storage, "training");
    expect(training).toMatch(UUID_V4);
    expect(training).not.toBe(customer);
    expect(storage.data[SESSION_KEY]).toBe(customer);
    expect(storage.data[SESSION_KEY_TRAINING]).toBe(training);
  });

  it("reuses the stored id per scope unless forced, and forceNew stays within scope", () => {
    const storage = memoryStorage();
    const first = getSessionIdForScope(storage, "training");
    expect(getSessionIdForScope(storage, "training")).toBe(first);
    const forced = getSessionIdForScope(storage, "training", true);
    expect(forced).not.toBe(first);
    expect(storage.data[SESSION_KEY_TRAINING]).toBe(forced);
    // Forcing a new TRAINING id leaves the customer id untouched.
    expect(storage.data[SESSION_KEY]).toBeUndefined();
  });

  it("sessionKeyForScope maps each scope to its storage key", () => {
    expect(sessionKeyForScope("customer")).toBe(SESSION_KEY);
    expect(sessionKeyForScope("training")).toBe(SESSION_KEY_TRAINING);
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

// Secure wave §6: the server-minted anon token is the durable quota identity.
describe("anon token persistence (secure wave)", () => {
  it("reads null before any token was stored", () => {
    expect(getAnonToken(memoryStorage())).toBeNull();
  });

  it("propagates storage failures so the component can use its memory fallback", () => {
    const storage: StorageLike = {
      getItem: () => {
        throw new Error("storage unavailable");
      },
      setItem: () => {
        throw new Error("storage unavailable");
      },
    };
    expect(() => getAnonToken(storage)).toThrow("storage unavailable");
  });

  it("persists a server-returned token and reads it back across reloads", () => {
    const storage = memoryStorage();
    expect(persistAnonToken(storage, "eyJhbnon.payload.sig")).toBe(true);
    expect(getAnonToken(storage)).toBe("eyJhbnon.payload.sig");
    expect(storage.data[ANON_TOKEN_KEY]).toBe("eyJhbnon.payload.sig");
  });

  it("self-heals by overwriting the stored token with a fresher one", () => {
    const storage = memoryStorage({ [ANON_TOKEN_KEY]: "old.token" });
    persistAnonToken(storage, "fresh.token");
    expect(getAnonToken(storage)).toBe("fresh.token");
  });

  it("ignores malformed payloads so they never clobber a good token", () => {
    const storage = memoryStorage({ [ANON_TOKEN_KEY]: "good.token" });
    expect(persistAnonToken(storage, "")).toBe(false);
    expect(persistAnonToken(storage, 42)).toBe(false);
    expect(persistAnonToken(storage, null)).toBe(false);
    expect(persistAnonToken(storage, undefined)).toBe(false);
    expect(getAnonToken(storage)).toBe("good.token");
  });

  it("leaves the legacy device/session ids untouched", () => {
    const storage = memoryStorage();
    const deviceId = getDeviceId(storage);
    const sessionId = getSessionId(storage);
    persistAnonToken(storage, "tok");
    // Legacy ids keep context-keying duty only; the token lives beside them.
    expect(storage.data[DEVICE_ID_KEY]).toBe(deviceId);
    expect(storage.data[SESSION_KEY]).toBe(sessionId);
  });
});
