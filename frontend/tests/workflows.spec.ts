import { expect, test, type Page, type Route } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const nutrients = {
  kcal: "250",
  protein: "3.4",
  carbohydrates: "50",
  fat: "2.8",
  saturates: null,
  sugars: "48.4",
  fibre: "5.2",
  salt: "0.04",
};
const empty = Object.fromEntries(
  Object.keys(nutrients).map((key) => [key, null]),
);
const food = {
  id: "mango-demo",
  revision: 1,
  archived: false,
  name: "Demo mango",
  brand: "Demo",
  category: "Fruit",
  preparation: "",
  aliases: ["mango"],
  basis_amount: "1",
  basis_unit: "pack",
  nutrients,
  conversions: [],
  usual_serving: { quantity: "1", unit: "pack" },
};
const entry = {
  id: "entry-1",
  revision: 1,
  batch_id: "batch-1",
  date: "2026-11-13",
  food_id: food.id,
  food_revision: 1,
  food_name: food.name,
  brand: food.brand,
  quantity: "2",
  unit: "pack",
  nutrients,
  created_at: "2026-11-13T12:00:00Z",
};
const previewItem = {
  food_id: food.id,
  food_revision: 1,
  food_name: food.name,
  brand: food.brand,
  quantity: "2",
  unit: "pack",
  nutrients,
};

function day(date: string, name?: string) {
  return {
    date,
    entries: name ? [{ ...entry, date, food_name: name }] : [],
    totals: name ? nutrients : empty,
    incomplete: name ? ["saturates"] : [],
  };
}

async function json(route: Route, data: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(data),
  });
}

async function standardAPI(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/foods") return json(route, [food]);
    if (url.pathname === "/api/settings")
      return json(route, {
        revision: 1,
        timezone: "Europe/London",
        goals: empty,
      });
    if (url.pathname === "/api/ai/status")
      return json(route, {
        provider: "chatgpt",
        model: "test-model",
        llama_base_url: "http://localhost/v1",
        providers: [
          {
            id: "chatgpt",
            connected: true,
            message: "Synthetic test connection",
          },
        ],
        auth_pending: false,
      });
    if (url.pathname === "/api/diary")
      return json(route, day(url.searchParams.get("date")!));
    if (url.pathname === "/api/preview") {
      const body = route.request().postDataJSON();
      return json(route, {
        date: body.date,
        items: [previewItem],
        totals: nutrients,
        incomplete: ["saturates"],
      });
    }
    if (url.pathname === "/api/starter-drafts") return json(route, []);
    return json(
      route,
      {
        error: {
          code: "unexpected_test_request",
          message: `Unexpected request: ${url.pathname}`,
        },
      },
      500,
    );
  });
}

async function addManual(page: Page) {
  await page
    .getByRole("button", { name: "Choose a food", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "Saved food", exact: true })
    .selectOption(food.id);
  await page.getByLabel("Amount", { exact: true }).fill("2");
  await page.getByRole("button", { name: "Add to review" }).click();
  await page.getByRole("button", { name: "Preview nutrition" }).click();
  await expect(
    page.getByRole("button", { name: "Confirm & save" }),
  ).toBeEnabled();
}

test("without native randomUUID, a lost confirmation survives reload and retries the same valid ID without rendering its old receipt", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(globalThis.crypto, "randomUUID", {
      value: undefined,
      configurable: true,
    });
  });
  await standardAPI(page);
  const requests: unknown[] = [];
  let committed = false;
  await page.route("**/api/diary?**", async (route) =>
    json(
      route,
      day(
        new URL(route.request().url()).searchParams.get("date")!,
        committed ? "Later edited name" : undefined,
      ),
    ),
  );
  await page.route("**/api/diary/confirm", async (route) => {
    requests.push(route.request().postDataJSON());
    committed = true;
    if (requests.length === 1) return route.abort("failed");
    return json(
      route,
      day(route.request().postDataJSON().date, "Original receipt name"),
    );
  });
  await page.goto("/");
  expect(await page.evaluate(() => typeof crypto.randomUUID)).toBe("undefined");
  await addManual(page);
  await page.getByRole("button", { name: "Confirm & save" }).click();
  await expect(
    page.getByRole("button", { name: "Retry this save" }),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Retry this save" }).click();
  await expect(page.getByRole("status")).toContainText("Added to your diary");
  expect(requests).toHaveLength(2);
  expect(requests[0]).toEqual(requests[1]);
  expect((requests[0] as { request_id: string }).request_id).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
  await expect(
    page.getByRole("heading", { name: "Later edited name" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Original receipt name" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Retry this save" }),
  ).toHaveCount(0);
});

