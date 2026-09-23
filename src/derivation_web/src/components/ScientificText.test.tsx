import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DerivationRoute, DerivationStep } from "../types";
import { DetailsDrawer } from "./DetailsDrawer";
import { RouteReader } from "./RouteReader";
import { ScientificInlineTitle, ScientificText, scientificTextPlainSummary, scientificTextPlainTitle, scientificTextTitleMarkdown, scientificTextToMarkdown, texToReadable, trimUnbalancedMath } from "./ScientificText";

function makeStep(overrides: Partial<DerivationStep> = {}): DerivationStep {
  return {
    id: "step-1",
    revisionId: "rev-1",
    order: 0,
    title: "First step",
    status: "sealed",
    content: {
      claim: "**Claim** with \\(x\\)",
      why: "Why \\(y\\)",
      source: "Source \\(z\\)",
      derivation: "Derive \\[q=1\\]",
      scope: "Scope \\(s\\)",
    },
    output_sha256: null,
    input: "input",
    reasoningSummary: "Short **summary** with \\(a+b\\)",
    output: "Full output with \\[c=d\\]",
    checks: { schema: "passed", physics: "passed", provenance: "passed" },
    provenance: null,
    branch_id: "branch-1",
    ...overrides,
  };
}

describe("ScientificText", () => {
  it.each([String.raw`\boxed{x}`, String.raw`x\cdots y`, String.raw`{\bf x}`, String.raw`{\rm x}`, String.raw`{\cal H}`])("renders conventional legacy TeX %s", (formula) => {
    const { container } = render(<ScientificText value={`$${formula}$`} />);
    expect(container.querySelectorAll(".katex")).toHaveLength(1);
    expect(container.querySelector(".katex-error")).not.toBeInTheDocument();
  });

  it.each([String.raw`\unknownmacro{x}`, String.raw`\input{/etc/passwd}`])("preserves unsupported TeX source without executing it: %s", (formula) => {
    const { container } = render(<ScientificText value={`$${formula}$`} />);
    expect(container.querySelector("annotation")?.textContent).toBe(formula);
    expect(container).toHaveTextContent(formula.split("{")[0]);
    expect(container.querySelector("script,iframe,img,a")).not.toBeInTheDocument();
  });

  it("keeps a control-character formula visible as a parse error", () => {
    const formula = "x\u0007y";
    const { container } = render(<ScientificText value={`$${formula}$`} />);
    expect(container.querySelector(".katex-error")?.textContent).toBe(formula);
  });

  it("renders all four supported TeX delimiter forms", () => {
    const { container } = render(
      <ScientificText value={"Dollar $a+b$ and slash \\(c+d\\).\n\n$$e=f$$\n\n\\[g=h\\]"} />,
    );

    expect(container.querySelectorAll(".katex")).toHaveLength(4);
    expect(container.querySelectorAll(".katex-display")).toHaveLength(2);
    expect(container.querySelectorAll(".katex-display[tabindex='0']")).toHaveLength(2);
    expect(screen.getByText(/Dollar/)).toBeInTheDocument();
  });

  it("renders safe Markdown while treating code as literal and dropping raw HTML", () => {
    const { container } = render(
      <ScientificText value={"**Bold**\n\n- first\n- second\n\n`\\(not math\\)`\n\n```tex\n\\[also not math\\]\n```\n\n<script>window.bad = true</script>\n\nAfter"} />,
    );

    expect(screen.getByText("Bold").tagName).toBe("STRONG");
    expect(screen.getByText("first").closest("li")).toBeInTheDocument();
    expect(screen.getByText("\\(not math\\)").tagName).toBe("CODE");
    expect(screen.getByText("\\[also not math\\]").tagName).toBe("CODE");
    expect(container.querySelector("script")).not.toBeInTheDocument();
    expect(screen.queryByText(/window\.bad/)).not.toBeInTheDocument();
    expect(screen.getByText("After")).toBeInTheDocument();
    expect(container.querySelector(".katex")).not.toBeInTheDocument();
  });

  it("keeps malformed TeX visible without breaking valid sibling formulas", () => {
    const { container } = render(<ScientificText value={"Valid $x^2$ then broken \\(\\frac{\\) and valid $y^2$."} />);

    expect(container.querySelectorAll(".katex")).toHaveLength(2);
    const error = container.querySelector(".katex-error");
    expect(error).toBeInTheDocument();
    expect(error).toHaveTextContent("\\frac{");
    expect(error).toHaveAttribute("title", expect.stringContaining("KaTeX parse error"));
  });

  it("blocks unsafe Markdown URLs and untrusted KaTeX commands", () => {
    const { container } = render(
      <ScientificText value={"[unsafe](javascript:alert(1)), [protocol-relative](//evil.example), and [safe](https://example.com). Formula $\\href{javascript:alert(1)}{click}$."} />,
    );

    expect(screen.getByRole("link", { name: "safe" })).toHaveAttribute("href", "https://example.com");
    expect(screen.getByText("unsafe").closest("a")).toBeNull();
    expect(screen.getByText("protocol-relative").closest("a")).toBeNull();
    expect(container.querySelector("a[href^='javascript:']")).not.toBeInTheDocument();
    expect(container.querySelector(".katex a")).not.toBeInTheDocument();
  });

  it("converts slash delimiters for Markdown export without touching code", () => {
    const source = "Text \\(x\\) and \\[y\\]. `\\(code\\)`\n\n```tex\n\\[block code\\]\n```";

    expect(scientificTextToMarkdown(source)).toBe("Text $x$ and\n\n$$\ny\n$$\n\n. `\\(code\\)`\n\n```tex\n\\[block code\\]\n```");
    expect(scientificTextPlainSummary("**Long** \\(x+y\\)", 80)).toBe("Long x+y");
    expect(scientificTextPlainTitle("Derivative of \\(x+y\\) at the origin")).toBe("Derivative of x+y at the origin");
    expect(scientificTextPlainTitle("Truncated block \\[\\partial_{x_0}")).toBe("Truncated block …");
  });

  it("keeps title formulas as truncated source instead of a [formula] placeholder", () => {
    expect(scientificTextPlainTitle("Split $\\psi_{n}(x)$ into even and odd parts")).toBe("Split ψ_n(x) into even and odd parts");
    expect(scientificTextPlainTitle("Levels $$E_n=\\hbar\\omega\\left(n+\\frac{1}{2}\\right),\\quad \\psi_n\\propto H_n(\\xi)e^{-\\xi^2/2}$$"))
      .toBe("Levels E_n=ħω(n+1/2), ψ_n∝H_n(ξ)e^-ξ^2/2");
    expect(scientificTextPlainTitle("Two siblings $A_1$ and $A_2$")).toBe("Two siblings A_1 and A_2");
  });

  it("renders a step title inline, folding display math into the line", () => {
    const { container } = render(<ScientificInlineTitle value={"Split \\[\\psi_{n}\\] here"} />);

    expect(container.querySelectorAll(".katex")).toHaveLength(1);
    expect(container.querySelector(".katex-display")).not.toBeInTheDocument();
    expect(container.querySelector("p")).not.toBeInTheDocument();
    expect(container).toHaveTextContent("Split");
  });

  it("preserves currency dollars without disabling common dollar-delimited math", () => {
    const { container } = render(
      <ScientificText value={"The fee was $5 and later became $10. Inline math $x=5$ and $5 + x$ remain math."} />,
    );

    expect(container.querySelectorAll(".katex")).toHaveLength(2);
    expect(container).toHaveTextContent("The fee was $5 and later became $10.");
    expect(scientificTextToMarkdown("Price is $5 and cost $10.")).toBe("Price is \\$5 and cost \\$10.");
  });

  it("keeps the native browser context menu on scientific prose and links", () => {
    render(<ScientificText value={"Readable prose with a [source](https://example.com)."} />);
    const prose = screen.getByText(/Readable prose/).closest("p");
    const link = screen.getByRole("link", { name: "source" });
    const proseEvent = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    const linkEvent = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });

    prose?.dispatchEvent(proseEvent);
    link.dispatchEvent(linkEvent);
    expect(proseEvent.defaultPrevented).toBe(false);
    expect(linkEvent.defaultPrevented).toBe(false);
  });
});

