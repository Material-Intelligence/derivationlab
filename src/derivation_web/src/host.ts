import { allowedOpenAiHttpsUrl } from "./security/externalUrl";
import type { UiLocale } from "./i18n";

export interface ProductHost {
  environment: "web" | "electron-macos";
  copyText(value: string): Promise<void>;
  openArtifact?(repositoryRelativePath: string): Promise<void>;
  revealRunEvidence?(runId: string): Promise<void>;
  openExternal?(url: string): Promise<void>;
  restart?(): Promise<boolean>;
  requestQuit?(): Promise<boolean>;
  setUiLocale?(locale: UiLocale): Promise<void>;
  getRuntimeMetadata?(): Promise<{
    releaseId: string;
    version: string;
    buildNumber: string;
    productMode: "release";
    desktop: true;
  }>;
}

export function assertAllowedExternalUrl(value: string): URL {
  const url = allowedOpenAiHttpsUrl(value);
  if (!url) throw new Error("External URL is not an allowed OpenAI HTTPS address");
  return url;
}

export const browserHost: ProductHost = {
  environment: "web",
  async copyText(value) {
    await navigator.clipboard.writeText(value);
  },
  async openExternal(value) {
    const url = assertAllowedExternalUrl(value);
    window.open(url.toString(), "_blank", "noopener,noreferrer");
  },
};

export function resolveProductHost(): ProductHost {
  return window.derivationLabHost ?? browserHost;
}
