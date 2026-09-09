import { describe, expect, it } from "vitest";
import {
  QUOTA_EXCEEDED_CODE,
  applyLeadBonus,
  normalizeQuota,
  parseQuotaErrorEnvelope,
  quotaBadgeLabel,
  quotaPolicy,
  quotaFromQueryResponse,
  shouldForceLeadForm,
} from "@/features/chat/quota";

// Secure-wave spec §5 contract fixtures, byte-faithful to the documented
// shapes so any backend drift fails here first.
const ANON_QUOTA_OK = {
  used_turns: 2,
  remaining_turns: 1,
  cap: 3,
  is_authenticated: false,
  bonus_granted: 0,
};

const AUTH_QUOTA = {
  used_turns: null,
  remaining_turns: null,
  cap: null,
  is_authenticated: true,
  bonus_granted: null,
};

const BODY_429 = {
  ok: false,
  error: {
    code: "ANONYMOUS_QUOTA_EXCEEDED",
    message: "Anh/chị đã dùng hết 3 lượt tư vấn miễn phí. Để lại số điện thoại để nhận tư vấn miễn phí nhé!",
    quota: { used_turns: 3, remaining_turns: 0, cap: 3, is_authenticated: false, bonus_granted: 0 },
  },
  lead_cta: { required: true },
};

const SSE_ERROR_FRAME = {
  code: "ANONYMOUS_QUOTA_EXCEEDED",
  quota: { used_turns: 3, remaining_turns: 0, cap: 3, is_authenticated: false, bonus_granted: 0 },
  lead_cta: { required: true },
};

describe("normalizeQuota", () => {
  it("parses the §5.1 anonymous snapshot", () => {
    expect(normalizeQuota(ANON_QUOTA_OK)).toEqual({
      usedTurns: 2,
      remainingTurns: 1,
      cap: 3,
      isAuthenticated: false,
      bonusGranted: 0,
    });
  });

  it("parses the registered customer shape as finite", () => {
    expect(normalizeQuota({
      used_turns: 2,
      remaining_turns: 3,
      cap: 5,
      is_authenticated: true,
      bonus_granted: 0,
    })).toEqual({
      usedTurns: 2,
      remainingTurns: 3,
      cap: 5,
      isAuthenticated: true,
      bonusGranted: 0,
    });
  });

  it("parses the finite sales shape instead of treating sales as unlimited", () => {
    const sales = normalizeQuota({
      used_turns: 4,
      remaining_turns: 6,
      cap: 10,
      is_authenticated: true,
      bonus_granted: 0,
    });
    expect(sales && quotaPolicy(sales)).toBe("sales");
    expect(sales && quotaBadgeLabel(sales)).toBe("Còn 6 lượt");
  });

  it("parses the admin unlimited shape", () => {
    const admin = normalizeQuota(AUTH_QUOTA);
    expect(admin && quotaPolicy(admin)).toBe("admin");
    expect(admin && quotaBadgeLabel(admin)).toBe("Không giới hạn");
  });

  it("rejects malformed input wholesale (safe default)", () => {
    expect(normalizeQuota(null)).toBeNull();
    expect(normalizeQuota("quota")).toBeNull();
    expect(normalizeQuota({})).toBeNull();
    expect(normalizeQuota({ used_turns: "2" })).toBeNull();
    expect(normalizeQuota({ used_turns: -1 })).toBeNull();
    expect(normalizeQuota({ used_turns: 1.5 })).toBeNull();
    // Missing optional fields degrade to null/0 instead of failing.
    expect(normalizeQuota({ used_turns: 1 })).toEqual({
      usedTurns: 1,
      remainingTurns: null,
      cap: null,
      isAuthenticated: false,
      bonusGranted: 0,
    });
    expect(normalizeQuota({ used_turns: null, remaining_turns: null, cap: null, is_authenticated: true })).toEqual({
      usedTurns: null,
      remainingTurns: null,
      cap: null,
      isAuthenticated: true,
      bonusGranted: 0,
    });
  });

  it("extracts the snapshot from a /query success payload", () => {
    expect(quotaFromQueryResponse({ answer: "…", quota: ANON_QUOTA_OK })).toEqual({
      usedTurns: 2,
      remainingTurns: 1,
      cap: 3,
      isAuthenticated: false,
      bonusGranted: 0,
    });
    expect(quotaFromQueryResponse({ answer: "…" })).toBeNull();
  });
});

describe("parseQuotaErrorEnvelope", () => {
  it("parses the JSON 429 body nested under `error`", () => {
    const info = parseQuotaErrorEnvelope(BODY_429);
    expect(info?.code).toBe(QUOTA_EXCEEDED_CODE);
    expect(info?.message).toContain("hết 3 lượt");
    expect(info?.quota?.remainingTurns).toBe(0);
  });

  it("parses the flat SSE error frame", () => {
    const info = parseQuotaErrorEnvelope(SSE_ERROR_FRAME);
    expect(info?.code).toBe(QUOTA_EXCEEDED_CODE);
    expect(info?.quota?.usedTurns).toBe(3);
  });

  it("returns null for malformed envelopes and keeps other codes intact", () => {
    expect(parseQuotaErrorEnvelope(null)).toBeNull();
    expect(parseQuotaErrorEnvelope("oops")).toBeNull();
    expect(parseQuotaErrorEnvelope({})).toBeNull();
    expect(parseQuotaErrorEnvelope({ error: {} })).toBeNull();
    expect(parseQuotaErrorEnvelope({ error: { code: "PROJECT_SCOPE" } })?.code).toBe(
      "PROJECT_SCOPE"
    );
    // A frame without a quota object still parses, quota stays null.
    expect(parseQuotaErrorEnvelope({ code: QUOTA_EXCEEDED_CODE })).toEqual({
      code: QUOTA_EXCEEDED_CODE,
      message: "",
      quota: null,
    });
  });
});

describe("shouldForceLeadForm", () => {
  const state = (over: Record<string, unknown> = {}) =>
    normalizeQuota({ used_turns: 3, remaining_turns: 0, cap: 3, is_authenticated: false, ...over });

  it("forces only an exhausted anonymous identity", () => {
    expect(shouldForceLeadForm(state())).toBe(true);
    expect(shouldForceLeadForm(state({ used_turns: 2, remaining_turns: 1 }))).toBe(false);
    expect(shouldForceLeadForm(normalizeQuota(AUTH_QUOTA))).toBe(false);
    expect(shouldForceLeadForm(null)).toBe(false);
  });
});

describe("applyLeadBonus (AC4 optimistic update)", () => {
  it("bumps cap, remaining and cumulative bonus by the granted amount", () => {
    const next = applyLeadBonus(normalizeQuota(BODY_429.error.quota), 5);
    expect(next).toEqual({
      usedTurns: 3,
      remainingTurns: 5,
      cap: 8,
      isAuthenticated: false,
      bonusGranted: 5,
    });
  });

  it("keeps null fields null for unlimited principals and no-ops on zero", () => {
    expect(applyLeadBonus(normalizeQuota(AUTH_QUOTA), 5)?.usedTurns).toBeNull();
    expect(applyLeadBonus(normalizeQuota(AUTH_QUOTA), 5)?.remainingTurns).toBeNull();
    expect(applyLeadBonus(normalizeQuota(AUTH_QUOTA), 5)?.cap).toBeNull();
    const before = normalizeQuota(BODY_429.error.quota);
    expect(applyLeadBonus(before, 0)).toEqual(before);
    expect(applyLeadBonus(null, 5)).toBeNull();
  });
});