describe("ScientificText integration", () => {
  it("renders every route step as prose and never repeats the summary", () => {
    const first = makeStep();
    const second = makeStep({ id: "step-2", revisionId: "rev-2", title: "Second step", reasoningSummary: "Other \\(b\\)", output: "Other output" });
    const route: DerivationRoute = {
      id: "route-1",
      label: "Route 1",
      nodeIds: [first.id, second.id],
      status: "complete",
      branch_id: "branch-1",
      status_history: [{ seq: 1, status: "completed" }],
    };
    const { container } = render(
      <RouteReader
        route={route}
        steps={[first, second]}
        selectedNodeId={first.id}
        branchableStepRevisionIds={[first.revisionId, second.revisionId]}
        onSelectStep={() => undefined}
        onOpenDetails={() => undefined}
        onOpenBranch={() => undefined}
        onCopyText={() => undefined}
      />,
    );

    const rows = container.querySelectorAll(".route-step");
    expect(rows).toHaveLength(2);
    // Continuous document: the unselected step renders its prose and formulas too.
    expect(rows[0].querySelectorAll(".scientific-text .katex").length).toBeGreaterThan(0);
    expect(rows[1].querySelectorAll(".scientific-text .katex").length).toBeGreaterThan(0);
    // The old collapsed <small> duplicated the body; the summary must appear once.
    expect((rows[0].textContent ?? "").match(/Short summary with/g)).toHaveLength(1);
    expect(within(rows[1] as HTMLElement).getByText(/Other output/)).toBeInTheDocument();
    expect(container.querySelector(".timeline-select")).not.toBeInTheDocument();
  });

  it("renders all five detail fields through ScientificText", () => {
    const { container } = render(<DetailsDrawer step={makeStep()} onClose={() => undefined} />);

    expect(container.querySelectorAll(".details-drawer section .scientific-text")).toHaveLength(5);
    expect(container.querySelectorAll(".details-drawer section .katex")).toHaveLength(5);
    expect(screen.getByText("Claim").tagName).toBe("STRONG");
  });

  it("labels active and failed routes without presenting them as complete", () => {
    const step = makeStep();
    const baseRoute: DerivationRoute = {
      id: "route-1",
      label: "Route 1",
      nodeIds: [step.id],
      status: "active",
      branch_id: "branch-1",
      status_history: [{ seq: 1, status: "active" }],
    };
    const props = {
      steps: [step],
      selectedNodeId: step.id,
      branchableStepRevisionIds: [step.revisionId],
      onSelectStep: () => undefined,
      onOpenDetails: () => undefined,
      onOpenBranch: () => undefined,
      onCopyText: () => undefined,
    };
    const { rerender } = render(<RouteReader {...props} route={baseRoute} />);
    expect(screen.getByText("Active")).toBeInTheDocument();

    rerender(<RouteReader {...props} route={{ ...baseRoute, status: "failed" }} />);
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.queryByText("Complete")).not.toBeInTheDocument();
  });
});

