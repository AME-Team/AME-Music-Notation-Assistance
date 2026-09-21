import { describe, expect, it } from "vitest";
import { classifyLogLevel, normalizeLogLevel, shouldLogDidFailLoad } from "./logLevel";

describe("classifyLogLevel (#148)", () => {
  it("treats plain uvicorn output as INFO", () => {
    expect(classifyLogLevel("INFO:     Started server process [22108]")).toBe("INFO");
    expect(classifyLogLevel('INFO:     127.0.0.1:64727 - "GET /health HTTP/1.1" 200 OK')).toBe(
      "INFO",
    );
  });

  it("detects python tracebacks and errors", () => {
    expect(classifyLogLevel('Traceback (most recent call last):\n  File "x.py", line 1')).toBe(
      "ERROR",
    );
    expect(classifyLogLevel("ModuleNotFoundError: No module named 'torch'")).toBe("ERROR");
    expect(classifyLogLevel("CRITICAL: application startup failed")).toBe("ERROR");
    expect(classifyLogLevel("[dsp_main] warning: beatmap.jsonにtime_signaturesが無い")).toBe(
      "WARN",
    );
  });

  it("detects warnings", () => {
    expect(classifyLogLevel("UserWarning: composite durations are not allowed")).toBe("WARN");
    expect(classifyLogLevel("WARNING:  deprecated call")).toBe("WARN");
  });

  it("prefers ERROR over WARN when both appear", () => {
    expect(classifyLogLevel("WARNING: ok\nERROR: boom")).toBe("ERROR");
  });
});

describe("normalizeLogLevel (#148 review)", () => {
  it("accepts valid levels case-insensitively", () => {
    expect(normalizeLogLevel("error")).toBe("ERROR");
    expect(normalizeLogLevel("Warn")).toBe("WARN");
    expect(normalizeLogLevel("INFO")).toBe("INFO");
  });

  it("falls back to ERROR for unknown or missing values", () => {
    // `logger[undefined]`のようなundefined呼び出しでログ自体が落ちないこと。
    expect(normalizeLogLevel(undefined)).toBe("ERROR");
    expect(normalizeLogLevel("fatal")).toBe("ERROR");
    expect(normalizeLogLevel(42)).toBe("ERROR");
  });
});

describe("shouldLogDidFailLoad (#148 review)", () => {
  it("logs main-frame failures", () => {
    expect(shouldLogDidFailLoad({ errorCode: -102, isMainFrame: true })).toBe(true);
  });

  it("ignores sub-frame failures and aborted navigations", () => {
    expect(shouldLogDidFailLoad({ errorCode: -102, isMainFrame: false })).toBe(false);
    expect(shouldLogDidFailLoad({ errorCode: -3, isMainFrame: true })).toBe(false);
  });
});
