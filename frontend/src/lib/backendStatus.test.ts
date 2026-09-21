import { describe, expect, it } from "vitest";
import {
  type BackendStatusSnapshot,
  mergeBackendStatus,
  watchBackendStatus,
} from "./backendStatus";
import type { BackendStatus } from "./electron-api";

/** 状態遷移を手動で発火できる偽のpreload API(#146)。 */
function fakeApi() {
  let listener: ((status: BackendStatus, detail?: string) => void) | null = null;
  let current: BackendStatusSnapshot = { status: "starting" };
  return {
    api: {
      getBackendStatus: async () => current,
      onBackendStatus: (cb: (status: BackendStatus, detail?: string) => void) => {
        listener = cb;
        return () => {
          listener = null;
        };
      },
    },
    setCurrent(snapshot: BackendStatusSnapshot) {
      current = snapshot;
    },
    push(status: BackendStatus, detail?: string) {
      listener?.(status, detail);
    },
    isSubscribed: () => listener !== null,
  };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("mergeBackendStatus (#146)", () => {
  it("accepts forward transitions", () => {
    expect(mergeBackendStatus({ status: "starting" }, { status: "ready" }).status).toBe("ready");
    expect(mergeBackendStatus({ status: "starting" }, { status: "error", detail: "boom" })).toEqual(
      { status: "error", detail: "boom" },
    );
  });

  it("never rolls a settled status back to starting", () => {
    // 購読イベント(ready)の後に、購読前の状態を返した現在値取得(starting)が
    // 遅れて解決しても巻き戻さない。
    expect(mergeBackendStatus({ status: "ready" }, { status: "starting" })).toEqual({
      status: "ready",
    });
    expect(mergeBackendStatus({ status: "error", detail: "boom" }, { status: "starting" })).toEqual(
      { status: "error", detail: "boom" },
    );
  });
});

describe("watchBackendStatus (#146)", () => {
  it("recovers the status when the event was sent before subscribing", async () => {
    // 実際に起きた形: mainが`ready`を送った後にレンダラーがマウントするため、
    // 購読してもイベントは二度と来ない。現在値の取得で復帰できること。
    const fake = fakeApi();
    fake.setCurrent({ status: "ready" });
    const seen: BackendStatusSnapshot[] = [];

    watchBackendStatus(fake.api, (snapshot) => seen.push(snapshot));
    await flush();

    expect(seen).toEqual([{ status: "ready" }]);
  });

  it("applies pushed events after subscribing", async () => {
    const fake = fakeApi();
    const seen: BackendStatusSnapshot[] = [];
    watchBackendStatus(fake.api, (snapshot) => seen.push(snapshot));
    await flush();

    fake.push("error", "backend exited unexpectedly (code 1)");
    expect(seen.at(-1)).toEqual({
      status: "error",
      detail: "backend exited unexpectedly (code 1)",
    });
  });

  it("does not roll back to starting when a stale pull resolves last", async () => {
    const fake = fakeApi();
    fake.setCurrent({ status: "starting" }); // 購読時点の古い値
    const seen: BackendStatusSnapshot[] = [];
    watchBackendStatus(fake.api, (snapshot) => seen.push(snapshot));

    fake.push("ready"); // イベントが先に届く
    await flush(); // その後で古い現在値(starting)が解決する

    expect(seen.at(-1)).toEqual({ status: "ready" });
    expect(seen.map((s) => s.status)).not.toContain("starting");
  });

  it("unsubscribes when the returned disposer is called", () => {
    const fake = fakeApi();
    const dispose = watchBackendStatus(fake.api, () => undefined);
    expect(fake.isSubscribed()).toBe(true);
    dispose();
    expect(fake.isSubscribed()).toBe(false);
  });
});
