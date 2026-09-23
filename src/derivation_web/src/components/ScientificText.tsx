import { memo, useEffect, useMemo, useRef } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";
import "./ScientificText.css";

interface ScientificTextProps {
  value: string;
  className?: string;
}

const KATEX_OPTIONS = {
  errorColor: "#b42318",
  maxExpand: 1000,
  maxSize: 20,
  strict: "warn" as const,
  throwOnError: false,
  trust: false,
};

type PlainTransform = (value: string) => string;

interface HastNode {
  type?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

function rehypeFocusableScrollableMath() {
  return (tree: HastNode) => {
    const visit = (node: HastNode) => {
      const className = node.properties?.className;
      if (node.type === "element" && Array.isArray(className) && className.includes("katex-display")) {
        node.properties = { ...node.properties, tabIndex: 0 };
      }
      node.children?.forEach(visit);
    };
    visit(tree);
  };
}

function backtickRunLength(value: string, index: number): number {
  let end = index;
  while (value[end] === "`") end += 1;
  return end - index;
}

function findMatchingBackticks(value: string, start: number, length: number): number {
  let cursor = start;
  while (cursor < value.length) {
    const next = value.indexOf("`", cursor);
    if (next === -1) return -1;
    const runLength = backtickRunLength(value, next);
    if (runLength === length) return next;
    cursor = next + runLength;
  }
  return -1;
}

function mapOutsideInlineCode(value: string, transform: PlainTransform): string {
  let output = "";
  let plainStart = 0;
  let cursor = 0;

  while (cursor < value.length) {
    if (value[cursor] !== "`") {
      cursor += 1;
      continue;
    }
    const runLength = backtickRunLength(value, cursor);
    const closingIndex = findMatchingBackticks(value, cursor + runLength, runLength);
    if (closingIndex === -1) {
      cursor += runLength;
      continue;
    }
    output += transform(value.slice(plainStart, cursor));
    const closingEnd = closingIndex + runLength;
    output += value.slice(cursor, closingEnd);
    plainStart = closingEnd;
    cursor = closingEnd;
  }

  return output + transform(value.slice(plainStart));
}

function fenceAtLineStart(line: string): { marker: "`" | "~"; length: number } | null {
  const match = /^(?: {0,3})(`{3,}|~{3,})/.exec(line);
  if (!match) return null;
  return { marker: match[1][0] as "`" | "~", length: match[1].length };
}

function isClosingFence(line: string, marker: "`" | "~", length: number): boolean {
  const escapedMarker = marker === "`" ? "`" : "~";
  return new RegExp(`^(?: {0,3})${escapedMarker}{${length},}\\s*$`).test(line.replace(/\r?\n$/, ""));
}

/** Apply a transformation only to prose, never to inline or fenced code. */
export function mapScientificProse(value: string, transform: PlainTransform): string {
  const lines = value.match(/.*(?:\r?\n|$)/g)?.filter(Boolean) ?? [];
  let output = "";
  let proseBuffer = "";
  let fence: { marker: "`" | "~"; length: number } | null = null;

  const flushProse = () => {
    output += mapOutsideInlineCode(proseBuffer, transform);
    proseBuffer = "";
  };

  for (const line of lines) {
    if (fence) {
      output += line;
      if (isClosingFence(line, fence.marker, fence.length)) fence = null;
      continue;
    }

    const openingFence = fenceAtLineStart(line);
    if (openingFence) {
      flushProse();
      output += line;
      fence = openingFence;
      continue;
    }
    proseBuffer += line;
  }
  flushProse();
  return output;
}

function isEscaped(value: string, index: number): boolean {
  let precedingBackslashes = 0;
  for (let cursor = index - 1; cursor >= 0 && value[cursor] === "\\"; cursor -= 1) precedingBackslashes += 1;
  return precedingBackslashes % 2 === 1;
}

function findSlashMathClose(value: string, start: number, closing: ")" | "]"): number {
  for (let cursor = start; cursor < value.length - 1; cursor += 1) {
    if (value[cursor] === "\\" && value[cursor + 1] === closing && !isEscaped(value, cursor)) return cursor;
  }
  return -1;
}

function convertSlashMathInProse(value: string): string {
  let output = "";
  let cursor = 0;

  while (cursor < value.length - 1) {
    if (value[cursor] !== "\\" || isEscaped(value, cursor)) {
      output += value[cursor];
      cursor += 1;
      continue;
    }
    const opener = value[cursor + 1];
    if (opener !== "(" && opener !== "[") {
      output += value[cursor];
      cursor += 1;
      continue;
    }
    const display = opener === "[";
    const closeIndex = findSlashMathClose(value, cursor + 2, display ? "]" : ")");
    if (closeIndex === -1) {
      output += value[cursor];
      cursor += 1;
      continue;
    }
    const delimiter = display ? "$$" : "$";
    output += `${delimiter}${value.slice(cursor + 2, closeIndex)}${delimiter}`;
    cursor = closeIndex + 2;
  }

  return output + value.slice(cursor);
}

function findDisplayDollarClose(value: string, start: number): number {
  for (let cursor = start; cursor < value.length - 1; cursor += 1) {
    if (value[cursor] === "$" && value[cursor + 1] === "$" && !isEscaped(value, cursor)) return cursor;
  }
  return -1;
}

function normalizeDisplayDollarMath(value: string): string {
  let output = "";
  let cursor = 0;
  while (cursor < value.length - 1) {
    if (value[cursor] !== "$" || value[cursor + 1] !== "$" || isEscaped(value, cursor)) {
      output += value[cursor];
      cursor += 1;
      continue;
    }
    const closeIndex = findDisplayDollarClose(value, cursor + 2);
    if (closeIndex === -1) {
      output += value[cursor];
      cursor += 1;
      continue;
    }
    const formula = value.slice(cursor + 2, closeIndex).trim();
    output = `${output.replace(/[ \t]+$/, "").replace(/\n*$/, "")}\n\n$$\n${formula}\n$$\n\n`;
    cursor = closeIndex + 2;
    while (value[cursor] === " " || value[cursor] === "\t") cursor += 1;
  }
  return output + value.slice(cursor);
}

/**
 * The amount when `$` at `index` opens a currency figure rather than math, so
 * the same rule decides "escape this dollar" and "this dollar is not an
 * unbalanced math opener". Returns null when the dollar reads as a delimiter.
 */
function currencyAmountAt(value: string, index: number): string | null {
  if (value[index] !== "$" || value[index + 1] === "$" || isEscaped(value, index)) return null;
  const amount = /^\d+(?:,\d{3})*(?:\.\d{1,2})?/.exec(value.slice(index + 1))?.[0];
  if (!amount) return null;
  const end = index + 1 + amount.length;
  const following = value[end];
  const looksLikeMath = /^\s*[+\-*/=<>^_]/.test(value.slice(end));
  if (following === "$" || looksLikeMath) return null;
  return following === undefined || /[\s,.!?;:]/.test(following) ? amount : null;
}

function protectCurrencyDollars(value: string): string {
  let output = "";
  let cursor = 0;
  while (cursor < value.length) {
    const amount = currencyAmountAt(value, cursor);
    if (amount === null) {
      output += value[cursor];
      cursor += 1;
      continue;
    }
    output += `\\$${amount}`;
    cursor += 1 + amount.length;
  }
  return output;
}

/**
 * Return a Markdown-compatible view of immutable Record text. Existing dollar
 * delimiters are preserved; canonical slash delimiters are converted only in
 * prose, never inside inline or fenced code.
 */
export function scientificTextToMarkdown(value: string): string {
  return mapScientificProse(value, (segment) => normalizeDisplayDollarMath(convertSlashMathInProse(protectCurrencyDollars(segment))));
}

export function scientificTextPlainSummary(value: string, maxLength = 180): string {
  const plain = mapScientificProse(scientificTextToMarkdown(value), (segment) =>
    segment
      .replace(/\$\$([\s\S]*?)\$\$/g, "$1")
      .replace(/\$([^$\n]+)\$/g, "$1")
      .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/(^|\s)[#>*_~]+/g, "$1")
      .replace(/[`*_~]/g, ""),
  )
    .replace(/\s+/g, " ")
    .trim();
  if (plain.length <= maxLength) return plain;
  return `${plain.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`;
}

/*
 * The backend truncates `StepView.title` to 120 characters, which regularly cuts
 * a title in the middle of a formula and leaves an opening `\[`, `\(`, `$$` or
 * `$` with no partner. Rendering that either dumps raw TeX into the reading desk
 * or feeds KaTeX a parse error, so titles are balanced before anything else.
 */
const PROSE_MARK = "\u0001";

/** Same length as `value`, with every character `mapScientificProse` treats as prose marked. */
function proseMask(value: string): string {
  return mapScientificProse(value, (segment) => segment.replace(/[\s\S]/g, PROSE_MARK));
}

function findUnescapedInProse(value: string, mask: string, token: string, from: number): number {
  for (let cursor = from; cursor + token.length <= value.length; cursor += 1) {
    if (mask[cursor] !== PROSE_MARK) continue;
    if (!value.startsWith(token, cursor)) continue;
    if (isEscaped(value, cursor)) continue;
    return cursor;
  }
  return -1;
}

function truncateBefore(value: string, index: number): string {
  const head = value.slice(0, index);
  const trimmed = head.replace(/\s+$/, "");
  if (!trimmed) return "…";
  return /\s$/.test(head) ? `${trimmed} …` : `${trimmed}…`;
}

/**
 * Drop everything from the first math delimiter that is never closed, so a
 * server-truncated title ends in prose plus an ellipsis instead of exposing half
 * a formula. Code spans and fenced code are excluded exactly as
 * `mapScientificProse` excludes them, `\$` stays an escaped dollar, and a
 * currency figure (`$5`) is not mistaken for an opener.
 */
export function trimUnbalancedMath(value: string): string {
  const mask = proseMask(value);
  let cursor = 0;
  while (cursor < value.length) {
    if (mask[cursor] !== PROSE_MARK) {
      cursor += 1;
      continue;
    }
    const character = value[cursor];
    const slashOpener = character === "\\" && !isEscaped(value, cursor) ? value[cursor + 1] : undefined;
    if (slashOpener === "(" || slashOpener === "[") {
      const closer = slashOpener === "[" ? "\\]" : "\\)";
      const closeIndex = findUnescapedInProse(value, mask, closer, cursor + 2);
      if (closeIndex === -1) return truncateBefore(value, cursor);
      cursor = closeIndex + closer.length;
      continue;
    }
    if (character === "$" && !isEscaped(value, cursor) && currencyAmountAt(value, cursor) === null) {
      const delimiter = value[cursor + 1] === "$" ? "$$" : "$";
      const closeIndex = findUnescapedInProse(value, mask, delimiter, cursor + delimiter.length);
      if (closeIndex === -1) return truncateBefore(value, cursor);
      cursor = closeIndex + delimiter.length;
      continue;
    }
    cursor += 1;
  }
  return value;
}

/** Macros with a readable single-character equivalent. */
const TEX_SYMBOLS: Readonly<Record<string, string>> = {
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", varepsilon: "ε", zeta: "ζ", eta: "η",
  theta: "θ", vartheta: "ϑ", iota: "ι", kappa: "κ", lambda: "λ", mu: "μ", nu: "ν", xi: "ξ",
  pi: "π", varpi: "ϖ", rho: "ρ", varrho: "ϱ", sigma: "σ", varsigma: "ς", tau: "τ", upsilon: "υ",
  phi: "φ", varphi: "φ", chi: "χ", psi: "ψ", omega: "ω",
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π", Sigma: "Σ", Upsilon: "Υ",
  Phi: "Φ", Psi: "Ψ", Omega: "Ω",
  partial: "∂", equiv: "≡", sum: "Σ", prod: "Π", int: "∫", infty: "∞", pm: "±", mp: "∓",
  times: "×", cdot: "·", to: "→", rightarrow: "→", leftarrow: "←", hbar: "ħ",
  langle: "⟨", rangle: "⟩", le: "≤", leq: "≤", ge: "≥", geq: "≥", neq: "≠", ne: "≠",
  approx: "≈", propto: "∝", nabla: "∇", dagger: "†", ast: "∗", in: "∈",
};

/**
 * Render *closed* TeX source as plain text for the surfaces that cannot host
 * KaTeX: tree nodes (SVG text), breadcrumbs, fork options, the sidebar,
 * context-menu labels and "copy title". A tree node reading `Cool the rod
 * from T_0 to ice temperature` is a title; `Cool the rod from T_{\mathrm 0}
 * to ice tem…` is source code that happens to be displayed.
 *
 * Unknown macros lose the command and keep their arguments, so an unmapped
 * `\foo{bar}` degrades to `bar` rather than to nothing.
 */
export function texToReadable(tex: string): string {
  let output = tex
    // `\\` is a line break, and the spacing macros carry no meaning in plain text.
    .replace(/\\\\/g, " ")
    .replace(/\\(?:left|right)\b\s*/g, "")
    .replace(/\\(?:qquad|quad)\b/g, " ")
    .replace(/\\[,;!:]/g, "");

  for (let pass = 0; pass < 8; pass += 1) {
    const next = output.replace(/\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, "$1/$2");
    if (next === output) break;
    output = next;
  }

  output = output
    // One left-to-right pass, so a dropped command can never fuse with the
    // letters behind it (`\cdot\vec{v}` must not become the macro `\cdotv`).
    // Styling wrappers (`\mathbf`, `\mathrm`, `\hat`, `\vec`, `\boldsymbol`, …)
    // are unmapped on purpose: dropping the command keeps exactly the argument.
    // A control word swallows the whitespace that terminates it, as TeX does.
    .replace(/\\([A-Za-z]+)\s*/g, (_match, name: string) => TEX_SYMBOLS[name] ?? "")
    .replace(/\\([^A-Za-z])/g, "$1");

  for (let pass = 0; pass < 8; pass += 1) {
    const next = output.replace(/([_^])\{([^{}]*)\}/g, "$1$2");
    if (next === output) break;
    output = next;
  }

  return output.replace(/[{}]/g, "").replace(/\s+/g, " ").trim();
}

/**
 * Title formulas are kept as (truncated) TeX source rather than replaced by a
 * `[formula]` placeholder: two sibling steps whose titles differ only inside the
 * formula are indistinguishable once the formula is erased, which is exactly the
 * case a branch picker has to disambiguate.
 *
 * Placeholders use private-use code points so the Markdown stripper in
 * `scientificTextPlainSummary` cannot mangle TeX (`_`, `*`, `~`, `#` are all
 * common in formulas and all stripped as Markdown syntax).
 */
const FORMULA_SLOT_OPEN = "\uE000";
const FORMULA_SLOT_CLOSE = "\uE001";
const DISPLAY_TITLE_FORMULA_LENGTH = 40;

function compactFormulaSource(source: string, maxLength?: number): string {
  const compact = source.replace(/\s+/g, " ").trim();
  if (maxLength === undefined || compact.length <= maxLength) return compact;
  return `${compact.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`;
}

function captureTitleFormulas(value: string, sink: string[]): string {
  const slot = (source: string, maxLength?: number) => {
    sink.push(compactFormulaSource(texToReadable(source), maxLength));
    return `${FORMULA_SLOT_OPEN}${sink.length - 1}${FORMULA_SLOT_CLOSE}`;
  };
  let compact = value
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, body: string) => slot(body, DISPLAY_TITLE_FORMULA_LENGTH))
    .replace(/\$\$([\s\S]*?)\$\$/g, (_match, body: string) => slot(body, DISPLAY_TITLE_FORMULA_LENGTH))
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, body: string) => slot(body))
    .replace(/(?<!\\)\$([^$\n]+?)(?<!\\)\$/g, (_match, body: string) => slot(body));
  // Safety net only: `trimUnbalancedMath` has already removed unbalanced
  // delimiters, but the capture regexes above are narrower than that scan
  // (`$…$` never spans a newline), so an odd delimiter can still reach here.
  const residual = [
    { token: "\\[", maxLength: DISPLAY_TITLE_FORMULA_LENGTH },
    { token: "$$", maxLength: DISPLAY_TITLE_FORMULA_LENGTH },
    { token: "\\(", maxLength: undefined },
    { token: "$", maxLength: undefined },
  ]
    .map((item) => ({ ...item, index: compact.indexOf(item.token) }))
    .filter((item) => item.index >= 0)
    .sort((left, right) => left.index - right.index)[0];
  if (residual) {
    const source = compact.slice(residual.index + residual.token.length);
    compact = compact.slice(0, residual.index) + slot(source, residual.maxLength);
  }
  return compact;
}

function restoreTitleFormulas(value: string, formulas: readonly string[]): string {
  return value.replace(
    new RegExp(`${FORMULA_SLOT_OPEN}(\\d+)${FORMULA_SLOT_CLOSE}`, "g"),
    (_match, index: string) => formulas[Number(index)] ?? "",
  );
}

/** Plain, bounded UI heading that never exposes Record TeX delimiters. */
export function scientificTextPlainTitle(value: string, maxLength = 132): string {
  const formulas: string[] = [];
  const withSlots = mapScientificProse(trimUnbalancedMath(value), (segment) => captureTitleFormulas(segment, formulas));
  const plain = restoreTitleFormulas(scientificTextPlainSummary(withSlots, Number.MAX_SAFE_INTEGER), formulas)
    .replace(/\s+/g, " ")
    .trim();
  if (plain.length <= maxLength) return plain;
  return `${plain.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`;
}

/**
 * Markdown for a one-line heading: display math is folded to inline math so a
 * title never opens a centred `.katex-display` block inside a heading.
 */
export function scientificTextTitleMarkdown(value: string): string {
  return mapScientificProse(trimUnbalancedMath(value), (segment) =>
    convertSlashMathInProse(protectCurrencyDollars(segment)).replace(
      /\$\$([\s\S]*?)\$\$/g,
      (_match, body: string) => `$${String(body).replace(/\s+/g, " ").trim()}$`,
    ),
  )
    .replace(/\s*\r?\n\s*/g, " ")
    .trim();
}

function safeMarkdownUrl(url: string): string {
  const trimmed = url.trim();
  if (trimmed.startsWith("//")) return "";
  if (/^(?:#|\/(?!\/)|\.\/|\.\.\/)/.test(trimmed)) return trimmed;
  try {
    const protocol = new URL(trimmed).protocol;
    return protocol === "http:" || protocol === "https:" || protocol === "mailto:" ? trimmed : "";
  } catch {
    return "";
  }
}

/**
 * macOS hides overlay scrollbars, so a `.katex-display` that scrolls sideways
 * looks like a formula that simply ends. `data-overflow` lets the stylesheet
 * fade the right edge, which is the only signal a reader gets that the
 * derivation continues past the pane.
 */
function useFormulaOverflowFlags(markdown: string) {
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const root = rootRef.current;
    if (!root || typeof ResizeObserver === "undefined") return;
    const blocks = [...root.querySelectorAll<HTMLElement>(".katex-display")];
    const measure = () => {
      for (const block of blocks) block.dataset.overflow = block.scrollWidth - block.clientWidth > 1 ? "true" : "false";
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    for (const block of blocks) observer.observe(block);
    return () => observer.disconnect();
  }, [markdown]);
  return rootRef;
}

function ScientificTextView({ value, className = "" }: ScientificTextProps) {
  const markdown = useMemo(() => scientificTextToMarkdown(value), [value]);
  const rootRef = useFormulaOverflowFlags(markdown);
  return (
    <div className={`scientific-text ${className}`.trim()} ref={rootRef}>
      <ReactMarkdown
        disallowedElements={["img"]}
        rehypePlugins={[[rehypeKatex, KATEX_OPTIONS], rehypeFocusableScrollableMath]}
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: true }]]}
        skipHtml
        unwrapDisallowed
        urlTransform={safeMarkdownUrl}
        components={{
          a: ({ children, href, ...props }) => href
            ? <a {...props} href={href} rel="noreferrer noopener">{children}</a>
            : <span className="unsafe-link-text">{children}</span>,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}

const INLINE_TITLE_DISALLOWED = ["img", "p", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "blockquote", "ul", "ol", "li", "table", "pre", "br"];

/**
 * Inline KaTeX rendering for a step title. Tree nodes stay on
 * `scientificTextPlainTitle` (SVG text cannot host KaTeX markup); the reading
 * desk uses this so `$\psi_n(x)$` reads as a formula, not as source.
 */
function ScientificInlineTitleView({ value, className = "" }: ScientificTextProps) {
  const markdown = useMemo(() => scientificTextTitleMarkdown(value), [value]);
  return (
    <span className={`scientific-inline-title ${className}`.trim()}>
      <ReactMarkdown
        disallowedElements={INLINE_TITLE_DISALLOWED}
        rehypePlugins={[[rehypeKatex, KATEX_OPTIONS]]}
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: true }]]}
        skipHtml
        unwrapDisallowed
        urlTransform={safeMarkdownUrl}
        components={{
          a: ({ children, href, ...props }) => href
            ? <a {...props} href={href} rel="noreferrer noopener">{children}</a>
            : <span className="unsafe-link-text">{children}</span>,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </span>
  );
}

/*
 * Markdown parsing plus KaTeX typesetting is the most expensive work the reading
 * desk does, and every SSE snapshot replaces the whole `RunView`. Both renderers
 * take a single immutable string, so the default shallow comparison is exactly
 * right: identical Record text is never re-typeset.
 */
export const ScientificText = memo(ScientificTextView);
export const ScientificInlineTitle = memo(ScientificInlineTitleView);
