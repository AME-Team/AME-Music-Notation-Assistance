import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getAgentReport } from "./client";

describe("getAgentReport", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("fetches report.md content for a run_id", async () => {
    const mockReport = {
      run_id: "run_test_01",
      content: "# AI 成果報告\n- 異名同音修正完了",
    };

    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => mockReport,
    } as Response);

    const result = await getAgentReport("run_test_01");
    expect(result).toEqual(mockReport);
    expect(global.fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/agent/runs/run_test_01/report",
      expect.objectContaining({
        headers: expect.any(Headers),
      }),
    );
  });

  it("throws error when report endpoint returns 404", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      statusText: "Not Found",
      text: async () => "report.md not found",
    } as Response);

    await expect(getAgentReport("run_non_existent")).rejects.toThrow("404: report.md not found");
  });
});
