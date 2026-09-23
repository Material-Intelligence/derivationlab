import { describe, expect, it } from "vitest";
import { assertAllowedExternalUrl } from "./host";

describe("ProductHost external URL policy", () => {
  it("allows only HTTPS OpenAI and ChatGPT hosts without credentials, ports, or fragments", () => {
    expect(assertAllowedExternalUrl("https://auth.openai.com/device").hostname).toBe("auth.openai.com");
    expect(assertAllowedExternalUrl("https://chatgpt.com/device").hostname).toBe("chatgpt.com");
    for (const unsafe of [
      "http://auth.openai.com/device",
      "https://openai.com.evil.example/device",
      "https://user@openai.com/device",
      "https://openai.com:444/device",
      "https://openai.com/device#token",
    ]) {
      expect(() => assertAllowedExternalUrl(unsafe)).toThrow("not an allowed OpenAI HTTPS address");
    }
  });
});
