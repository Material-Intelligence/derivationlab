import { useState } from "react";
import type { ProductHost } from "../host";
import type { useProductAccount } from "../hooks/useProductAccount";
import { useLocale } from "../i18n";

interface AccountGateProps {
  accountState: ReturnType<typeof useProductAccount>;
  host: ProductHost;
}

export function AccountGate({ accountState, host }: AccountGateProps) {
  const { messages: m, formatDateTime } = useLocale();
  const { account, login, loading, busy, error, refresh, importExisting, startLogin, cancelLogin } = accountState;
  const [confirmingImport, setConfirmingImport] = useState(false);
  const [copied, setCopied] = useState(false);

  if (loading) {
    return <main className="state-page" aria-live="polite"><div className="spinner" /><h1>{m.checkingAccount}</h1></main>;
  }

  const diagnosticLabel: Record<string, string> = {
    product_auth_missing: m.diagnosticMissing,
    product_auth_invalid: m.diagnosticInvalid,
    source_auth_available: m.diagnosticSourceAvailable,
    source_auth_unavailable: m.diagnosticSourceUnavailable,
    account_check_failed: m.diagnosticCheckFailed,
    ready: m.diagnosticReady,
  };
  const loginFinished = login && login.status.status !== "pending";
  return (
    <main className="start-shell account-shell">
      <section className="start-card account-card" aria-labelledby="account-title">
        <div className="account-heading">
          <span className="brand-mark">Φ</span>
          <div><p className="eyebrow">{m.localProductAccount}</p><h1 id="account-title">{m.connectAccount}</h1></div>
        </div>
        <p>{m.accountExplanation}</p>

        {account && login?.status.status !== "pending" && <div className={`account-status ${account.status}`}>
          <strong>{account.status === "reauth_required" ? m.reauthRequired : account.status === "unavailable" ? m.accountUnavailable : m.signedOut}</strong>
          <span>{diagnosticLabel[account.diagnostic] ?? account.diagnostic}</span>
        </div>}
        {error && <div className="error-banner" role="alert">{error}</div>}

        {login ? (
          <section className="device-login" aria-live="polite">
            <h2>{login.status.status === "pending" ? m.finishInBrowser : m.signInStatus}</h2>
            <p>{m.verificationCode}</p>
            <div className="device-code"><code>{login.request.user_code}</code><button type="button" className="quiet-button" onClick={async () => { await host.copyText(login.request.user_code); setCopied(true); }}>{copied ? m.copied : m.copy}</button></div>
            <small>{m.codeExpires} {formatDateTime(login.request.expires_at)}</small>
            {loginFinished && <div className={`account-login-result ${login.status.status}`}>{login.status.status === "signed_in" ? m.loginSuccess : login.status.diagnostic ?? (login.status.status === "expired" ? m.loginExpired : login.status.status === "canceled" ? m.loginCanceled : m.loginFailed)}</div>}
            <div className="dialog-actions">
              {login.status.status === "pending" && <button type="button" className="quiet-button" disabled={busy} onClick={() => void cancelLogin()}>{m.cancelLogin}</button>}
              {loginFinished && login.status.status !== "signed_in" && <button type="button" className="quiet-button" disabled={busy} onClick={() => void startLogin()}>{m.startAgain}</button>}
              <button type="button" className="primary-button" disabled={busy || login.status.status !== "pending" || !host.openExternal} onClick={() => void host.openExternal?.(login.request.verification_url)}>{m.openLoginPage}</button>
            </div>
          </section>
        ) : confirmingImport ? (
          <section className="import-confirmation">
            <h2>{m.confirmImportTitle}</h2>
            <p>{m.confirmImportCopy}</p>
            <div className="dialog-actions"><button type="button" className="quiet-button" disabled={busy} onClick={() => setConfirmingImport(false)}>{m.cancel}</button><button type="button" className="primary-button" disabled={busy} onClick={() => void importExisting()}>{busy ? m.validating : m.confirmImport}</button></div>
          </section>
        ) : (
          <div className="account-actions">
            {account?.import_available && <button type="button" className="primary-button" disabled={busy} onClick={() => setConfirmingImport(true)}>{m.importExisting}</button>}
            <button type="button" className={account?.import_available ? "quiet-button" : "primary-button"} disabled={busy} onClick={() => void startLogin()}>{m.useSeparateAccount}</button>
            {!account && <button type="button" className="quiet-button" disabled={busy} onClick={() => void refresh()}>{m.retryCheck}</button>}
          </div>
        )}
      </section>
    </main>
  );
}
