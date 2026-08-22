import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryRequestError, streamQuery } from "@/lib/api";
import {
  loadActiveProjects,
  FALLBACK_ACTIVE_PROJECTS,
  shouldForceProjectPicker,
  sortActiveProjects,
} from "@/features/chat/activeProjects";

// Story 10.3: the backend answers 422 PROJECT_SCOPE when more than one project
// is active and none was chosen. The FE turns that into the ProjectPicker and
// re-sends the pending question with the chosen project_key + persistent
// device_id. These tests pin the seams that make that flow testable without a
// browser (vitest runs in the node environment).
function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const PROJECT_SCOPE_BODY = {
  ok: false,
  error: { code: "PROJECT_SCOPE", message: "Vui lòng chọn dự án (có nhiều dự án đang mở bán)" },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("streamQuery PROJECT_SCOPE detection", () => {
  it("surfaces a QueryRequestError with code PROJECT_SCOPE on 422", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(PROJECT_SCOPE_BODY, 422)));
    const onError = vi.fn();
    await streamQuery({ query: "giá 2PN bao nhiêu?", project_key: "", device_id: "dev-1" }, { onError });
    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0] as QueryRequestError;
    expect(err).toBeInstanceOf(QueryRequestError);
    expect(err.code).toBe("PROJECT_SCOPE");
    expect(err.status).toBe(422);
  });

  it("carries the backend message for the picker hint", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(PROJECT_SCOPE_BODY, 422)));
    const onError = vi.fn();
    await streamQuery({ query: "x", project_key: "" }, { onError });
    expect((onError.mock.calls[0][0] as QueryRequestError).message).toContain("chọn dự án");
  });

  it("keeps the generic message when the error body is not JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("boom", { status: 500 })));
    const onError = vi.fn();
    await streamQuery({ query: "x", project_key: "camellia" }, { onError });
    const err = onError.mock.calls[0][0] as QueryRequestError;
    expect(err.code).toBeNull();
    expect(err.message).toContain("500");
  });
});

describe("query resend carries identity + project scope (story 10.1-FE + 10.3)", () => {
  it("posts device_id and project_key in the request body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({}, 200));
    vi.stubGlobal("fetch", fetchMock);
    await streamQuery(
      { query: "giá bán?", session_id: "sess-1", device_id: "dev-1", project_key: "soleil", history: [] },
      {}
    );
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toContain('"device_id":"dev-1"');
    expect(init.body).toContain('"project_key":"soleil"');
  });

  it("allows an empty project_key before any project is chosen", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({}, 200));
    vi.stubGlobal("fetch", fetchMock);
    await streamQuery({ query: "x", project_key: "" }, {});
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toContain('"project_key":""');
  });
});

describe("loadActiveProjects (picker catalogue)", () => {
  it("prefers a project list carried by the 422 error body, normalized to the contract shape", async () => {
    const fromError = [
      { project_key: "camellia", ten_thuong_mai: "The Camellia" },
      { project_key: "soleil", ten_thuong_mai: "Soleil" },
    ];
    const projects = await loadActiveProjects({ projects: fromError });
    // Legacy 422 rows normalize to the wave-1 contract shape (name first) and
    // sort hot-first, then by display name (no hot flag here -> name order).
    expect(projects).toEqual([
      { project_key: "soleil", name: "Soleil", ten_thuong_mai: "Soleil" },
      { project_key: "camellia", name: "The Camellia", ten_thuong_mai: "The Camellia" },
    ]);
  });

  it("falls back to the endpoint when the 422 body has no projects", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ projects: [{ project_key: "camellia", ten_thuong_mai: "The Camellia" }] }, 200))
    );
    const projects = await loadActiveProjects(undefined);
    expect(projects).toEqual([{ project_key: "camellia", name: "The Camellia", ten_thuong_mai: "The Camellia" }]);
  });

  it("keeps the wave-1 contract fields (name, location, geo, is_hot) from the endpoint", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            projects: [
              {
                project_key: "soleil",
                name: "The Soleil Đà Nẵng",
                location: "Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà, Đà Nẵng",
                lat: 16.0710756,
                lng: 108.2436243,
                is_hot: false,
              },
              {
                project_key: "camellia",
                name: "The Camellia Sơn Trà - Đà Nẵng",
                location: "Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Sơn Trà, Đà Nẵng",
                lat: 16.1052,
                lng: 108.2558,
                is_hot: true,
              },
            ],
          },
          200
        )
      )
    );
    const projects = await loadActiveProjects(undefined);
    // Hot project sorts first; the full contract fields survive normalization.
    expect(projects[0].project_key).toBe("camellia");
    expect(projects[0].is_hot).toBe(true);
    expect(projects[0].location).toContain("Lê Văn Lương");
    expect(projects[0].lat).toBe(16.1052);
    expect(projects[0].lng).toBe(108.2558);
    expect(projects[1].project_key).toBe("soleil");
    expect(projects[1].is_hot).toBe(false);
  });

  it("falls back to the seed-grounded catalogue when no source provides a list", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("not found", { status: 404 })));
    const projects = await loadActiveProjects(undefined);
    expect(projects).toEqual(FALLBACK_ACTIVE_PROJECTS);
    expect(projects.length).toBeGreaterThan(0);
  });

  it("ignores a malformed 422 projects list and keeps falling back", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("not found", { status: 404 })));
    const projects = await loadActiveProjects({ projects: "not-an-array" });
    expect(projects).toEqual(FALLBACK_ACTIVE_PROJECTS);
  });
});

describe("shouldForceProjectPicker (master-plan rule, story 10.1)", () => {
  it("forces the picker when >1 project is active and no explicit choice is stored", () => {
    expect(shouldForceProjectPicker(2, null)).toBe(true);
    expect(shouldForceProjectPicker(3, null)).toBe(true);
  });

  it("does NOT force when the customer already made an explicit choice", () => {
    // ragre.project_key is written only by the picker (a user decision), never
    // by the system, so a stored key skips the gate.
    expect(shouldForceProjectPicker(2, "camellia")).toBe(false);
    expect(shouldForceProjectPicker(2, "soleil")).toBe(false);
  });

  it("does NOT force when at most one project is active (single-project default)", () => {
    expect(shouldForceProjectPicker(1, null)).toBe(false);
    expect(shouldForceProjectPicker(0, null)).toBe(false);
  });
});

describe("sortActiveProjects (hot-first, then display name)", () => {
  const nonHot = (key: string, name: string) => ({ project_key: key, name });
  it("puts is_hot projects before non-hot ones regardless of input order", () => {
    const projects: Array<{ project_key: string; name: string; is_hot?: boolean }> = [
      nonHot("soleil", "The Soleil Đà Nẵng"),
      nonHot("camellia", "The Camellia Sơn Trà - Đà Nẵng"),
    ];
    projects[1].is_hot = true;
    const sorted = sortActiveProjects(projects);
    expect(sorted[0].project_key).toBe("camellia");
    expect(sorted[1].project_key).toBe("soleil");
    // The input array itself is never mutated.
    expect(projects[0].project_key).toBe("soleil");
  });

  it("orders same-hotness projects by display name (vi locale)", () => {
    const sorted = sortActiveProjects([
      nonHot("b", "Bảo An"),
      nonHot("a", "An Phú"),
    ]);
    expect(sorted.map((p) => p.project_key)).toEqual(["a", "b"]);
  });

  it("leaves the fallback catalogue order unchanged (already hot-first)", () => {
    const sorted = sortActiveProjects(FALLBACK_ACTIVE_PROJECTS);
    expect(sorted).toEqual(FALLBACK_ACTIVE_PROJECTS);
  });
});
