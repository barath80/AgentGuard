import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { tokenStore, type ExecutionSummary } from "./api";

export type LiveStatus = "connecting" | "live" | "offline";

interface ChangeEvent {
  executions: ExecutionSummary[];
  approvals: { id: string; status: string; execution_id: string }[];
}

/** Parse one SSE frame ("event: x\ndata: {...}"). Comments (": heartbeat") yield nothing. */
export function parseFrame(frame: string): { event: string; data: unknown } | null {
  let event = "message";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (!data.length) return null;
  try {
    return { event, data: JSON.parse(data.join("\n")) };
  } catch {
    return null;
  }
}

/**
 * Run `fn` at most once per `ms`: the first call runs now, later calls inside the window are
 * coalesced into a single trailing run. Returns the throttled function and a cancel.
 */
export function throttle(fn: () => void, ms: number): { run: () => void; cancel: () => void } {
  let last = -Infinity;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const fire = () => {
    timer = null;
    last = Date.now();
    fn();
  };
  return {
    run() {
      if (timer) return;
      const wait = last + ms - Date.now();
      if (wait <= 0) fire();
      else timer = setTimeout(fire, wait);
    },
    cancel() {
      if (timer) clearTimeout(timer);
      timer = null;
    },
  };
}

// Heavy views refresh at most this often while traffic streams in; approvals refresh at once.
const BULK_REFRESH_MS = 5000;

/**
 * Subscribe to /v1/console/stream. Uses fetch rather than EventSource so the token travels in
 * the Authorization header, never in a URL. Reconnects with backoff; while offline, the
 * queries' own slower refetch intervals keep the views fresh.
 */
export function useLiveUpdates(onChange?: (change: ChangeEvent) => void): LiveStatus {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<LiveStatus>("connecting");

  useEffect(() => {
    let stopped = false;
    let controller: AbortController | null = null;
    let retry = 1000;
    const refreshViews = throttle(() => {
      for (const key of ["overview", "executions", "agents", "workflows"]) {
        queryClient.invalidateQueries({ queryKey: [key] });
      }
    }, BULK_REFRESH_MS);

    async function connect() {
      while (!stopped) {
        controller = new AbortController();
        try {
          const response = await fetch("/v1/console/stream", {
            headers: { Authorization: `Bearer ${tokenStore.get() ?? ""}`, Accept: "text/event-stream" },
            signal: controller.signal,
          });
          if (response.status === 401) {
            setStatus("offline");
            return; // signed out; the API layer handles the redirect
          }
          if (!response.ok || !response.body) throw new Error(`stream ${response.status}`);
          const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
          let buffer = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += value.replace(/\r\n/g, "\n");
            let split;
            while ((split = buffer.indexOf("\n\n")) >= 0) {
              const frame = parseFrame(buffer.slice(0, split));
              buffer = buffer.slice(split + 2);
              if (!frame) continue;
              if (frame.event === "ready") {
                setStatus("live");
                retry = 1000;
              } else if (frame.event === "change") {
                const change = frame.data as ChangeEvent;
                if (change.executions.length) {
                  refreshViews.run();
                  for (const e of change.executions) queryClient.invalidateQueries({ queryKey: ["execution", e.id] });
                }
                if (change.approvals.length) queryClient.invalidateQueries({ queryKey: ["approvals"] });
                onChange?.(change);
              }
            }
          }
        } catch {
          if (stopped) return;
        }
        if (stopped) return;
        setStatus("offline");
        await new Promise((r) => setTimeout(r, retry));
        retry = Math.min(retry * 2, 30_000);
        setStatus("connecting");
      }
    }

    connect();
    return () => {
      stopped = true;
      refreshViews.cancel();
      controller?.abort();
    };
  }, [queryClient, onChange]);

  return status;
}
