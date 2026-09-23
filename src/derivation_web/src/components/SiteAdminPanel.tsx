import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import type { DerivationClient } from "../api";
import type { IntakeSessionView, RunSummary, RunView, SiteAccountView } from "../api/generated";
import { useLocale } from "../i18n";

interface SiteAdminPanelProps {
  api: DerivationClient;
  onClose: () => void;
}

const copy = {
  en: {
    title: "Manage website users",
    close: "Close user management",
    username: "Username",
    email: "Email",
    initialPassword: "Initial password",
    role: "Role",
    user: "User",
    admin: "Administrator",
    create: "Create user",
    creating: "Creating…",
    passwordPolicy: "15–128 characters. Common or predictable passwords are rejected. This password does not expire; the user may change it later.",
    accounts: "Accounts",
    resetPassword: "New permanent password",
    reset: "Set password",
    disable: "Disable",
    mustChange: "password change required",
    viewContent: "View content",
    contentTitle: "Read-only tenant content",
    contentNotice: "Every catalog or detail opened here writes a metadata-only audit event.",
    runs: "Derivations",
    intakes: "Intakes",
    noRuns: "No derivations",
    noIntakes: "No intakes",
    loadingContent: "Loading content…",
    closeContent: "Close content view",
  },
  "zh-CN": {
    title: "管理网站用户",
    close: "关闭用户管理",
    username: "用户名",
    email: "邮箱",
    initialPassword: "初始密码",
    role: "角色",
    user: "普通用户",
    admin: "管理员",
    create: "创建用户",
    creating: "正在创建…",
    passwordPolicy: "15–128 个字符；系统会拒绝常见或容易猜测的密码。密码不会过期，用户之后可以自行修改。",
    accounts: "现有用户",
    resetPassword: "新的永久密码",
    reset: "设置密码",
    disable: "停用",
    mustChange: "需要更换密码",
    viewContent: "查看内容",
    contentTitle: "只读用户内容",
    contentNotice: "在这里打开目录或详情时，系统都会写入一条仅含元数据的审计记录。",
    runs: "推导",
    intakes: "题面访谈",
    noRuns: "没有推导",
    noIntakes: "没有题面访谈",
    loadingContent: "正在加载内容…",
    closeContent: "关闭内容查看",
  },
} as const;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unknown error";
}