test("a clarification with otherwise valid fields still needs deliberate human resolution", async ({
  page,
}) => {
  await standardAPI(page);
  await page.route("**/api/ai/interpret", (route) =>
    json(route, {
      provider: "chatgpt",
      model: "test-model",
      outcome: "clarification",
      message: "Check preparation.",
      items: [
        {
          status: "clarification",
          food_id: food.id,
          quantity: "2",
          unit: "pack",
          question: "Is this the saved preparation?",
          candidates: [food.id],
        },
      ],
    }),
  );
  await page.goto("/");
  await page
    .getByRole("textbox", { name: "Describe your food and quantity" })
    .fill("2 mango packs");
  await page.getByRole("button", { name: "Interpret entry" }).click();
  await expect(
    page.getByRole("button", { name: "Preview nutrition" }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "I've checked this food and amount" })
    .click();
  await expect(
    page.getByRole("button", { name: "Preview nutrition" }),
  ).toBeEnabled();
});

test("an edit refuses a newer entry revision returned by preview", async ({
  page,
}) => {
  await standardAPI(page);
  let writes = 0;
  await page.route("**/api/diary?**", (route) =>
    json(
      route,
      day(new URL(route.request().url()).searchParams.get("date")!, food.name),
    ),
  );
  await page.route("**/api/preview", (route) =>
    json(route, {
      date: route.request().postDataJSON().date,
      items: [{ ...previewItem, entry_id: entry.id, entry_revision: 2 }],
      totals: nutrients,
      incomplete: ["saturates"],
    }),
  );
  await page.route("**/api/entries/**", (route) => {
    writes += 1;
    return json(route, entry);
  });
  await page.goto("/");
  await page.getByRole("button", { name: `Edit ${food.name}` }).click();
  await page.getByRole("button", { name: "Preview changes" }).click();
  await expect(
    page.getByText("This entry changed while you were editing.", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save changes", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Close & refresh" }).click();
  expect(writes).toBe(0);
});

test("an old delayed day response cannot overwrite a more recently selected day", async ({
  page,
}) => {
  await standardAPI(page);
  let releaseOld: (() => void) | undefined;
  let oldStarted: (() => void) | undefined;
  const started = new Promise<void>((resolve) => {
    oldStarted = resolve;
  });
  const delayed = new Promise<void>((resolve) => {
    releaseOld = resolve;
  });
  await page.route("**/api/diary?**", async (route) => {
    const date = new URL(route.request().url()).searchParams.get("date")!;
    if (date === "2026-10-01") {
      oldStarted?.();
      await delayed;
      return json(route, day(date, "Old slow day"));
    }
    return json(
      route,
      day(date, date === "2026-10-02" ? "New selected day" : undefined),
    );
  });
  await page.goto("/");
  await page.getByLabel("Diary date", { exact: true }).fill("2026-10-01");
  await started;
  await page.getByLabel("Diary date", { exact: true }).fill("2026-10-02");
  await expect(
    page.getByRole("heading", { name: "New selected day" }),
  ).toBeVisible();
  const oldResponse = page.waitForResponse((response) =>
    response.url().includes("date=2026-10-01"),
  );
  releaseOld?.();
  await oldResponse;
  await expect(page.getByRole("heading", { name: "Old slow day" })).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("heading", { name: "New selected day" }),
  ).toBeVisible();
});

