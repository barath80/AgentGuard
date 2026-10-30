import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { api, type AgentSpec, type Me } from "../lib/api";
import { Button, Card, Segmented } from "./ui";

const INPUT =
  "h-9 w-full rounded-lg border border-line-strong bg-surface px-3 text-[13px] text-ink outline-none transition placeholder:text-ink-3 focus:border-accent focus:ring-3 focus:ring-accent/20";
const ENVIRONMENTS = ["dev", "staging", "prod"] as const;

function Field({ id, label, hint, children }: { id: string; label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <label htmlFor={id} className="mb-1.5 block text-[13px] font-medium text-ink">
        {label}
      </label>
      {children}
      {hint && <p className="mt-1 text-xs text-ink-3">{hint}</p>}
    </div>
  );
}

/** Operator form for adding an agent identity to the registry. The server validates everything again. */
export function RegisterAgent({ me, onDone }: { me: Me; onDone: () => void }) {
  const queryClient = useQueryClient();
  const fixedTenants = me.tenants.filter((t) => t !== "*");
  const [spec, setSpec] = useState<AgentSpec>({
    workload_identity_ref: "",
    owner: "",
    tenant_id: me.reads_all_tenants ? "" : (fixedTenants[0] ?? ""),
    approved_environments: ["dev"],
    risk_category: "medium",
    rate_limit_per_minute: 60,
    daily_spend_limit: null,
  });
  const set = <K extends keyof AgentSpec>(key: K, value: AgentSpec[K]) => setSpec((s) => ({ ...s, [key]: value }));
  const mutation = useMutation({
    mutationFn: () => api.registerAgent(spec),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      onDone();
    },
  });

  const toggleEnv = (env: (typeof ENVIRONMENTS)[number]) =>
    set(
      "approved_environments",
      spec.approved_environments.includes(env)
        ? spec.approved_environments.filter((e) => e !== env)
        : ENVIRONMENTS.filter((e) => e === env || spec.approved_environments.includes(e)),
    );

  const submit = (event: FormEvent) => {
    event.preventDefault();
    mutation.mutate();
  };

  return (
    <Card title="Register an agent" subtitle="The workload identity is what the gateway verifies on every call; nothing an agent sends can change these limits." className="mb-4">
      <form onSubmit={submit} className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <Field id="ref" label="Workload identity" hint="name@pool, lowercase">
            <input
              id="ref"
              required
              autoFocus
              value={spec.workload_identity_ref}
              onChange={(e) => set("workload_identity_ref", e.target.value.trim().toLowerCase())}
              placeholder="returns-agent@prod-workload-pool"
              className={`${INPUT} font-mono`}
            />
          </Field>
          <Field id="owner" label="Owning team">
            <input id="owner" required value={spec.owner} onChange={(e) => set("owner", e.target.value)} placeholder="support-eng" className={INPUT} />
          </Field>
          <Field id="tenant" label="Tenant">
            {me.reads_all_tenants ? (
              <input
                id="tenant"
                required
                value={spec.tenant_id}
                onChange={(e) => set("tenant_id", e.target.value.trim().toLowerCase())}
                placeholder="tenant-a"
                className={`${INPUT} font-mono`}
              />
            ) : (
              <select id="tenant" value={spec.tenant_id} onChange={(e) => set("tenant_id", e.target.value)} className={INPUT}>
                {fixedTenants.map((t) => (
                  <option key={t}>{t}</option>
                ))}
              </select>
            )}
          </Field>
        </div>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <fieldset className="min-w-0">
            <legend className="mb-1.5 text-[13px] font-medium text-ink">Approved environments</legend>
            <div className="flex flex-wrap gap-3 pt-1.5">
              {ENVIRONMENTS.map((env) => (
                <label key={env} className="inline-flex items-center gap-1.5 text-[13px] text-ink-2">
                  <input
                    type="checkbox"
                    checked={spec.approved_environments.includes(env)}
                    onChange={() => toggleEnv(env)}
                    className="size-4 accent-[var(--color-accent)]"
                  />
                  <span className="font-mono">{env}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="min-w-0">
            <p className="mb-1.5 text-[13px] font-medium text-ink">Risk category</p>
            <Segmented
              label="Risk category"
              value={spec.risk_category}
              onChange={(v) => set("risk_category", v)}
              options={[
                { value: "low", label: "Low" },
                { value: "medium", label: "Medium" },
                { value: "high", label: "High" },
              ]}
            />
          </div>
          <Field id="rate" label="Rate limit" hint="calls per minute">
            <input
              id="rate"
              type="number"
              min={1}
              max={10000}
              required
              value={spec.rate_limit_per_minute}
              onChange={(e) => set("rate_limit_per_minute", Number(e.target.value))}
              className={`${INPUT} tabular`}
            />
          </Field>
          <Field id="spend" label="Daily spend limit" hint="rolling 24h; blank for no budget">
            <input
              id="spend"
              type="number"
              min={0}
              step="0.01"
              value={spec.daily_spend_limit ?? ""}
              onChange={(e) => set("daily_spend_limit", e.target.value === "" ? null : Number(e.target.value))}
              placeholder="No budget"
              className={`${INPUT} tabular`}
            />
          </Field>
        </div>

        {mutation.error && (
          <p role="alert" className="text-[13px] text-critical-ink">
            {mutation.error.message}
          </p>
        )}
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button variant="ghost" onClick={onDone}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            icon={Plus}
            loading={mutation.isPending}
            disabled={spec.approved_environments.length === 0}
          >
            Register agent
          </Button>
        </div>
      </form>
    </Card>
  );
}