export function SiteAdminPanel({ api, onClose }: SiteAdminPanelProps) {
  const { locale } = useLocale();
  const text = copy[locale];
  const [accounts, setAccounts] = useState<SiteAccountView[]>([]);
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"user" | "admin">("user");
  const [resetPasswords, setResetPasswords] = useState<Record<string, string>>({});
  const [contentAccount, setContentAccount] = useState<SiteAccountView>();
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [intakes, setIntakes] = useState<IntakeSessionView[]>([]);
  const [selectedRun, setSelectedRun] = useState<RunView>();
  const [selectedIntake, setSelectedIntake] = useState<IntakeSessionView>();
  const [contentBusy, setContentBusy] = useState(false);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const accountsRequestRef = useRef(0);
  const catalogRequestRef = useRef(0);
  const detailRequestRef = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++accountsRequestRef.current;
    try {
      const nextAccounts = await api.listSiteAccounts();
      if (accountsRequestRef.current !== requestId) return;
      setAccounts(nextAccounts);
      setError(undefined);
    } catch (caught) {
      if (accountsRequestRef.current !== requestId) return;
      setError(errorMessage(caught));
    }
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    closeButtonRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  const createAccount = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      await api.createSiteAccount({
        username,
        email,
        password,
        role,
      });
      setUsername("");
      setEmail("");
      setPassword("");
      await refresh();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const resetPassword = async (account: SiteAccountView) => {
    const next = resetPasswords[account.user_id] ?? "";
    if (!next) return;
    setBusy(true);
    try {
      await api.resetSiteAccountPassword(account.user_id, { new_password: next });
      setResetPasswords((current) => ({ ...current, [account.user_id]: "" }));
      await refresh();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const disableAccount = async (account: SiteAccountView) => {
    setBusy(true);
    try {
      await api.setSiteAccountStatus(account.user_id, {
        status: "disabled",
      });
      await refresh();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const viewContent = async (account: SiteAccountView) => {
    const requestId = ++catalogRequestRef.current;
    detailRequestRef.current += 1;
    setRuns([]);
    setIntakes([]);
    setContentAccount(account);
    setSelectedRun(undefined);
    setSelectedIntake(undefined);
    setContentBusy(true);
    try {
      const [nextRuns, nextIntakes] = await Promise.all([
        api.listAdminSiteAccountRuns(account.user_id),
        api.listAdminSiteAccountIntakes(account.user_id),
      ]);
      if (catalogRequestRef.current !== requestId) return;
      setRuns(nextRuns);
      setIntakes(nextIntakes);
      setError(undefined);
    } catch (caught) {
      if (catalogRequestRef.current !== requestId) return;
      setError(errorMessage(caught));
    } finally {
      if (catalogRequestRef.current === requestId) setContentBusy(false);
    }
  };

  const closeContent = () => {
    catalogRequestRef.current += 1;
    detailRequestRef.current += 1;
    setContentAccount(undefined);
    setRuns([]);
    setIntakes([]);
    setSelectedRun(undefined);
    setSelectedIntake(undefined);
    setContentBusy(false);
  };

  const viewRun = async (runId: string) => {
    if (!contentAccount) return;
    const ownerUserId = contentAccount.user_id;
    const requestId = ++detailRequestRef.current;
    setSelectedRun(undefined);
    setSelectedIntake(undefined);
    setContentBusy(true);
    try {
      const run = await api.getAdminSiteAccountRun(ownerUserId, runId);
      if (detailRequestRef.current !== requestId) return;
      setSelectedRun(run);
      setSelectedIntake(undefined);
      setError(undefined);
    } catch (caught) {
      if (detailRequestRef.current !== requestId) return;
      setError(errorMessage(caught));
    } finally {
      if (detailRequestRef.current === requestId) setContentBusy(false);
    }
  };

  const viewIntake = async (sessionId: string) => {
    if (!contentAccount) return;
    const ownerUserId = contentAccount.user_id;
    const requestId = ++detailRequestRef.current;
    setSelectedRun(undefined);
    setSelectedIntake(undefined);
    setContentBusy(true);
    try {
      const intake = await api.getAdminSiteAccountIntake(ownerUserId, sessionId);
      if (detailRequestRef.current !== requestId) return;
      setSelectedIntake(intake);
      setSelectedRun(undefined);
      setError(undefined);
    } catch (caught) {
      if (detailRequestRef.current !== requestId) return;
      setError(errorMessage(caught));
    } finally {
      if (detailRequestRef.current === requestId) setContentBusy(false);
    }
  };

  return (
    <div className="site-admin-scrim" role="presentation">
      <section className="site-admin-panel" role="dialog" aria-modal="true" aria-labelledby="site-admin-title">
        <header><h2 id="site-admin-title">{text.title}</h2><button ref={closeButtonRef} type="button" className="quiet-button" onClick={onClose}>{text.close}</button></header>
        <form className="site-admin-create" onSubmit={(event) => void createAccount(event)}>
          <label>{text.username}<input value={username} onChange={(event) => setUsername(event.target.value)} required /></label>
          <label>{text.email}<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></label>
          <label>{text.initialPassword}<input type="password" autoComplete="new-password" minLength={15} maxLength={128} value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
          <label>{text.role}<select value={role} onChange={(event) => setRole(event.target.value as "user" | "admin")}><option value="user">{text.user}</option><option value="admin">{text.admin}</option></select></label>
          <p className="site-admin-password-policy">{text.passwordPolicy}</p>
          <button type="submit" className="primary-button" disabled={busy}>{busy ? text.creating : text.create}</button>
        </form>
        {error && <div className="error-banner" role="alert">{error}</div>}
        <h3>{text.accounts}</h3>
        <div className="site-account-list">
          {accounts.map((account) => (
            <article key={account.user_id} className="site-account-row">
              <div><strong>{account.username}</strong><span>{account.email} · {account.role} · {account.status}{account.must_change_password ? ` · ${text.mustChange}` : ""}</span></div>
              <div className="site-account-actions">
                <input aria-label={`${text.resetPassword}: ${account.username}`} type="password" autoComplete="new-password" minLength={15} maxLength={128} placeholder={text.resetPassword} value={resetPasswords[account.user_id] ?? ""} onChange={(event) => setResetPasswords((current) => ({ ...current, [account.user_id]: event.target.value }))} />
                <button type="button" className="quiet-button" disabled={busy || !(resetPasswords[account.user_id] ?? "")} onClick={() => void resetPassword(account)}>{text.reset}</button>
                <button type="button" className="quiet-button" disabled={busy || contentBusy} onClick={() => void viewContent(account)}>{text.viewContent}</button>
                {account.status === "active" && <button type="button" className="danger-button" disabled={busy} onClick={() => void disableAccount(account)}>{text.disable}</button>}
              </div>
            </article>
          ))}
        </div>
        {contentAccount && (
          <section className="site-admin-content" aria-label={`${text.contentTitle}: ${contentAccount.username}`} aria-busy={contentBusy}>
            <header>
              <div><h3>{text.contentTitle}: {contentAccount.username}</h3><p>{text.contentNotice}</p></div>
              <button type="button" className="quiet-button" onClick={closeContent}>{text.closeContent}</button>
            </header>
            {contentBusy && <p aria-live="polite">{text.loadingContent}</p>}
            <div className="site-admin-content-catalogs">
              <div><h4>{text.runs}</h4>{!contentBusy && runs.length === 0 && <p>{text.noRuns}</p>}{runs.map((run) => <button type="button" disabled={contentBusy} key={run.id} onClick={() => void viewRun(run.id)}><strong>{run.question}</strong><span>{run.phase} · {run.step_count} steps</span></button>)}</div>
              <div><h4>{text.intakes}</h4>{!contentBusy && intakes.length === 0 && <p>{text.noIntakes}</p>}{intakes.map((intake) => <button type="button" disabled={contentBusy} key={intake.session_id} onClick={() => void viewIntake(intake.session_id)}><strong>{intake.session_id}</strong><span>{intake.status} · revision {intake.revision}</span></button>)}</div>
            </div>
            {selectedRun && <pre className="site-admin-content-detail">{JSON.stringify(selectedRun, null, 2)}</pre>}
            {selectedIntake && <pre className="site-admin-content-detail">{JSON.stringify(selectedIntake, null, 2)}</pre>}
          </section>
        )}
      </section>
    </div>
  );
}