test("a photo and its caption survive refresh, then are removed by explicit cancellation", async ({
  page,
}) => {
  await page.setViewportSize({ width: 393, height: 851 });
  await standardAPI(page);
  await page.goto("/");
  await page
    .getByRole("textbox", { name: "Describe your food and quantity" })
    .fill("Demo food on the scale");
  await page.getByLabel("Take a photo", { exact: true }).setInputFiles({
    name: "synthetic-scale.png",
    mimeType: "image/png",
    buffer: Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=",
      "base64",
    ),
  });
  await expect(
    page.getByAltText("Your attached food or scale photo"),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("textbox", { name: "Describe your food and quantity" }),
  ).toHaveValue("Demo food on the scale");
  await expect(
    page.getByAltText("Your attached food or scale photo"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Cancel draft" }).click();
  await expect(
    page.getByAltText("Your attached food or scale photo"),
  ).toHaveCount(0);
  await page.reload();
  await expect(
    page.getByAltText("Your attached food or scale photo"),
  ).toHaveCount(0);
  await expect(
    page.getByRole("textbox", { name: "Describe your food and quantity" }),
  ).toHaveValue("");
  await expect(
    page.getByRole("button", { name: "Choose a food", exact: true }),
  ).toBeVisible();
});

test("pasted nutrition screenshot stays local until extraction and requires review before approval", async ({
  page,
}) => {
  await standardAPI(page);
  const extractionBodies: Record<string, unknown>[] = [];
  let approvals = 0;
  const extractedDraft = {
    ...food,
    name: "Synthetic screenshot food",
    basis_amount: "100",
    basis_unit: "g",
    usual_serving: null,
  };
  await page.route("**/api/ai/extract-food", (route) => {
    extractionBodies.push(route.request().postDataJSON());
    return json(route, {
      provider: "chatgpt",
      model: "test-model",
      draft: extractedDraft,
      warnings: ["Synthetic test label. Review the numbers before approval."],
    });
  });
  await page.route("**/api/foods", (route) => {
    if (route.request().method() === "POST") {
      approvals += 1;
      return json(
        route,
        {
          ...route.request().postDataJSON(),
          id: "approved-screenshot",
          revision: 1,
          archived: false,
        },
        201,
      );
    }
    return json(route, [food]);
  });
  await page.goto("/");
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: /My foods/ })
    .click();
  await page.getByRole("button", { name: "Add food", exact: true }).click();
  const pasteTarget = page.getByRole("region", {
    name: "Nutrition label image input",
    exact: true,
  });
  await pasteTarget.focus();
  const pastedDataURL = await pasteTarget.evaluate((target) => {
    const canvas = document.createElement("canvas");
    canvas.width = 500;
    canvas.height = 420;
    const ctx = canvas.getContext("2d")!;
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, 500, 420);
    ctx.fillStyle = "#243024";
    ctx.font = "bold 22px sans-serif";
    ctx.fillText("SYNTHETIC TEST LABEL", 24, 38);
    ctx.font = "17px sans-serif";
    [
      "Nutrition per 100 g",
      "Calories       250 kcal",
      "Protein         3.4 g",
      "Carbohydrates    50 g",
      "Fat             2.8 g",
      "Sugars         48.4 g",
      "Fibre           5.2 g",
      "Salt           0.04 g",
    ].forEach((line, index) => ctx.fillText(line, 24, 85 + index * 38));
    const dataURL = canvas.toDataURL("image/png");
    const bytes = Uint8Array.from(atob(dataURL.split(",")[1]), (char) =>
      char.charCodeAt(0),
    );
    const clipboard = new DataTransfer();
    clipboard.items.add(
      new File([bytes], "pasted-nutrition-table.png", { type: "image/png" }),
    );
    const paste = new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: clipboard,
    });
    target.dispatchEvent(paste);
    if (!paste.defaultPrevented)
      throw new Error("The screenshot paste was not handled.");
    return dataURL;
  });
  await expect(page.getByAltText("Nutrition label to review")).toHaveAttribute(
    "src",
    pastedDataURL,
  );
  expect(extractionBodies).toHaveLength(0);
  expect(approvals).toBe(0);
  await page
    .getByRole("button", { name: "Read this label", exact: true })
    .click();
  await expect(page.getByRole("textbox", { name: /Food name/ })).toHaveValue(
    extractedDraft.name,
  );
  expect(extractionBodies).toHaveLength(1);
  expect(extractionBodies[0].image_data_url).toBe(pastedDataURL);
  expect(approvals).toBe(0);

  // Text-only paste must keep its native default behaviour in editable fields.
  const ordinaryPaste = await page
    .getByRole("textbox", { name: "Brand", exact: true })
    .evaluate((input) => {
      const clipboard = new DataTransfer();
      clipboard.setData("text/plain", "Example supermarket");
      const paste = new ClipboardEvent("paste", {
        bubbles: true,
        cancelable: true,
        clipboardData: clipboard,
      });
      input.dispatchEvent(paste);
      return paste.defaultPrevented;
    });
  expect(ordinaryPaste).toBe(false);

  await mkdir(resolve("artifacts"), { recursive: true });
  await page.screenshot({
    path: resolve("artifacts", "paste-food-review.png"),
    fullPage: false,
  });
  await page.getByRole("button", { name: "Approve food", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(approvals).toBe(1);
  await expect(page.getByAltText("Nutrition label to review")).toHaveCount(0);
});