/*
 * Server-truncated titles. The backend truncates `StepView.title` to 120
 * characters, so a production title regularly ends inside a formula. The source
 * strings below reproduce the shapes such titles take (a cut inside display
 * math, inside inline math, right after a hyphen, and after several closed
 * formulas) on a textbook subject: heat conduction along a finite rod.
 */
const TRUNCATED_TITLES: [string, string][] = [
  [
    "Separate variables for a rod of length \\(L\\) whose ends are held at zero temperature, and write the general solution as \\[u(x,t)=\\sum_{k\\ge1}b_k\\,e^{-t/\\tau_k}...",
    "Separate variables for a rod of length L whose ends are held at zero temperature, and write the general solution as …",
  ],
  [
    "A thin copper rod starts at a uniform \\(T_0\\) and is plunged into ice water; the temperature at its midpoint is then \\(u(L/2,t)\\approx\\tfrac{4T_0}{\\pi}e^{...",
    "A thin copper rod starts at a uniform T_0 and is plunged into ice water; the temperature at its midpoint is then …",
  ],
  [
    "Only the slowest mode survives once \\(t>\\tau_2\\), which is why a single exponential fits the measured cooling curve in the one-\\(k...",
    "Only the slowest mode survives once t>τ_2, which is why a single exponential fits the measured cooling curve in the one-…",
  ],
  [
    "With \\(u(0,t)=u(L,t)=0\\) and \\(u(x,0)=f(x)\\), project \\(f\\) onto the modes \\(\\phi_k\\) and read off \\(b_k\\) and \\(\\tau_k=L^2/(\\pi^2k^2D)\\) from \\(\\int_0^L...",
    "With u(0,t)=u(L,t)=0 and u(x,0)=f(x), project f onto the modes φ_k and read off b_k and τ_k=L^2/(π^2k^2D) from …",
  ],
  [
    "Rescale with $s=x/L$ and $\\theta=u/T_0$ so the rod equation loses its constants: $$\\partial_\\sigma\\theta=\\partial_s^2\\theta,\\qquad\\theta(0,...",
    "Rescale with s=x/L and θ=u/T_0 so the rod equation loses its constants: …",
  ],
];

