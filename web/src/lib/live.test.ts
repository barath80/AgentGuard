import { describe, expect, it } from "vitest";
import { parseFrame } from "./live";

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