test("Today accepts pasted images and preserves the prior draft when an image fails validation", async ({
  page,
}) => {
  await standardAPI(page);
  await page.goto("/");
  const caption = page.getByRole("textbox", {
    name: "Describe your food and quantity",
  });
  await caption.fill("Keep this food caption");
  const imageTarget = page.getByRole("region", {
    name: "Food image input",
    exact: true,
  });
  await imageTarget.focus();
  await imageTarget.evaluate((target) => {
    const png = Uint8Array.from(
      atob(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=",
      ),
      (char) => char.charCodeAt(0),
    );
    const clipboard = new DataTransfer();
    clipboard.items.add(
      new File([png], "pasted-scale.png", { type: "image/png" }),
    );
    target.dispatchEvent(
      new ClipboardEvent("paste", {
        bubbles: true,
        cancelable: true,
        clipboardData: clipboard,
      }),
    );
  });
  const photo = page.getByAltText("Your attached food or scale photo");
  await expect(photo).toBeVisible();
  const original = await photo.getAttribute("src");
  const textPastePrevented = await caption.evaluate((input) => {
    const clipboard = new DataTransfer();
    clipboard.setData("text/plain", " ordinary text");
    const paste = new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: clipboard,
    });
    input.dispatchEvent(paste);
    return paste.defaultPrevented;
  });
  expect(textPastePrevented).toBe(false);
  await imageTarget.evaluate((target) => {
    const clipboard = new DataTransfer();
    clipboard.items.add(
      new File([new Uint8Array(10 * 1024 * 1024 + 1)], "too-large.png", {
        type: "image/png",
      }),
    );
    target.dispatchEvent(
      new ClipboardEvent("paste", {
        bubbles: true,
        cancelable: true,
        clipboardData: clipboard,
      }),
    );
  });
  await expect(page.getByRole("alert")).toContainText("larger than 10 MB");
  await expect(photo).toHaveAttribute("src", original!);
  await expect(caption).toHaveValue("Keep this food caption");
  await page.reload();
  await expect(photo).toHaveAttribute("src", original!);
  await expect(caption).toHaveValue("Keep this food caption");
});

