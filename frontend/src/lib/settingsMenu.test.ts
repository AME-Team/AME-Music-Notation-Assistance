import { describe, expect, it, vi } from "vitest";
import { OPEN_SETTINGS_CHANNEL, watchOpenSettings } from "./settingsMenu";

describe("watchOpenSettings (#158)", () => {
  it("メニューイベントをコールバックへ中継し、購読解除を返す", () => {
    let listener: (() => void) | undefined;
    const unsubscribe = vi.fn();
    const onOpen = vi.fn();

    const release = watchOpenSettings(
      {
        onOpenSettings: (cb) => {
          listener = cb;
          return unsubscribe;
        },
      },
      onOpen,
    );

    listener?.();
    expect(onOpen).toHaveBeenCalledTimes(1);

    release();
    expect(unsubscribe).toHaveBeenCalledTimes(1);
  });

  it("旧preload(API未提供)でも落ちず、何もしない購読解除を返す", () => {
    const onOpen = vi.fn();

    expect(() => watchOpenSettings({}, onOpen)).not.toThrow();
    expect(() => watchOpenSettings(undefined, onOpen)).not.toThrow();
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("チャンネル名の値そのものを固定する(実ソースとの一致はcontractテストで検証)", () => {
    // 実際の一致は contract テスト(preload.ts のソース照合)で検証する。
    expect(OPEN_SETTINGS_CHANNEL).toBe("menu:open-settings");
  });
});
