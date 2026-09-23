import { useEffect, useState } from "react";
import type { ProductHost } from "../host";
import type { DerivationRoute, ReportBundleView } from "../types";
import { ModalDialog } from "./ModalDialog";
import { useLocale } from "../i18n";

interface ReportExportDialogProps {
  route: DerivationRoute | undefined;
  exporting: boolean;
  result: ReportBundleView | null;
  error: string | null;
  host: ProductHost;
  pdfHref: string | null;
  onClose: () => void;
  onConfirm: () => Promise<void> | void;
}

export function ReportExportDialog({ route, exporting, result, error, host, pdfHref, onClose, onConfirm }: ReportExportDialogProps) {
  const { messages: m, locale } = useLocale();
  const [copied, setCopied] = useState(false);

  useEffect(() => setCopied(false), [result?.export_id]);
  if (!route) return null;

  const pdfPath = result?.status === "success" ? `${result.bundle_path}/report.pdf` : null;
  const failure = result?.manifest.failure;
  const code = failure && typeof failure === "object" && "code" in failure && typeof failure.code === "string"
    ? failure.code : "unknown";
  // Do not render arbitrary compiler messages or paths from the manifest.
  const failureMessages: Record<string, [string, string]> = {
    tectonic_runtime_unavailable: ["The PDF compiler is unavailable. Ask the administrator to repair it, then retry.", "PDF 编译器不可用。请联系管理员修复后重试。"],
    tectonic_version_mismatch: ["The PDF compiler version needs repair. Contact the administrator, then retry.", "PDF 编译器版本不匹配。请联系管理员修复后重试。"],
    tectonic_timeout: ["PDF compilation timed out. Retry; if it happens again, contact the administrator.", "PDF 编译超时。请重试；若再次发生，请联系管理员。"],
    tectonic_compile_failed: ["The report could not be compiled. Retry; if it fails again, contact the administrator with this error code.", "报告编译失败。请重试；若仍失败，请将此错误码提供给管理员。"],
    tectonic_output_limit: ["Compilation exceeded its output limit. Contact the administrator with this error code.", "编译输出超出限制。请将此错误码提供给管理员。"],
    tectonic_pdf_output_limit: ["The PDF exceeded its size limit. Contact the administrator with this error code.", "PDF 大小超出限制。请将此错误码提供给管理员。"],
    tectonic_missing_glyphs: ["The PDF font is missing required characters. Contact the administrator to repair the fonts, then retry.", "PDF 字体缺少所需字符。请联系管理员修复字体后重试。"],
  };
  const safeCode = Object.hasOwn(failureMessages, code) ? code : "report_export_failed";
  const failureMessage = failureMessages[safeCode]?.[locale === "zh-CN" ? 1 : 0]
    ?? (locale === "zh-CN" ? "PDF 导出失败。请重试；若仍失败，请联系管理员。" : "PDF export failed. Retry; if it fails again, contact the administrator.");
  return (
    <ModalDialog labelledBy="report-export-title" dismissible={!exporting} onClose={onClose}>
      <section className="report-dialog">
        <div className="dialog-heading">
          <div><p className="eyebrow">ReportBundle V1</p><h2 id="report-export-title">{m.reportTitle}</h2></div>
          <button type="button" className="icon-button" onClick={onClose} disabled={exporting} aria-label={m.closeReport} data-modal-initial-focus>×</button>
        </div>
        {!result && <>
          <p className="dialog-copy">{m.reportCopy}</p>
          <dl className="report-confirmation">
            <div><dt>{m.primaryRoute}</dt><dd>{route.label}</dd></div>
            <div><dt>{m.format}</dt><dd>{m.reportFormat}</dd></div>
            <div><dt>{m.compile}</dt><dd>{m.reportCompile}</dd></div>
          </dl>
          {error && <div className="error-banner" role="alert">{error}</div>}
          <div className="dialog-actions">
            <button type="button" className="quiet-button" onClick={onClose} disabled={exporting}>{m.cancel}</button>
            <button type="button" className="primary-button" onClick={() => void onConfirm()} disabled={exporting}>{exporting ? m.compiling : `${m.confirmExport} “${route.label}”`}</button>
          </div>
        </>}
        {result && <div className={`report-result ${result.status}`} aria-live="polite">
          <h3>{result.status === "success" ? m.exportSuccess : m.exportFailure}</h3>
          {result.status === "success" ? <p>{m.exportSuccessCopy}</p> : <div className="error-banner" role="alert"><p>{failureMessage}</p><code>{safeCode}</code></div>}
          {result.status === "success" && <code>{result.bundle_path}</code>}
          <div className="dialog-actions">
            {pdfPath && <button type="button" className="quiet-button" onClick={async () => { await host.copyText(pdfPath); setCopied(true); }}>{copied ? m.copied : m.copyPdfPath}</button>}
            {result.status === "failed" && <button type="button" className="primary-button" disabled={exporting} onClick={() => void onConfirm()}>{locale === "zh-CN" ? "重试 PDF 导出" : "Retry PDF export"}</button>}
            {pdfHref && <a className="quiet-button" href={pdfHref} download>{m.downloadPdf}</a>}
            {pdfPath && host.openArtifact && <button type="button" className="quiet-button" onClick={() => void host.openArtifact?.(pdfPath)}>{m.openPdf}</button>}
            <button type="button" className="primary-button" onClick={onClose}>{m.done}</button>
          </div>
        </div>}
      </section>
    </ModalDialog>
  );
}
