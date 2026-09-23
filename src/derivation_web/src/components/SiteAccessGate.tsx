import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { ApiError, type DerivationClient } from "../api";
import type { SiteModeView, SiteSessionView } from "../api/generated";
import { useLocale } from "../i18n";
import { SiteAdminPanel } from "./SiteAdminPanel";

interface SiteAccessGateProps {
  api: DerivationClient;
  children: ReactNode;
}

type GateState =
  | { mode: "checking"; error?: string }
  | { mode: "desktop"; error?: string }
  | { mode: "signed_out"; error?: string }
  | { mode: "authenticated"; session: SiteSessionView; error?: string };

const copy = {
  en: {
    checking: "Checking website access",
    title: "Sign in to DerivationLab",
    explanation: "Use the username or email and password issued by the site administrator.",
    privacyNotice: "During this Development/Pilot, administrators can inspect complete user derivations and account activity for support and auditing.",
    identifier: "Username or email",
    password: "Password",
    signIn: "Sign in",
    signingIn: "Signing in…",
    changeTitle: "Choose a new password",
    changeExplanation: "Your temporary password must be replaced before the product can open.",
    currentPassword: "Current password",
    newPassword: "New password",
    confirmPassword: "Confirm new password",
    mismatch: "The new passwords do not match.",
    changing: "Changing password…",
    changePassword: "Change password",
    signedInAs: "Signed in as",
    administrator: "Administrator",
    signOut: "Sign out",
    manageUsers: "Manage users",
  },
  "zh-CN": {
    checking: "正在检查网站访问权限",
    title: "登录 DerivationLab",
    explanation: "请使用网站管理员分配的用户名或邮箱与密码。",
    privacyNotice: "当前 Development/Pilot 阶段，管理员可为支持与审计查看用户的完整推导内容和账号活动。",
    identifier: "用户名或邮箱",
    password: "密码",
    signIn: "登录",
    signingIn: "正在登录…",
    changeTitle: "设置新密码",
    changeExplanation: "首次登录必须先更换临时密码，然后才能进入产品。",
    currentPassword: "当前密码",
    newPassword: "新密码",
    confirmPassword: "再次输入新密码",
    mismatch: "两次输入的新密码不一致。",
    changing: "正在更换密码…",
    changePassword: "更换密码",
    signedInAs: "已登录",
    administrator: "管理员",
    signOut: "退出",
    manageUsers: "管理用户",
  },
} as const;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unknown error";
}

export function SiteAccessGate({ api, children }: SiteAccessGateProps) {
  const { locale } = useLocale();
  const text = copy[locale];
  const [state, setState] = useState<GateState>({ mode: "checking" });
  const [busy, setBusy] = useState(false);
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [adminOpen, setAdminOpen] = useState(false);
  const [siteChannel, setSiteChannel] = useState<SiteModeView["channel"]>("development");

  const channelLabel = siteChannel.charAt(0).toUpperCase() + siteChannel.slice(1);

  const checkSession = useCallback(async () => {
    try {
      const siteMode = await api.getSiteMode();
      setSiteChannel(siteMode.channel);
      if (siteMode.mode === "desktop") {
        setState({ mode: "desktop" });
        return;
      }
      const session = await api.getSiteSession();
      setState({ mode: "authenticated", session });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setState({ mode: "signed_out" });
      } else {
        setState({ mode: "signed_out", error: errorMessage(error) });
      }
    }
  }, [api]);

  useEffect(() => {
    const unsubscribe = api.onSiteSessionInvalid(() => {
      setPassword("");
      setNewPassword("");
      setConfirmation("");
      setAdminOpen(false);
      setState({ mode: "signed_out" });
    });
    void checkSession();
    return unsubscribe;
  }, [api, checkSession]);

  const login = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      const session = await api.loginSite({ identifier, password });
      setPassword("");
      setState({ mode: "authenticated", session });
    } catch (error) {
      setState({ mode: "signed_out", error: errorMessage(error) });
    } finally {
      setBusy(false);
    }
  };

  const changePassword = async (event: FormEvent) => {
    event.preventDefault();
    if (newPassword !== confirmation) {
      setState((current) => ({ ...current, error: text.mismatch }));
      return;
    }
    setBusy(true);
    try {
      await api.changeSitePassword({ current_password: password, new_password: newPassword });
      setPassword("");
      setNewPassword("");
      setConfirmation("");
      setState({ mode: "signed_out" });
    } catch (error) {
      setState((current) => ({ ...current, error: errorMessage(error) }));
    } finally {
      setBusy(false);
    }
  };

  const logout = async () => {
    setBusy(true);
    try {
      await api.logoutSite();
      setState({ mode: "signed_out" });
    } catch (error) {
      setState((current) => current.mode === "authenticated"
        ? { ...current, error: errorMessage(error) }
        : current);
    } finally {
      setBusy(false);
    }
  };

  if (state.mode === "desktop") return children;
  if (state.mode === "checking") {
    return <main className="state-page" aria-live="polite"><div className="spinner" /><h1>{text.checking}</h1></main>;
  }
  if (state.mode === "signed_out") {
    return (
      <main className="start-shell account-shell site-login-shell">
        <form className="start-card account-card site-login-card" onSubmit={(event) => void login(event)}>
          <p className="eyebrow">DerivationLab {channelLabel}</p>
          <h1>{text.title}</h1>
          <p>{text.explanation}</p>
          <p className="site-privacy-notice">{text.privacyNotice}</p>
          <label>{text.identifier}<input autoComplete="username" value={identifier} onChange={(event) => setIdentifier(event.target.value)} required /></label>
          <label>{text.password}<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
          {state.error && <div className="error-banner" role="alert">{state.error}</div>}
          <div className="account-actions"><button type="submit" className="primary-button" disabled={busy}>{busy ? text.signingIn : text.signIn}</button></div>
        </form>
      </main>
    );
  }

  if (state.session.account.must_change_password) {
    return (
      <main className="start-shell account-shell site-login-shell">
        <form className="start-card account-card site-login-card" onSubmit={(event) => void changePassword(event)}>
          <p className="eyebrow">DerivationLab {channelLabel}</p>
          <h1>{text.changeTitle}</h1>
          <p>{text.changeExplanation}</p>
          <label>{text.currentPassword}<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
          <label>{text.newPassword}<input type="password" autoComplete="new-password" minLength={15} maxLength={128} value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required /></label>
          <label>{text.confirmPassword}<input type="password" autoComplete="new-password" minLength={15} maxLength={128} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} required /></label>
          {state.error && <div className="error-banner" role="alert">{state.error}</div>}
          <div className="account-actions"><button type="submit" className="primary-button" disabled={busy}>{busy ? text.changing : text.changePassword}</button></div>
        </form>
      </main>
    );
  }

  const account = state.session.account;
  return (
    <div className="site-session-shell">
      <header className="site-session-bar">
        <span><strong>{channelLabel}</strong> · {text.signedInAs} <strong>{account.username}</strong> · {account.email}{account.role === "admin" ? ` · ${text.administrator}` : ""}</span>
        {account.role === "admin" && <button type="button" className="quiet-button" onClick={() => setAdminOpen(true)}>{text.manageUsers}</button>}
        <button type="button" className="quiet-button" disabled={busy} onClick={() => void logout()}>{text.signOut}</button>
      </header>
      {state.error && <div className="error-banner site-session-error" role="alert">{state.error}</div>}
      <div className="site-session-product">{children}</div>
      {adminOpen && <SiteAdminPanel api={api} onClose={() => setAdminOpen(false)} />}
    </div>
  );
}
