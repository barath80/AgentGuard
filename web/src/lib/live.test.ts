import { afterEach, describe, expect, it, vi } from "vitest";
import { parseFrame, throttle } from "./live";

describe("parseFrame", () => {
  it("reads event and JSON data", () => {
    expect(parseFrame('event: change\ndata: {"executions":[],"approvals":[]}')).toEqual({
      event: "change",
      data: { executions: [], approvals: [] },
    });
  });
  it("ignores heartbeats and malformed data", () => {
    expect(parseFrame(": heartbeat")).toBeNull();
    expect(parseFrame("event: change\ndata: {not json")).toBeNull();
  });
});

describe("throttle", () => {
  afterEach(() => vi.useRealTimers());

  it("runs at once, then coalesces a burst into one trailing run", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const t = throttle(fn, 5000);
    t.run();
    expect(fn).toHaveBeenCalledTimes(1);
    for (let i = 0; i < 20; i++) t.run(); // a burst of changes
    expect(fn).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(5000);
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("cancel drops a pending run", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const t = throttle(fn, 5000);
    t.run();
    t.run();
    t.cancel();
    vi.advanceTimersByTime(10_000);
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
