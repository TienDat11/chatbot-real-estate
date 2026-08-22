import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryRequestError, streamQuery } from "@/lib/api";
import { loadActiveProjects, FALLBACK_ACTIVE_PROJECTS } from "@/features/chat/activeProjects";

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
  it("prefers a project list carried by the 422 error body", async () => {
    const fromError = [
      { project_key: "camellia", ten_thuong_mai: "The Camellia" },
      { project_key: "soleil", ten_thuong_mai: "Soleil" },
    ];
    const projects = await loadActiveProjects({ projects: fromError });
    expect(projects).toEqual(fromError);
  });

  it("falls back to the endpoint when the 422 body has no projects", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ projects: [{ project_key: "camellia", ten_thuong_mai: "The Camellia" }] }, 200))
    );
    const projects = await loadActiveProjects(undefined);
    expect(projects).toEqual([{ project_key: "camellia", ten_thuong_mai: "The Camellia" }]);
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
