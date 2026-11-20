import { expect, test, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const enabled =
  Boolean(process.env.KCALLM_INTEGRATION_URL) &&
  process.env.KCALLM_INTEGRATION_ALLOW_WRITE === "1";
const foodName = "Synthetic oats · integration fixture";

test.skip(
  !enabled,
  "Requires an explicitly authorised, isolated real backend and KCALLM_INTEGRATION_ALLOW_WRITE=1.",
);

async function logPortion(page: Page, foodId: string, amount: string) {
  await page
    .getByRole("button", { name: "Choose a food", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "Saved food", exact: true })
    .selectOption(foodId);
  await page.getByRole("textbox", { name: "Amount", exact: true }).fill(amount);
  await page
    .getByRole("button", { name: "Add to review", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Preview nutrition", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Confirm & save", exact: true }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Confirm & save", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("Added to your diary");
}

test("real backend: reviewed food, decimal preview, confirmation, historical edit and undo", async ({
  page,
  request,
}) => {
  test.setTimeout(60000);
  const existing = await request.get("/api/foods");
  expect(existing.ok()).toBe(true);
  expect(
    await existing.json(),
    "This test requires an isolated backend with an empty active food catalogue.",
  ).toEqual([]);
  const originalSettingsResponse = await request.get("/api/settings");
  const originalSettings = await originalSettingsResponse.json();
  const browserErrors: string[] = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));

  await page.goto("/");
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: /My foods/ })
    .click();
  await page.getByRole("button", { name: "Add food", exact: true }).click();
  const foodDialog = page.getByRole("dialog");
  await foodDialog.getByRole("textbox", { name: /Food name/ }).fill(foodName);
  await foodDialog
    .getByRole("textbox", { name: "Brand", exact: true })
    .fill("Test data only");
  await foodDialog
    .getByRole("textbox", { name: "Category", exact: true })
    .fill("Synthetic fixtures");
  await foodDialog
    .getByRole("textbox", { name: "Preparation", exact: true })
    .fill("Illustrative values, not a real product");
  await foodDialog
    .getByRole("textbox", { name: "Nutrition basis amount", exact: true })
    .fill("100");
  await foodDialog
    .getByRole("combobox", { name: "Nutrition basis unit", exact: true })
    .fill("g");
  for (const [label, value] of Object.entries({
    Calories: "123.4",
    Protein: "10",
    Carbs: "20",
    Fat: "3.2",
    Saturates: "0.6",
    Sugars: "0",
    Fibre: "4",
    Salt: "0.01",
  })) {
    await foodDialog
      .getByRole("textbox", { name: new RegExp(`^${label}`) })
      .fill(value);
  }
  await foodDialog
    .getByRole("button", { name: "Approve food", exact: true })
    .click();
  await expect(foodDialog).toHaveCount(0);
  await expect(
    page.getByRole("cell", { name: new RegExp(foodName) }).first(),
  ).toBeVisible();
  const foods = await (await request.get("/api/foods")).json();
  expect(foods).toHaveLength(1);
  const foodId = foods[0].id as string;

  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Settings", exact: true })
    .click();
  await page
    .getByRole("textbox", { name: "Calories daily target", exact: true })
    .fill("2000");
  await page
    .getByRole("button", { name: "Save targets & timezone", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText(
    "Your targets and date settings are saved.",
  );
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Today", exact: true })
    .click();
  const date = await page
    .getByRole("textbox", { name: "Diary date", exact: true })
    .inputValue();
  await logPortion(page, foodId, "180");
  const confirmed = await (await request.get(`/api/diary?date=${date}`)).json();
  expect(confirmed.entries).toHaveLength(1);
  expect(confirmed.totals.kcal).toBe("222.12");
  expect(confirmed.totals.protein).toBe("18");
  await expect(
    page.getByText("1,777.88 kcal remaining", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Dismiss saved message", exact: true })
    .click();

  await page
    .getByRole("button", { name: `Edit ${foodName}`, exact: true })
    .click();
  const editDialog = page.getByRole("dialog");
  await editDialog
    .getByRole("textbox", { name: "Amount", exact: true })
    .fill("200");
  await editDialog
    .getByRole("button", { name: "Preview changes", exact: true })
    .click();
  await editDialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(editDialog).toHaveCount(0);
  await expect(
    page.getByText("1,753.2 kcal remaining", { exact: true }),
  ).toBeVisible();
  const edited = await (await request.get(`/api/diary?date=${date}`)).json();
  expect(edited.entries).toHaveLength(1);
  expect(edited.entries[0].quantity).toBe("200");
  expect(edited.totals.kcal).toBe("246.8");

  await logPortion(page, foodId, "50");
  await expect(
    page.getByRole("heading", { name: foodName, exact: true }),
  ).toHaveCount(2);
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: foodName, exact: true }),
  ).toHaveCount(1);
  await expect(
    page.getByText("1,753.2 kcal remaining", { exact: true }),
  ).toBeVisible();

  const artifactDir = resolve("artifacts");
  await mkdir(artifactDir, { recursive: true });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: resolve(artifactDir, "real-backend-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 393, height: 851 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: resolve(artifactDir, "real-backend-mobile.png"),
    fullPage: false,
  });
  await page.screenshot({
    path: resolve(artifactDir, "real-backend-mobile-full.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);

  // Cleanup uses only the test record created above, on the explicitly isolated host.
  await page
    .getByRole("button", { name: `Delete ${foodName}`, exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Remove entry", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: foodName, exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: /My foods/ })
    .click();
  await page
    .getByRole("button", { name: `Archive ${foodName}`, exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Archive food", exact: true })
    .click();
  await expect(
    page.getByRole("cell", { name: new RegExp(foodName) }),
  ).toHaveCount(0);
  const currentSettings = await (await request.get("/api/settings")).json();
  const restoredSettings = await request.put("/api/settings", {
    data: { ...originalSettings, revision: currentSettings.revision },
  });
  expect(restoredSettings.ok()).toBe(true);
  expect(browserErrors).toEqual([]);
});
