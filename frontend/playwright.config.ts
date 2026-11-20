import { defineConfig, devices } from "@playwright/test";

const integrationURL = process.env.KCALLM_INTEGRATION_URL;

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: integrationURL ?? "http://127.0.0.1:5174",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1280, height: 900 },
      },
    },
  ],
  webServer: integrationURL
    ? undefined
    : {
        command: "npm run dev -- --port 5174",
        url: "http://127.0.0.1:5174",
        reuseExistingServer: false,
      },
});
