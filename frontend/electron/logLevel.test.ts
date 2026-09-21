import { describe, expect, it } from "vitest";
import { classifyLogLevel, escapeLogNewlines, shouldLogDidFailLoad } from "./logLevel";

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

describe("shouldLogDidFailLoad (#149 review)", () => {
  it("logs main-frame failures", () => {
    expect(shouldLogDidFailLoad({ errorCode: -102, isMainFrame: true })).toBe(true);
  });

  it("ignores sub-frame failures and aborted navigations", () => {
    // 通常動作でも出るもの(サブフレーム・リダイレクト/遷移中断)は記録しない。
    expect(shouldLogDidFailLoad({ errorCode: -102, isMainFrame: false })).toBe(false);
    expect(shouldLogDidFailLoad({ errorCode: -3, isMainFrame: true })).toBe(false);
  });
});

describe("escapeLogNewlines (#149 review)", () => {
  it("keeps one log entry per line", () => {
    // 偽のログ行(`[ERROR] [backend] ...`)の注入を防ぐ。
    expect(escapeLogNewlines("ok\n[ERROR] [backend] fake")).toBe("ok\\n[ERROR] [backend] fake");
    expect(escapeLogNewlines("a\r\nb\rc")).toBe("a\\nb\\nc");
  });

  it("leaves single-line text untouched", () => {
    expect(escapeLogNewlines("Uncaught Error: boom")).toBe("Uncaught Error: boom");
  });
});
