import type { AccountRateLimitsView, AccountRateLimitWindowView } from "../api/generated";
import { useLocale } from "../i18n";

function planLabel(planType: string | null | undefined) {
  if (!planType) return "ChatGPT";
  return `ChatGPT ${planType.charAt(0).toUpperCase()}${planType.slice(1)}`;
}

export function AccountRateLimitStatus({ value }: { value: AccountRateLimitsView | null }) {
  const { messages: m, formatDateTime } = useLocale();
  if (!value || value.status === "signed_out") return null;
  if (value.status === "temporarily_unavailable" || value.windows.length === 0) {
    return (
      <footer className="account-rate-limit-status stale" aria-label={`ChatGPT. ${m.usageUnavailable}`}>
        <strong>ChatGPT</strong>
        <span>{m.usageUnavailable}</span>
      </footer>
    );
  }

  const label = (window: AccountRateLimitWindowView) =>
    window.kind === "weekly" ? m.usageWeekly : window.kind === "five_hour" ? m.usageFiveHour : m.usageOther;
  const weekly = value.windows.find((window) => window.kind === "weekly");
  const primary = weekly ?? value.windows[0];
  const secondary = value.windows.find((window) => window !== primary);
  const reset = primary.resets_at ? `${m.usageResets}: ${formatDateTime(primary.resets_at)}` : null;
  const accessible = [
    planLabel(value.plan_type),
    ...value.windows.map((window) => `${label(window)} ${window.remaining_percent}% ${m.usageLeft}`),
    reset,
    value.stale ? m.usageStale : null,
  ].filter(Boolean).join(". ");

  return (
    <footer className={`account-rate-limit-status${value.stale ? " stale" : ""}`} aria-label={accessible}>
      <strong>{planLabel(value.plan_type)}</strong>
      <span className="quota-primary">{label(primary)} <b>{primary.remaining_percent}%</b> {m.usageLeft}</span>
      {secondary && <span className="quota-secondary">{label(secondary)} <b>{secondary.remaining_percent}%</b> {m.usageLeft}</span>}
      {reset && <span className="quota-reset">{reset}</span>}
      {value.stale && <span className="quota-stale">{m.usageStale}</span>}
    </footer>
  );
}
