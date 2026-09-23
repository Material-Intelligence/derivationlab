import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { SubmitIntakeRoundRequest } from "../types";
import {
  fixtureCreateRunDefaults,
  fixtureIntakeFrontier,
  fixtureIntakeSession,
} from "../fixtures";
import { RunStartForm } from "./RunStartForm";

/** What the HTTP client throws when the authoritative state refuses a command. */
const conflict = (message: string) =>
  Object.assign(new Error(message), { status: 409, code: "intake_command_invalid" });

const answerWholeFrontier = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(
    within(await screen.findByRole("group", { name: "Fourier convention" })).getByRole(
      "radio",
      { name: /Use Fourier convention/ },
    ),
  );
  await user.click(
    within(screen.getByRole("group", { name: "output form" })).getByRole("radio", {
      name: /Use output form/,
    }),
  );
};

function props(overrides: Record<string, unknown> = {}) {
  return {
    loading: false,
    defaults: fixtureCreateRunDefaults,
    onCreate: vi.fn().mockResolvedValue(fixtureIntakeSession()),
    onLoad: vi.fn().mockResolvedValue(fixtureIntakeSession()),
    onListActive: vi.fn().mockResolvedValue([]),
    onRound: vi.fn().mockResolvedValue(fixtureIntakeSession("candidate_ready")),
    onFinalize: vi
      .fn()
      .mockResolvedValue(fixtureIntakeSession("candidate_ready", { finalized: true })),
    onConfirm: vi.fn().mockResolvedValue(fixtureIntakeSession("confirmed")),
    onCancel: vi.fn().mockResolvedValue(fixtureIntakeSession("cancelled")),
    onSessionChange: vi.fn(),
    onSubmit: vi.fn(),
    ...overrides,
  };
}

/** The same switch `FakeDerivationService` uses: a strategy stalls the grill. */
const stallingRound = () =>
  vi.fn(async (_sessionId: string, request: SubmitIntakeRoundRequest) =>
    Object.values(request.answers ?? {}).some(
      (answer) => (answer as { strategy?: string | null }).strategy,
    )
      ? fixtureIntakeSession("convergence_required")
      : fixtureIntakeSession("candidate_ready"),
  );

