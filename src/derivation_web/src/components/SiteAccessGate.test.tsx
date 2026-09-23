import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import type { IntakeSessionView, RunSummary } from "../api/generated";
import { createFixtureApi, fixtureRun } from "../fixtures";
import { browserHost } from "../host";
import { LocaleProvider } from "../i18n";
import { SiteAccessGate } from "./SiteAccessGate";

const session = {
  account: {
    user_id: "a".repeat(32),
    username: "alice",
    email: "alice@example.test",
    role: "user" as const,
    status: "active" as const,
    must_change_password: false,
  },
  idle_expires_at: "2027-01-15T08:00:00Z",
  absolute_expires_at: "2027-01-16T08:00:00Z",
};

describe("SiteAccessGate", () => {
  it("opens the product only after website login and supports logout", async () => {
    const api = createFixtureApi();
    api.getSiteMode = vi.fn(async () => ({ mode: "server" as const, channel: "preview" as const }));
    api.getSiteSession = vi.fn(async () => {
      throw new ApiError(401, "site_session_required", "Sign in", null, "request-1");
    });
    api.loginSite = vi.fn(async () => session);
    api.logoutSite = vi.fn(async () => undefined);
    const user = userEvent.setup();

    render(
      <LocaleProvider host={browserHost}>
        <SiteAccessGate api={api}><div>Private product</div></SiteAccessGate>
      </LocaleProvider>,
    );

    await user.type(await screen.findByLabelText("Username or email"), "alice");
    await user.type(screen.getByLabelText("Password"), "private-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Private product")).toBeVisible();
    expect(screen.getByText(/alice@example.test/)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { name: "Sign in to DerivationLab" })).toBeVisible();
    expect(api.logoutSite).toHaveBeenCalledOnce();
  });

  it("forces a temporary-password user to change it and then sign in again", async () => {
    const api = createFixtureApi();
    api.getSiteMode = vi.fn(async () => ({ mode: "server" as const, channel: "preview" as const }));
    api.getSiteSession = vi.fn(async () => ({
      ...session,
      account: { ...session.account, must_change_password: true },
    }));
    api.changeSitePassword = vi.fn(async () => undefined);
    const user = userEvent.setup();

    render(
      <LocaleProvider host={browserHost}>
        <SiteAccessGate api={api}><div>Private product</div></SiteAccessGate>
      </LocaleProvider>,
    );

    await user.type(await screen.findByLabelText("Current password"), "temporary-password");
    await user.type(screen.getByLabelText("New password"), "replacement-private-password");
    await user.type(screen.getByLabelText("Confirm new password"), "replacement-private-password");
    await user.click(screen.getByRole("button", { name: "Change password" }));

    expect(await screen.findByRole("heading", { name: "Sign in to DerivationLab" })).toBeVisible();
    expect(api.changeSitePassword).toHaveBeenCalledWith({
      current_password: "temporary-password",
      new_password: "replacement-private-password",
    });
  });

  it("shows account management only to administrators", async () => {
    const api = createFixtureApi();
    api.getSiteMode = vi.fn(async () => ({ mode: "server" as const, channel: "preview" as const }));
    const administrator = {
      ...session,
      account: { ...session.account, role: "admin" as const },
    };
    api.getSiteSession = vi.fn(async () => administrator);
    api.listSiteAccounts = vi.fn(async () => [administrator.account]);
    api.createSiteAccount = vi.fn(async (request) => ({
      user_id: "b".repeat(32),
      username: request.username,
      email: request.email,
      role: request.role ?? "user",
      status: "active" as const,
      must_change_password: false,
    }));
    api.listAdminSiteAccountRuns = vi.fn(async () => [{
      id: fixtureRun.id,
      question: "Audited run",
      status: fixtureRun.status,
      phase: fixtureRun.phase,
      step_count: fixtureRun.steps.length,
      route_count: fixtureRun.routes.length,
      read_only: true,
      created_at: fixtureRun.created_at,
      updated_at: fixtureRun.updated_at,
    }]);
    api.listAdminSiteAccountIntakes = vi.fn(async () => []);
    api.getAdminSiteAccountRun = vi.fn(async () => ({
      ...fixtureRun,
      read_only: true,
      commands: {
        can_pause: false,
        can_resume: false,
        can_interrupt: false,
        branchable_step_revision_ids: [],
      },
    }));
    const user = userEvent.setup();

    render(
      <LocaleProvider host={browserHost}>
        <SiteAccessGate api={api}><div>Private product</div></SiteAccessGate>
      </LocaleProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "Manage users" }));
    expect(await screen.findByRole("dialog", { name: "Manage website users" })).toBeVisible();
    await user.type(screen.getByLabelText("Username"), "bob");
    await user.type(screen.getByLabelText("Email"), "bob@example.test");
    await user.type(screen.getByLabelText("Initial password"), "bob-permanent-private-password");
    await user.click(screen.getByRole("button", { name: "Create user" }));

    expect(api.createSiteAccount).toHaveBeenCalledWith({
      username: "bob",
      email: "bob@example.test",
      password: "bob-permanent-private-password",
      role: "user",
    });

    await user.click(screen.getByRole("button", { name: "View content" }));
    await user.click(await screen.findByRole("button", { name: /Audited run/ }));
    expect(await screen.findByText(/"id": "demo-run"/)).toBeVisible();
    expect(api.getAdminSiteAccountRun).toHaveBeenCalledWith(administrator.account.user_id, fixtureRun.id);
  });

  it("ignores an administrator content response after its view is closed", async () => {
    const api = createFixtureApi();
    const administrator = {
      ...session,
      account: { ...session.account, role: "admin" as const },
    };
    let resolveRuns!: (value: RunSummary[]) => void;
    let resolveIntakes!: (value: IntakeSessionView[]) => void;
    api.getSiteMode = vi.fn(async () => ({ mode: "server" as const, channel: "preview" as const }));
    api.getSiteSession = vi.fn(async () => administrator);
    api.listSiteAccounts = vi.fn(async () => [administrator.account]);
    api.listAdminSiteAccountRuns = vi.fn(() => new Promise<RunSummary[]>((resolve) => { resolveRuns = resolve; }));
    api.listAdminSiteAccountIntakes = vi.fn(() => new Promise<IntakeSessionView[]>((resolve) => { resolveIntakes = resolve; }));
    const user = userEvent.setup();

    render(
      <LocaleProvider host={browserHost}>
        <SiteAccessGate api={api}><div>Private product</div></SiteAccessGate>
      </LocaleProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "Manage users" }));
    await user.click(await screen.findByRole("button", { name: "View content" }));
    await user.click(screen.getByRole("button", { name: "Close content view" }));
    await act(async () => {
      resolveRuns([]);
      resolveIntakes([]);
    });

    expect(screen.queryByRole("button", { name: "Close content view" })).not.toBeInTheDocument();
  });

  it("fails closed when the explicit mode endpoint is unavailable", async () => {
    const api = createFixtureApi();
    api.getSiteMode = vi.fn(async () => {
      throw new ApiError(404, "not_found", "Missing deployment endpoint", null, "request-2");
    });

    render(
      <LocaleProvider host={browserHost}>
        <SiteAccessGate api={api}><div>Private product</div></SiteAccessGate>
      </LocaleProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Sign in to DerivationLab" })).toBeVisible();
    expect(screen.queryByText("Private product")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Missing deployment endpoint");
  });
});