test("failed label extraction preserves the screenshot and name, then reviews partial nutrient values on explicit retry", async ({
  page,
}) => {
  await standardAPI(page);
  const manuallyEnteredName = "My baking potato";
  const warning =
    "Fat <0.5 g, saturates <0.1 g and salt <0.01 g are non-exact label values. These fields remain unknown; review before approval.";
  const partialNutrients = {
    kcal: "99",
    protein: "2",
    carbohydrates: "20",
    sugars: "0.5",
    fibre: "2",
    fat: null,
    saturates: null,
    salt: null,
  };
  const extractionBodies: Record<string, unknown>[] = [];
  const approvedBodies: Record<string, unknown>[] = [];
  await page.route("**/api/ai/extract-food", (route) => {
    extractionBodies.push(route.request().postDataJSON());
    if (extractionBodies.length === 1) {
      return json(
        route,
        {
          error: {
            code: "invalid_food_draft",
            message:
              "The provider returned an invalid food draft. Your photo is still available for retry.",
          },
        },
        502,
      );
    }
    return json(route, {
      provider: "chatgpt",
      model: "test-model",
      warnings: [warning],
      draft: {
        name: manuallyEnteredName,
        brand: "",
        category: "",
        preparation: "",
        aliases: [],
        basis_amount: "100",
        basis_unit: "g",
        nutrients: partialNutrients,
        conversions: [],
        usual_serving: null,
      },
    });
  });
  await page.route("**/api/foods", (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      approvedBodies.push(body);
      return json(
        route,
        { ...body, id: "reviewed-partial-food", revision: 1, archived: false },
        201,
      );
    }
    return json(route, [food]);
  });
  await page.goto("/");
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: /My foods/ })
    .click();
  await page.getByRole("button", { name: "Add food", exact: true }).click();
  const nameInput = page.getByRole("textbox", { name: /Food name/ });
  await nameInput.fill(manuallyEnteredName);
  await page
    .getByRole("region", { name: "Nutrition label image input", exact: true })
    .evaluate((target) => {
      const png = Uint8Array.from(
        atob(
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=",
        ),
        (char) => char.charCodeAt(0),
      );
      const clipboard = new DataTransfer();
      clipboard.items.add(
        new File([png], "pasted-potato-table.png", { type: "image/png" }),
      );
      target.dispatchEvent(
        new ClipboardEvent("paste", {
          bubbles: true,
          cancelable: true,
          clipboardData: clipboard,
        }),
      );
    });
  const photo = page.getByAltText("Nutrition label to review");
  await expect(photo).toBeVisible();
  const originalPhoto = await photo.getAttribute("src");
  await page
    .getByRole("button", { name: "Read this label", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("invalid food draft");
  await expect(photo).toHaveAttribute("src", originalPhoto!);
  await expect(nameInput).toHaveValue(manuallyEnteredName);
  expect(extractionBodies).toHaveLength(1);
  expect(approvedBodies).toHaveLength(0);

  await page.reload();
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: /My foods/ })
    .click();
  await expect(photo).toHaveAttribute("src", originalPhoto!);
  await expect(nameInput).toHaveValue(manuallyEnteredName);
  expect(extractionBodies).toHaveLength(1);
  await page
    .getByRole("button", { name: "Read this label", exact: true })
    .click();
  await expect(page.getByText(warning, { exact: true })).toBeVisible();
  expect(extractionBodies).toHaveLength(2);
  expect(extractionBodies[0].image_data_url).toBe(originalPhoto);
  expect(extractionBodies[1].image_data_url).toBe(originalPhoto);
  for (const [label, expected] of Object.entries({
    Calories: "99",
    Protein: "2",
    Carbs: "20",
    Sugars: "0.5",
    Fibre: "2",
    Fat: "",
    Saturates: "",
    Salt: "",
  })) {
    await expect(
      page
        .getByRole("dialog")
        .getByRole("textbox", { name: new RegExp(`^${label}`) }),
    ).toHaveValue(expected);
  }
  await expect(
    page.getByRole("textbox", { name: "Nutrition basis amount", exact: true }),
  ).toHaveValue("100");
  await expect(
    page.getByRole("combobox", { name: "Nutrition basis unit", exact: true }),
  ).toHaveValue("g");
  await expect(
    page.getByRole("checkbox", { name: /Use an explicit usual serving/ }),
  ).not.toBeChecked();
  expect(approvedBodies).toHaveLength(0);
  await page.getByRole("button", { name: "Approve food", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(approvedBodies).toHaveLength(1);
  expect(approvedBodies[0].nutrients).toEqual(partialNutrients);
});

