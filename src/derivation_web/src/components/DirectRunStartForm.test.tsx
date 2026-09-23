import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { fixtureCreateRunDefaults, fixtureIntakeSession, fixtureProblemPresets } from "../fixtures";
import { DirectRunStartForm, parseDirectProblem } from "./DirectRunStartForm";
import { NewRunStart } from "./NewRunStart";

function props() {
  return { loading: false, defaults: fixtureCreateRunDefaults, loadPresets: vi.fn().mockResolvedValue(fixtureProblemPresets()), onSubmit: vi.fn() };
}

async function choose(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("option", { name: "First example" });
  await user.selectOptions(screen.getByLabelText("Prepared problem"), "fixture_first");
}

describe("direct problem entry", () => {
  it("starts the exact displayed science without Intake confirmation, with explicit unlimited and checker policy", async () => {
    const values = props();
    const user = userEvent.setup();
    render(<DirectRunStartForm {...values} />);
    await choose(user);
    expect(screen.getByRole("checkbox", { name: "No total call limit" })).toBeChecked();
    expect(screen.queryByText("I confirm")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start derivation" }));
    expect(values.onSubmit).toHaveBeenCalledTimes(1);
    const request = values.onSubmit.mock.calls[0][0];
    expect(request.problem).toEqual({ ...fixtureProblemPresets().presets[0].problem, allowed_references: fixtureProblemPresets().method_references });
    expect(request.problem).toMatchObject({ origin: "direct_spec", confirmed_by_user: false });
    expect(request.config).toMatchObject({ checker_enabled: true, record_version: "1.1", max_model_calls: null, max_local_repairs: 3, writer: { model: "gpt-5.5", effort: "high" }, checker: { model: "gpt-5.5", effort: "high" } });
    expect(request.runtime.capability_profile).toBe("source_reading_v1");
  });

  it("turns checking and source access off without changing the scientific problem", async () => {
    const values = props();
    const user = userEvent.setup();
    render(<DirectRunStartForm {...values} />);
    await choose(user);
    await user.click(screen.getByRole("checkbox", { name: "Check each segment and return feedback" }));
    await user.selectOptions(screen.getByLabelText("Reference access"), "none");
    expect(screen.getByText("Checker off · segments remain unchecked")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Start derivation" }));
    const request = values.onSubmit.mock.calls[0][0];
    expect(request.problem).toEqual({ ...fixtureProblemPresets().presets[0].problem, allowed_references: [], source_pack: null });
    expect(request.config).toMatchObject({ checker_enabled: false, reference_allowed: false, allowed_paths: [] });
  });

  it("offers no reference access when the build lists no method papers", async () => {
    const values = props();
    const catalog = fixtureProblemPresets();
    values.loadPresets = vi.fn().mockResolvedValue({ ...catalog, method_references: [] });
    const user = userEvent.setup();
    render(<DirectRunStartForm {...values} />);
    await choose(user);
    expect(screen.getByLabelText("Reference access")).toHaveValue("none");
    expect(screen.queryByRole("option", { name: "Method papers" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start derivation" }));
    const request = values.onSubmit.mock.calls[0][0];
    expect(request.problem).toEqual({ ...catalog.presets[0].problem, allowed_references: [], source_pack: null });
    expect(request.config).toMatchObject({ reference_allowed: false });
  });

  it("does not silently substitute another model when GPT-5.5 is absent", async () => {
    const values = props();
    values.defaults = { ...values.defaults, model_options: values.defaults.model_options.filter((item) => item.model !== "gpt-5.5") };
    const user = userEvent.setup();
    render(<DirectRunStartForm {...values} />);
    await choose(user);
    expect(screen.getByLabelText("Model")).toHaveValue("gpt-5.5");
    expect(screen.getByRole("alert")).toHaveTextContent("unavailable");
    expect(screen.getByRole("button", { name: "Start derivation" })).toBeDisabled();
    expect(values.onSubmit).not.toHaveBeenCalled();
  });

  it("keeps failed imports unarmed and permits corrected imports", async () => {
    const values = props();
    const user = userEvent.setup();
    render(<DirectRunStartForm {...values} />);
    await choose(user);
    const invalid = new File(["{}"], "invalid.json", { type: "application/json" });
    Object.defineProperty(invalid, "text", { value: async () => "{}" });
    await user.upload(screen.getByLabelText("Import problem JSON"), invalid);
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "Start derivation" })).toBeDisabled();
    const problem = fixtureProblemPresets().presets[1].problem;
    const file = new File([JSON.stringify(problem)], "problem.json", { type: "application/json" });
    Object.defineProperty(file, "text", { value: async () => JSON.stringify(problem) });
    await user.upload(screen.getByLabelText("Import problem JSON"), file);
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Start derivation" }));
    expect(values.onSubmit.mock.calls[0][0].problem.problem_id).toBe("fixture_second");
  });

  it("rejects imported human-confirmed provenance rather than relabeling it", () => {
    expect(() => parseDirectProblem(JSON.stringify(fixtureIntakeSession("confirmed").frozen_problem))).toThrow("direct_origin_required");
  });

  it("keeps optional Intake reachable without invoking it on direct entry", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn();
    const values = { ...props(), onCreate, onLoad: vi.fn(), onListActive: vi.fn().mockResolvedValue([]), onRound: vi.fn(), onFinalize: vi.fn(), onConfirm: vi.fn(), onCancel: vi.fn(), onSessionChange: vi.fn() };
    render(<NewRunStart {...values} />);
    expect(screen.getByRole("heading", { name: "Start from a complete problem" })).toBeVisible();
    expect(onCreate).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Clarify with AI" }));
    expect(screen.getByLabelText("Your research problem")).toBeVisible();
    expect(onCreate).not.toHaveBeenCalled();
  });
});
