import { defineConfig } from "@hey-api/openapi-ts";

export default defineConfig({
  input: "../derivation_api/openapi.json",
  output: {
    path: process.env.DERIVATION_API_GENERATED_DIR ?? "src/api/generated",
    clean: true,
  },
  plugins: [
    "@hey-api/typescript",
    {
      name: "zod",
      compatibilityVersion: "mini",
      requests: false,
      responses: true,
    },
  ],
});