describe("server-truncated titles", () => {
  it.each(TRUNCATED_TITLES)("reads %#: as plain prose", (title, expected) => {
    expect(scientificTextPlainTitle(title)).toBe(expected);
  });

  it("never leaves an unbalanced delimiter for the inline renderer", () => {
    for (const [title] of TRUNCATED_TITLES) {
      const markdown = scientificTextTitleMarkdown(title);
      expect(markdown.split("$").length % 2).toBe(1);
      expect(markdown).not.toContain("\\(");
      expect(markdown).not.toContain("\\[");
    }
  });

  it("renders a truncated title without KaTeX errors or raw source", () => {
    const { container } = render(<ScientificInlineTitle value={TRUNCATED_TITLES[2][0]} />);

    expect(container.querySelector(".katex-error")).not.toBeInTheDocument();
    expect(container.querySelectorAll(".katex")).toHaveLength(1);
    expect(container).toHaveTextContent(/measured cooling curve in the one-…$/);
  });
});

describe("trimUnbalancedMath", () => {
  it("keeps balanced titles untouched", () => {
    expect(trimUnbalancedMath("Balanced \\(a+b\\) and $c$ and \\[d\\]")).toBe("Balanced \\(a+b\\) and $c$ and \\[d\\]");
  });

  it("cuts at the last unclosed opener and keeps the closed prefix", () => {
    expect(trimUnbalancedMath("Closed \\(a\\) then open $$b")).toBe("Closed \\(a\\) then open …");
    expect(trimUnbalancedMath("No space before-\\(x")).toBe("No space before-…");
  });

  it("ignores delimiters inside code and escaped or currency dollars", () => {
    expect(trimUnbalancedMath("Literal `\\(not math` stays")).toBe("Literal `\\(not math` stays");
    expect(trimUnbalancedMath("Costs $5 to run")).toBe("Costs $5 to run");
    expect(trimUnbalancedMath("Escaped \\$ alone")).toBe("Escaped \\$ alone");
  });
});

describe("texToReadable", () => {
  it("maps Greek letters, operators and relations", () => {
    expect(texToReadable("\\alpha_{n}(t)")).toBe("α_n(t)");
    expect(texToReadable("\\sum_n \\int \\hbar\\omega \\to \\infty")).toBe("Σ_n ∫ħω→∞");
    expect(texToReadable("a \\le b \\ge c \\neq d \\approx e \\pm f \\times g \\cdot h")).toBe("a ≤b ≥c ≠d ≈e ±f ×g ·h");
  });

  it("unwraps styling macros with and without braces", () => {
    expect(texToReadable("\\mathbf{r}")).toBe("r");
    expect(texToReadable("\\mathbf r")).toBe("r");
    expect(texToReadable("\\mathbf0")).toBe("0");
    expect(texToReadable("\\hat{\\mathbf n}\\cdot\\vec{v}")).toBe("n·v");
    expect(texToReadable("\\mathrm{d}\\boldsymbol{x}")).toBe("dx");
  });

  it("flattens fractions, spacing macros and brace groups", () => {
    expect(texToReadable("\\frac{1}{2m}")).toBe("1/2m");
    expect(texToReadable("\\left(\\partial_{x_i}u\\right)\\,\\quad X")).toBe("(∂_x_iu) X");
    expect(texToReadable("c_k^{*}")).toBe("c_k^*");
  });

  it("drops an unknown command but keeps its argument", () => {
    expect(texToReadable("\\unknownmacro{payload}")).toBe("payload");
  });
});
