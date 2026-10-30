import { describe, expect, it } from "vitest";
import { describeRule, reasonLabel, shortId, timeAgo, timeUntil } from "./format";

describe("describeRule", () => {
  it("renders an approval rule with its argument condition", () => {
    expect(
      describeRule({
        priority: 20,
        decision: "approval_required",
        approval_role: "support-lead",
        match: { tool: "issue-refund", arg: { field: "amount", op: "gt", value: 500 } },
      }),
    ).toBe("When tool is issue-refund and amount > 500 → require approval from support-lead");
  });

  it("joins list values and treats an empty match as always", () => {
    expect(describeRule({ priority: 1, decision: "deny", match: { environment: ["prod", "staging"] } })).toBe(
      "When environment is prod or staging → deny",
    );
    expect(describeRule({ priority: 1, decision: "allow", match: {} })).toBe("Always → allow");
  });
});

describe("time helpers", () => {
  const now = Date.parse("2026-10-29T12:00:00Z");
  it("formats past and future times", () => {
    expect(timeAgo("2026-10-29T11:58:00Z", now)).toBe("2m ago");
    expect(timeUntil("2026-10-29T12:05:30Z", now)).toBe("5m 30s left");
    expect(timeUntil("2026-10-29T11:00:00Z", now)).toBe("expired");
  });
});

describe("labels", () => {
  it("shortens hashes and control reasons", () => {
    expect(shortId("sha256:abcdef0123456789")).toBe("abcdef01");
    expect(reasonLabel("control:rate_limit")).toBe("Control: rate limit");
  });
});