describe("persistent frontier-based Problem Intake", () => {
  it("defaults Intake to GPT-5.6-Sol high fast before the first organize call", async () => {
    const user = userEvent.setup();
    const values = props();
    render(<RunStartForm {...values} />);

    const model = screen.getByLabelText("Model");
    const effort = screen.getByLabelText("Effort");
    const speed = screen.getByLabelText("Speed");
    expect(model.tagName).toBe("SELECT");
    expect(effort.tagName).toBe("SELECT");
    expect(speed.tagName).toBe("SELECT");
    expect(model).toHaveValue("gpt-5.6-sol");
    expect(effort).toHaveValue("high");
    expect(speed).toHaveValue("fast");
    expect(values.defaults.allowed_models.length).toBeGreaterThan(0);
    expect(values.defaults.allowed_efforts.length).toBeGreaterThan(0);
    for (const item of values.defaults.model_options) {
      expect(
        within(model).getByRole("option", { name: item.display_name }),
      ).toHaveValue(item.model);
    }
    for (const item of values.defaults.model_options[0].supported_efforts) {
      expect(within(effort).getByRole("option", { name: item })).toBeInTheDocument();
    }
    await user.selectOptions(effort, "ultra");
    await user.selectOptions(model, "gpt-5.5");
    expect(effort).toHaveValue("medium");
    expect(within(effort).queryByRole("option", { name: "ultra" })).toBeNull();
    expect(within(effort).queryByRole("option", { name: "max" })).toBeNull();
    await user.type(screen.getByLabelText("Your research problem"), "Derive the response tensor");
    await user.click(screen.getByRole("button", { name: "Let AI organize the problem" }));

    await waitFor(() => expect(values.onCreate).toHaveBeenCalledTimes(1));
    expect(values.onCreate).toHaveBeenCalledWith({
      initial_message: "Derive the response tensor",
      model: "gpt-5.5",
      effort: "medium",
      service_tier: "fast",
    });
  });

  it("falls back to Standard when the selected model has no Fast tier", async () => {
    const user = userEvent.setup();
    render(<RunStartForm {...props()} />);

    await user.selectOptions(screen.getByLabelText("Model"), "gpt-5.4-mini");

    const speed = screen.getByLabelText("Speed");
    expect(speed).toHaveValue("standard");
    expect(within(speed).queryByRole("option", { name: "Fast" })).toBeNull();
  });

  it("exposes writer checker and judge model effort selects before confirm", async () => {
    const user = userEvent.setup();
    const values = props({
      initialSessionId: "intake-ui",
      onLoad: vi.fn().mockResolvedValue(fixtureIntakeSession("candidate_ready")),
    });
    render(<RunStartForm {...values} />);

    expect(await screen.findByRole("heading", { name: "Confirm this derivation problem" })).toBeVisible();
    const writer = screen.getByRole("group", { name: "writer" });
    const checker = screen.getByRole("group", { name: "checker" });
    const judge = screen.getByRole("group", { name: "judge" });
    const writerModel = within(writer).getByLabelText("Model");
    const writerEffort = within(writer).getByLabelText("Effort");
    expect(writerModel.tagName).toBe("SELECT");
    expect(writerEffort.tagName).toBe("SELECT");
    expect(within(checker).getByLabelText("Model").tagName).toBe("SELECT");
    expect(within(judge).getByLabelText("Effort").tagName).toBe("SELECT");
    expect(writerModel.closest("details.advanced-config")).toBeNull();
    expect(screen.queryByRole("group", { name: "writer" })?.closest("details.advanced-config")).toBeNull();
    expect(screen.getByLabelText("Speed")).toHaveValue("fast");
    expect(screen.getByLabelText("Max model calls")).toHaveValue(100);

    await user.selectOptions(writerModel, "gpt-5.5");
    expect(within(writerEffort).queryByRole("option", { name: "ultra" })).toBeNull();
    await user.selectOptions(writerEffort, "xhigh");
    await user.selectOptions(within(checker).getByLabelText("Model"), "gpt-5.6-sol");
    await user.selectOptions(within(checker).getByLabelText("Effort"), "max");
    await user.selectOptions(within(judge).getByLabelText("Model"), "gpt-5.5");
    await user.selectOptions(within(judge).getByLabelText("Effort"), "high");
    await user.click(screen.getByRole("checkbox", { name: /I confirm that the complete/ }));
    await user.click(screen.getByRole("button", { name: "Confirm and start derivation" }));

    await waitFor(() => expect(values.onSubmit).toHaveBeenCalledTimes(1));
    expect(values.onSubmit.mock.calls[0][0].config.writer).toMatchObject({
      model: "gpt-5.5",
      effort: "xhigh",
    });
    expect(values.onSubmit.mock.calls[0][0].config.checker).toMatchObject({
      model: "gpt-5.6-sol",
      effort: "max",
    });
    expect(values.onSubmit.mock.calls[0][0].config.judge).toMatchObject({
      model: "gpt-5.5",
      effort: "high",
    });
    expect(values.onSubmit.mock.calls[0][0].runtime.service_tier).toBe("fast");
  });

  it("shows every independent frontier question and submits them together", async () => {
    const user = userEvent.setup();
    const values = props();
    render(<RunStartForm {...values} />);

    expect(
      screen.getByText("At most 3 rounds of questions; after that you decide whether to start."),
    ).toBeVisible();
    await user.type(screen.getByLabelText("Your research problem"), "Derive the response tensor");
    await user.click(screen.getByRole("button", { name: "Let AI organize the problem" }));
    const conventionGroup = await screen.findByRole("group", {
      name: "Fourier convention",
    });
    const outputGroup = screen.getByRole("group", { name: "output form" });
    expect(conventionGroup).toBeVisible();
    expect(outputGroup).toBeVisible();
    expect(within(conventionGroup).getByText(/Recommended/)).toBeVisible();
    expect(within(outputGroup).getByText(/Recommended/)).toBeVisible();
    expect(screen.getByText("Rounds used: 0 / 3")).toBeVisible();
    expect(
      within(conventionGroup).getByText(
        /Answering fourier convention changes which quantity the derivation delivers/,
      ),
    ).toBeVisible();
    const continueButton = screen.getByRole("button", { name: "Answer and continue" });
    expect(continueButton).toBeDisabled();

    await user.click(screen.getByRole("radio", { name: /Use Fourier convention/ }));
    expect(continueButton).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: /Use output form/ }));
    expect(continueButton).toBeEnabled();
    await user.click(continueButton);

    await waitFor(() => expect(values.onRound).toHaveBeenCalledTimes(1));
    expect(values.onRound).toHaveBeenCalledWith(
      "intake-ui",
      expect.objectContaining({
        base_revision: 1,
        answers: {
          convention: { selected_option_ids: ["minus"], custom_text: null },
          "output-form": { selected_option_ids: ["full-chain"], custom_text: null },
        },
      }),
    );
  });

  it("accepts a ladder strategy as a complete answer on its own", async () => {
    const user = userEvent.setup();
    const values = props({ initialSessionId: "intake-ui", onRound: stallingRound() });
    render(<RunStartForm {...values} />);

    const conventionGroup = await screen.findByRole("group", { name: "Fourier convention" });
    const continueButton = screen.getByRole("button", { name: "Answer and continue" });
    await user.click(
      within(conventionGroup).getByRole("button", {
        name: "Start from the simplest approximation, refine later",
      }),
    );
    expect(continueButton).toBeDisabled();
    const outputGroup = screen.getByRole("group", { name: "output form" });
    await user.click(within(outputGroup).getByRole("button", { name: "Derive both routes" }));
    expect(continueButton).toBeEnabled();
    await user.click(continueButton);

    await waitFor(() => expect(values.onRound).toHaveBeenCalledTimes(1));
    expect(values.onRound.mock.calls[0][1]).toMatchObject({
      base_revision: 1,
      answers: {
        convention: { selected_option_ids: [], custom_text: null, strategy: "simplest_first" },
        "output-form": { selected_option_ids: [], custom_text: null, strategy: "both_routes" },
      },
    });
  });

  it("keeps custom text when a ladder strategy is selected", async () => {
    const user = userEvent.setup();
    const values = props({ initialSessionId: "intake-ui", onRound: stallingRound() });
    render(<RunStartForm {...values} />);

    const conventionGroup = await screen.findByRole("group", { name: "Fourier convention" });
    await user.type(
      within(conventionGroup).getByLabelText("Other / custom answer"),
      "Keep the supplied phase convention.",
    );
    await user.click(
      within(conventionGroup).getByRole("button", {
        name: "Start from the simplest approximation, refine later",
      }),
    );
    const outputGroup = screen.getByRole("group", { name: "output form" });
    await user.click(within(outputGroup).getByRole("radio", { name: /Use output form/ }));
    await user.click(screen.getByRole("button", { name: "Answer and continue" }));

    await waitFor(() => expect(values.onRound).toHaveBeenCalledTimes(1));
    expect(values.onRound.mock.calls[0][1].answers.convention).toEqual({
      selected_option_ids: [],
      custom_text: "Keep the supplied phase convention.",
      strategy: "simplest_first",
    });
  });

  it("drops a chosen strategy as soon as an explicit option is selected", async () => {
    const user = userEvent.setup();
    const values = props({ initialSessionId: "intake-ui", onRound: stallingRound() });
    render(<RunStartForm {...values} />);

    const conventionGroup = await screen.findByRole("group", { name: "Fourier convention" });
    const strategyButton = within(conventionGroup).getByRole("button", {
      name: "Start from the simplest approximation, refine later",
    });
    await user.click(strategyButton);
    expect(strategyButton).toHaveAttribute("aria-pressed", "true");
    await user.click(within(conventionGroup).getByRole("radio", { name: /Use Fourier convention/ }));
    expect(strategyButton).toHaveAttribute("aria-pressed", "false");

    const outputGroup = screen.getByRole("group", { name: "output form" });
    await user.click(within(outputGroup).getByRole("radio", { name: /Use output form/ }));
    await user.click(screen.getByRole("button", { name: "Answer and continue" }));
    await waitFor(() => expect(values.onRound).toHaveBeenCalledTimes(1));
    expect(values.onRound.mock.calls[0][1].answers.convention).toEqual({
      selected_option_ids: ["minus"],
      custom_text: null,
    });
  });

  it("stops the grill, declares the defaults, and finalizes into the confirmation", async () => {
    const user = userEvent.setup();
    const values = props({ initialSessionId: "intake-ui", onRound: stallingRound() });
    render(<RunStartForm {...values} />);

    const conventionGroup = await screen.findByRole("group", { name: "Fourier convention" });
    await user.click(
      within(conventionGroup).getByRole("button", {
        name: "Start from the simplest approximation, refine later",
      }),
    );
    const outputGroup = screen.getByRole("group", { name: "output form" });
    await user.click(within(outputGroup).getByRole("radio", { name: /Use output form/ }));
    await user.click(screen.getByRole("button", { name: "Answer and continue" }));

    expect(
      await screen.findByRole("heading", {
        name: "Questioning stopped. You decide whether to start.",
      }),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Questioning stopped after the independent audit rejected the specification twice.",
      ),
    ).toBeVisible();
    expect(screen.getByText("Rounds used: 1 / 3")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Answer and continue" })).not.toBeInTheDocument();

    const pending = screen.getByRole("group", { name: "Included physics" });
    expect(
      within(pending).getByText("Auditor asked: absorption coefficient versus photon energy"),
    ).toBeVisible();
    expect(
      within(pending).getByText(/Including phonons changes the target quantity itself/),
    ).toBeVisible();

    // The stalled screen opens the two ladder panels instead of hiding them.
    const defaults = screen.getByText(/^Declared defaults \(2\)$/).closest("details");
    expect(defaults).toHaveAttribute("open");
    expect(within(defaults!).getByText("Conventions")).toBeVisible();
    expect(within(defaults!).getByText("Approximation levels")).toBeVisible();
    expect(within(defaults!).getByText("Instead of: Gaussian units")).toBeVisible();
    const ladder = screen.getByText(/^Refinement ladder \(2\)$/).closest("details");
    expect(ladder).toHaveAttribute("open");
    expect(within(ladder!).getByText("Baseline: textbook-simplest version")).toBeVisible();
    expect(within(ladder!).getByText("0 · Textbook-simplest baseline")).toBeVisible();
    expect(within(ladder!).getByText("Parallel branch")).toBeVisible();

    const startButton = screen.getByRole("button", { name: "Declare the defaults and start" });
    expect(startButton).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: "Direct transitions only" }));
    expect(startButton).toBeEnabled();
    await user.click(startButton);

    await waitFor(() => expect(values.onFinalize).toHaveBeenCalledTimes(1));
    expect(values.onFinalize).toHaveBeenCalledWith("intake-ui", {
      base_revision: 2,
      answers: { regime: { selected_option_ids: ["direct-only"], custom_text: null } },
    });

    expect(
      await screen.findByRole("heading", { name: "Confirm this derivation problem" }),
    ).toBeVisible();
    expect(
      screen.getByText(
        /Questioning stopped after the independent audit rejected the specification twice\. You declared the defaults and ended the questioning\./,
      ),
    ).toBeVisible();
    const finalizedDefaults = screen.getByText(/^Declared defaults \(3\)$/).closest("details");
    expect(
      within(finalizedDefaults!).getByText("Problem decisions the agent made for you"),
    ).toBeVisible();

    await user.click(screen.getByRole("checkbox", { name: /I confirm that the complete/ }));
    await user.click(screen.getByRole("button", { name: "Confirm and start derivation" }));
    await waitFor(() => expect(values.onConfirm).toHaveBeenCalledWith("intake-ui", 3));
    await waitFor(() => expect(values.onSubmit).toHaveBeenCalledTimes(1));
  });

  it("finalizes a capped session with no pending questions", async () => {
    const user = userEvent.setup();
    const stalled = fixtureIntakeSession("convergence_required", {
      withoutPending: true,
    });
    const values = props({
      initialSessionId: "intake-ui",
      onLoad: vi.fn().mockResolvedValue(stalled),
    });
    render(<RunStartForm {...values} />);

    expect(
      await screen.findByRole("heading", {
        name: "Questioning stopped. You decide whether to start.",
      }),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Answer whatever remains undecided, confirm the declared defaults, and start.",
      ),
    ).toBeVisible();
    expect(screen.getByText("Declared defaults on this rung: Unit system · Line shape")).toBeVisible();
    expect(screen.getByText("Decisions on this rung: output form")).toBeVisible();
    const startButton = screen.getByRole("button", {
      name: "Declare the defaults and start",
    });
    expect(startButton).toBeEnabled();
    await user.click(startButton);

    await waitFor(() => expect(values.onFinalize).toHaveBeenCalledTimes(1));
    expect(values.onFinalize).toHaveBeenCalledWith("intake-ui", {
      base_revision: 2,
      answers: {},
    });
  });

  it("puts back the answers a failed round kept, ready to submit again", async () => {
    const user = userEvent.setup();
    const held = fixtureIntakeSession("active", { heldSubmission: true });
    const values = props({
      initialSessionId: "intake-ui",
      onLoad: vi.fn().mockResolvedValue(held),
    });
    render(<RunStartForm {...values} />);

    const conventionGroup = await screen.findByRole("group", { name: "Fourier convention" });
    await waitFor(() =>
      expect(
        within(conventionGroup).getByRole("radio", { name: /Avoid Fourier convention/ }),
      ).toBeChecked(),
    );
    expect(within(conventionGroup).getByLabelText("Other / custom answer")).toHaveValue(
      "Use the retarded response.",
    );
    expect(
      screen.getByText(
        "The last attempt failed before it finished. Your answers were kept, so you only have to submit them again.",
      ),
    ).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Answer and continue" }));

    await waitFor(() => expect(values.onRound).toHaveBeenCalledTimes(1));
    expect(values.onRound.mock.calls[0][1]).toEqual({
      base_revision: 1,
      answers: {
        convention: {
          selected_option_ids: ["plus"],
          custom_text: "Use the retarded response.",
        },
        "output-form": { selected_option_ids: ["symbolic"], custom_text: null },
      },
      user_message: null,
    });
  });

  it("pre-fills nothing and says nothing when no submission is held", async () => {
    const values = props({ initialSessionId: "intake-ui" });
    render(<RunStartForm {...values} />);

    const conventionGroup = await screen.findByRole("group", { name: "Fourier convention" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Answer and continue" })).toBeDisabled(),
    );
    expect(
      within(conventionGroup).getByRole("radio", { name: /Use Fourier convention/ }),
    ).not.toBeChecked();
    expect(within(conventionGroup).getByLabelText("Other / custom answer")).toHaveValue("");
    expect(
      screen.queryByText(
        "The last attempt failed before it finished. Your answers were kept, so you only have to submit them again.",
      ),
    ).toBeNull();
  });

  it("reloads a session that moved after a failed round", async () => {
    const user = userEvent.setup();
    const active = fixtureIntakeSession();
    const moved = fixtureIntakeSession("candidate_ready");
    const onLoad = vi
      .fn()
      .mockResolvedValueOnce(active)
      .mockResolvedValueOnce(moved);
    const onRound = vi.fn().mockRejectedValue(conflict("The session changed."));
    const values = props({ initialSessionId: "intake-ui", onLoad, onRound });
    render(<RunStartForm {...values} />);

    await answerWholeFrontier(user);
    await user.click(screen.getByRole("button", { name: "Answer and continue" }));

    expect(await screen.findByText("The session changed.")).toBeVisible();
    expect(
      await screen.findByRole("heading", { name: "Confirm this derivation problem" }),
    ).toBeVisible();
    expect(onLoad).toHaveBeenCalledTimes(2);
    // The session left the answering phase, so re-sending the round is hopeless.
    expect(onRound).toHaveBeenCalledTimes(1);
  });

  it("recovers a stale revision and lands the round without a second click", async () => {
    const user = userEvent.setup();
    const active = fixtureIntakeSession();
    const moved = { ...fixtureIntakeSession(), revision: 7 };
    const onLoad = vi
      .fn()
      .mockResolvedValueOnce(active)
      .mockResolvedValue(moved);
    const onRound = vi
      .fn()
      .mockRejectedValueOnce(conflict("expected revision 7, received 1"))
      .mockResolvedValue(fixtureIntakeSession("candidate_ready"));
    const values = props({ initialSessionId: "intake-ui", onLoad, onRound });
    render(<RunStartForm {...values} />);

    await answerWholeFrontier(user);
    await user.click(screen.getByRole("button", { name: "Answer and continue" }));

    await waitFor(() => expect(onRound).toHaveBeenCalledTimes(2));
    expect(onRound.mock.calls[0][1].base_revision).toBe(1);
    expect(onRound.mock.calls[1][1]).toEqual({
      base_revision: 7,
      answers: {
        convention: { selected_option_ids: ["minus"], custom_text: null },
        "output-form": { selected_option_ids: ["full-chain"], custom_text: null },
      },
      user_message: null,
    });
    expect(
      await screen.findByRole("heading", { name: "Confirm this derivation problem" }),
    ).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("drops an answer whose decision closed and says which question it was", async () => {
    const user = userEvent.setup();
    const active = fixtureIntakeSession();
    const narrowed = {
      ...fixtureIntakeSession(),
      revision: 7,
      frontier: [fixtureIntakeFrontier[1]],
    };
    const onLoad = vi
      .fn()
      .mockResolvedValueOnce(active)
      .mockResolvedValue(narrowed);
    const onRound = vi
      .fn()
      .mockRejectedValueOnce(conflict("decision convention is not open"))
      .mockResolvedValue(fixtureIntakeSession("candidate_ready"));
    const values = props({ initialSessionId: "intake-ui", onLoad, onRound });
    render(<RunStartForm {...values} />);

    await answerWholeFrontier(user);
    await user.click(screen.getByRole("button", { name: "Answer and continue" }));

    await waitFor(() => expect(onRound).toHaveBeenCalledTimes(2));
    expect(Object.keys(onRound.mock.calls[1][1].answers)).toEqual(["output-form"]);
    expect(
      await screen.findByText(/those answers were dropped:.*Fourier convention/),
    ).toBeVisible();
  });

  it("does not claim a round failed while it is still running", async () => {
    // The journal row is written before the 50-90 s model call and served
    // throughout it. A reload mid-round finds one with no failure reason, and
    // telling that user their submission already failed invites a second
    // submission into the round that is still going.
    const running = fixtureIntakeSession("active", { heldSubmission: "running" });
    const values = props({
      initialSessionId: "intake-ui",
      onLoad: vi.fn().mockResolvedValue(running),
    });
    render(<RunStartForm {...values} />);

    const conventionGroup = await screen.findByRole("group", { name: "Fourier convention" });
    // The answers still come back: a process killed mid-round never writes a
    // reason either, and losing them is what the journal exists to prevent.
    await waitFor(() =>
      expect(
        within(conventionGroup).getByRole("radio", { name: /Avoid Fourier convention/ }),
      ).toBeChecked(),
    );
    expect(
      screen.queryByText(
        "The last attempt failed before it finished. Your answers were kept, so you only have to submit them again.",
      ),
    ).toBeNull();
  });

  it("reports a round that stays refused next to the button the user pressed", async () => {
    const user = userEvent.setup();
    const onLoad = vi.fn().mockResolvedValue(fixtureIntakeSession());
    const onRound = vi
      .fn()
      .mockRejectedValue(conflict("refinement ladder references undeclared defaults"));
    const values = props({ initialSessionId: "intake-ui", onLoad, onRound });
    render(<RunStartForm {...values} />);

    await answerWholeFrontier(user);
    const button = screen.getByRole("button", { name: "Answer and continue" });
    await user.click(button);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("refinement ladder references undeclared defaults");
    expect(alert.closest(".intake-frontier")).toBe(button.closest(".intake-frontier"));
    expect(alert.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).
      toBeTruthy();
    // The reload says the session never moved, so this refusal is the server's
    // verdict on this exact state, not a race. Re-sending would buy the same
    // error two model calls later.
    expect(onLoad).toHaveBeenCalledTimes(2);
    expect(onRound).toHaveBeenCalledTimes(1);
  });

  const typeCorrection = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(await screen.findByRole("button", { name: "Continue editing" }));
    await user.type(
      screen.getByLabelText("What should change?"),
      "Use the retarded response.",
    );
    await user.click(screen.getByRole("button", { name: "Submit changes" }));
  };

  it("re-sends a correction only when the session moved underneath it", async () => {
    const user = userEvent.setup();
    const moved = { ...fixtureIntakeSession("candidate_ready"), revision: 9 };
    const onLoad = vi
      .fn()
      .mockResolvedValueOnce(fixtureIntakeSession("candidate_ready"))
      .mockResolvedValue(moved);
    const onRound = vi
      .fn()
      .mockRejectedValueOnce(conflict("expected revision 9, received 2"))
      .mockResolvedValue(fixtureIntakeSession("candidate_ready"));
    const values = props({ initialSessionId: "intake-ui", onLoad, onRound });
    render(<RunStartForm {...values} />);

    await typeCorrection(user);

    await waitFor(() => expect(onRound).toHaveBeenCalledTimes(2));
    expect(onRound.mock.calls[1][1].base_revision).toBe(9);
  });

  it("does not re-send a correction the session refused outright", async () => {
    const user = userEvent.setup();
    const onLoad = vi.fn().mockResolvedValue(fixtureIntakeSession("candidate_ready"));
    const onRound = vi.fn().mockRejectedValue(conflict("the round could not be applied"));
    const values = props({ initialSessionId: "intake-ui", onLoad, onRound });
    render(<RunStartForm {...values} />);

    await typeCorrection(user);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "the round could not be applied",
    );
    // A correction round is two model calls too; a flat refusal is not retried.
    expect(onRound).toHaveBeenCalledTimes(1);
  });

  it("keeps the refusal visible when the recovery reload also fails", async () => {
    const user = userEvent.setup();
    const onLoad = vi
      .fn()
      .mockResolvedValueOnce(fixtureIntakeSession())
      .mockRejectedValue(new Error("Session reload failed"));
    const onRound = vi.fn().mockRejectedValue(conflict("expected revision 4, received 1"));
    const values = props({ initialSessionId: "intake-ui", onLoad, onRound });
    render(<RunStartForm {...values} />);

    await answerWholeFrontier(user);
    await user.click(screen.getByRole("button", { name: "Answer and continue" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "expected revision 4, received 1",
    );
    expect(screen.getByRole("status")).toHaveTextContent("Session reload failed");
    expect(onRound).toHaveBeenCalledTimes(1);
  });

  it("leaves a non-conflict failure alone instead of reloading the session", async () => {
    const user = userEvent.setup();
    const onLoad = vi.fn().mockResolvedValue(fixtureIntakeSession());
    const onRound = vi.fn().mockRejectedValue(new Error("Network unreachable"));
    const values = props({ initialSessionId: "intake-ui", onLoad, onRound });
    render(<RunStartForm {...values} />);

    await answerWholeFrontier(user);
    await user.click(screen.getByRole("button", { name: "Answer and continue" }));

    expect(await screen.findByText("Network unreachable")).toBeVisible();
    expect(onRound).toHaveBeenCalledTimes(1);
    expect(onLoad).toHaveBeenCalledTimes(1);
  });

  it("renders empty declared-default and ladder panels before the model produces them", async () => {
    const user = userEvent.setup();
    const values = props({ initialSessionId: "intake-ui" });
    render(<RunStartForm {...values} />);

    const defaults = (await screen.findByText(/^Declared defaults \(0\)$/)).closest("details");
    expect(defaults).not.toHaveAttribute("open");
    await user.click(within(defaults!).getByText(/^Declared defaults \(0\)$/));
    expect(within(defaults!).getByText("No default has been declared yet.")).toBeVisible();
    const ladder = screen.getByText(/^Refinement ladder \(0\)$/).closest("details");
    await user.click(within(ladder!).getByText(/^Refinement ladder \(0\)$/));
    expect(within(ladder!).getByText("No refinement ladder yet.")).toBeVisible();
  });

  it("records the ladder strategy in the decision log", async () => {
    const user = userEvent.setup();
    const values = props({
      initialSessionId: "intake-ui",
      onLoad: vi.fn().mockResolvedValue(fixtureIntakeSession("convergence_required")),
    });
    render(<RunStartForm {...values} />);

    await user.click(await screen.findByText("View decisions and conversation"));
    const log = screen.getByText("Decision log").closest("section");
    expect(within(log!).getAllByText(/Recorded answer: Simplest first/)).toHaveLength(2);
  });

  it("resumes a candidate by URL and confirms before creating a Run", async () => {
    const user = userEvent.setup();
    const candidate = fixtureIntakeSession("candidate_ready");
    const values = props({
      initialSessionId: "intake-ui",
      onLoad: vi.fn().mockResolvedValue(candidate),
    });
    render(<RunStartForm {...values} />);

    expect(await screen.findByRole("heading", { name: "Confirm this derivation problem" })).toBeVisible();
    expect(values.onLoad).toHaveBeenCalledWith("intake-ui");
    expect(screen.getByText("View the complete prompt organized by AI").closest("details")).not.toHaveAttribute("open");
    const trace = screen.getByText("View decisions and conversation").closest("details");
    expect(trace).not.toHaveAttribute("open");
    await user.click(screen.getByText("View decisions and conversation"));
    expect(screen.getByText("Decision log")).toBeVisible();
    expect(screen.getByText("Conversation archive")).toBeVisible();
    expect(screen.getByText("Derive the response tensor from the supplied model.")).toBeVisible();
    const startButton = screen.getByRole("button", { name: "Confirm and start derivation" });
    expect(startButton).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /I confirm that the complete/ }));
    await user.click(startButton);

    await waitFor(() => expect(values.onConfirm).toHaveBeenCalledWith("intake-ui", 2));
    await waitFor(() => expect(values.onSubmit).toHaveBeenCalledTimes(1));
    expect(values.onSubmit.mock.calls[0][0].problem).toMatchObject({
      problem_id: "intake-ui",
      confirmed_by_user: true,
      deliverable: "Canonical deliverable",
    });
  });

  it("discovers unfinished sessions, including stalled ones, without filling the main page", async () => {
    const user = userEvent.setup();
    const values = props({
      onListActive: vi
        .fn()
        .mockResolvedValue([fixtureIntakeSession("convergence_required")]),
    });
    render(<RunStartForm {...values} />);

    const summary = await screen.findByText("Resume an unfinished problem (1)");
    expect(summary.closest("details")).not.toHaveAttribute("open");
    await user.click(summary);
    const entry = screen.getByRole("button", { name: /Derive the bounded response tensor/ });
    expect(entry).toHaveTextContent("convergence_required");
    await user.click(entry);
    await waitFor(() => expect(values.onLoad).toHaveBeenCalledWith("intake-ui"));
  });

  it("retries Run creation from an already confirmed session without confirming again", async () => {
    const user = userEvent.setup();
    const confirmedSession = fixtureIntakeSession("confirmed");
    const values = props({
      initialSessionId: "intake-ui",
      onLoad: vi.fn().mockResolvedValue(confirmedSession),
    });
    render(<RunStartForm {...values} />);

    const startButton = await screen.findByRole("button", {
      name: "Start confirmed derivation",
    });
    expect(screen.queryByRole("checkbox", { name: /I confirm that the complete/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue editing" })).not.toBeInTheDocument();
    await user.click(startButton);

    await waitFor(() => expect(values.onSubmit).toHaveBeenCalledTimes(1));
    expect(values.onConfirm).not.toHaveBeenCalled();
    expect(values.onSubmit.mock.calls[0][0].problem.problem_id).toBe("intake-ui");
  });
});
