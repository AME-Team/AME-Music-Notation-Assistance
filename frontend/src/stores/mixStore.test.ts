import { beforeEach, describe, expect, it } from "vitest";
import { useMixStore } from "./mixStore";

describe("useMixStore", () => {
  beforeEach(() => {
    useMixStore.getState().reset();
  });

  it("is audible by default", () => {
    expect(useMixStore.getState().isAudible("drums")).toBe(true);
  });

  it("a muted track is not audible", () => {
    useMixStore.getState().toggleMute("drums");
    expect(useMixStore.getState().isAudible("drums")).toBe(false);
  });

  it("toggling mute twice restores audibility", () => {
    useMixStore.getState().toggleMute("drums");
    useMixStore.getState().toggleMute("drums");
    expect(useMixStore.getState().isAudible("drums")).toBe(true);
  });

  it("when any track is soloed, only soloed tracks are audible", () => {
    useMixStore.getState().toggleSolo("vocals");
    expect(useMixStore.getState().isAudible("vocals")).toBe(true);
    expect(useMixStore.getState().isAudible("drums")).toBe(false);
  });

  it("mute wins even for a soloed track", () => {
    useMixStore.getState().toggleSolo("vocals");
    useMixStore.getState().toggleMute("vocals");
    expect(useMixStore.getState().isAudible("vocals")).toBe(false);
  });

  it("reset clears both mute and solo state", () => {
    useMixStore.getState().toggleMute("drums");
    useMixStore.getState().toggleSolo("vocals");
    useMixStore.getState().reset();
    expect(useMixStore.getState().isAudible("drums")).toBe(true);
    expect(useMixStore.getState().isAudible("vocals")).toBe(true);
  });
});
