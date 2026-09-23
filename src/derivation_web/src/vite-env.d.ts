/// <reference types="vite/client" />

import type { ProductHost } from "./host";

declare global {
  interface Window {
    derivationLabHost?: ProductHost;
  }
}