for (const width of [360, 393]) {
  test(`mobile ${width}px gallery capture and layouts`, async ({ page }) => {
    await page.setViewportSize({ width, height: 851 });
    await standardAPI(page);
    await page.goto("/");
    const noOverflow = async () =>
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
    await expect(
      page.getByRole("button", { name: "Gallery", exact: true }),
    ).toBeVisible();
    const chooserPromise = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "Gallery", exact: true }).click();
    const chooser = await chooserPromise;
    expect(await chooser.element().getAttribute("capture")).toBeNull();
    await chooser.setFiles({
      name: "gallery.png",
      mimeType: "image/png",
      buffer: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=",
        "base64",
      ),
    });
    await expect(
      page.getByAltText("Your attached food or scale photo"),
    ).toBeVisible();
    await noOverflow();
    await mkdir(resolve("artifacts"), { recursive: true });
    await page.screenshot({
      path: resolve("artifacts", `mobile-today-${width}.png`),
      fullPage: true,
    });
    await page
      .getByRole("navigation")
      .getByRole("button", { name: /My foods/ })
      .click();
    await expect(page.getByText("Demo mango", { exact: true })).toBeVisible();
    await noOverflow();
    await page.screenshot({
      path: resolve("artifacts", `mobile-foods-${width}.png`),
      fullPage: true,
    });
    await page.getByRole("button", { name: "Add food", exact: true }).click();
    await expect(
      page.getByRole("dialog").getByLabel("Take a photo", { exact: true }),
    ).toHaveAttribute("capture", "environment");
    const labelChooserPromise = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "Gallery", exact: true }).click();
    const labelChooser = await labelChooserPromise;
    expect(await labelChooser.element().getAttribute("capture")).toBeNull();
    await labelChooser.setFiles({
      name: "label.png",
      mimeType: "image/png",
      buffer: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=",
        "base64",
      ),
    });
    await expect(page.getByAltText("Nutrition label to review")).toBeVisible();
    expect(
      await page
        .getByRole("dialog")
        .evaluate((el) => el.scrollWidth <= el.clientWidth),
    ).toBe(true);
    await page.screenshot({
      path: resolve("artifacts", `mobile-editor-${width}.png`),
    });
    await page
      .getByRole("button", { name: "Close dialog", exact: true })
      .click();
    await page
      .getByRole("navigation")
      .getByRole("button", { name: "Settings", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "Daily targets" }),
    ).toBeVisible();
    await noOverflow();
    await page.screenshot({
      path: resolve("artifacts", `mobile-settings-${width}.png`),
      fullPage: true,
    });
  });
}

test("ranked food guesses retain scale weight and require explicit confirmation", async ({
  page,
}) => {
  await standardAPI(page);
  const potato = {
    ...food,
    id: "potato",
    name: "Baking potato",
    basis_amount: "100",
    basis_unit: "g",
    usual_serving: null,
  };
  const sweet = { ...potato, id: "sweet-potato", name: "Sweet potato" };
  await page.route("**/api/foods", (r) => json(r, [potato, sweet]));
  await page.route("**/api/ai/interpret", (r) =>
    json(r, {
      provider: "chatgpt",
      model: "test-model",
      outcome: "clarification",
      message: "Please check the food.",
      items: [
        {
          status: "clarification",
          food_id: "potato",
          quantity: "180",
          unit: "g",
          question: "Looks like a potato. Which saved food is this?",
          candidates: ["potato", "sweet-potato"],
        },
      ],
    }),
  );
  await page.goto("/");
  await page
    .getByLabel("Describe your food and quantity")
    .fill("What is on my scale?");
  await page
    .getByRole("button", { name: "Interpret entry", exact: true })
    .click();
  const guesses = page.getByRole("group", {
    name: "Suggested foods for item 1",
  });
  await expect(guesses.getByRole("button").first()).toContainText(
    "Baking potato",
  );
  await expect(guesses.getByRole("button").first()).toContainText("Best guess");
  await expect(
    page.getByRole("button", { name: "Preview nutrition", exact: true }),
  ).toBeDisabled();
  await guesses
    .getByRole("button", { name: "Sweet potato", exact: true })
    .click();
  await expect(page.getByLabel("Food 1", { exact: true })).toHaveValue(
    "sweet-potato",
  );
  await expect(page.getByLabel("Amount", { exact: true })).toHaveValue("180");
  await expect(page.locator(".proposal-amount select")).toHaveValue("g");
  await expect(
    page.getByRole("button", { name: "Preview nutrition", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "I've checked this food and amount" })
    .click();
  await expect(
    page.getByRole("button", { name: "Preview nutrition", exact: true }),
  ).toBeEnabled();
});
