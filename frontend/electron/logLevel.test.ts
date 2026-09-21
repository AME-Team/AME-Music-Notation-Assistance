import { describe, expect, it } from "vitest";
import { classifyLogLevel } from "./logLevel";

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
