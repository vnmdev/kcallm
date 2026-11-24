import { createUUID } from "./id";

export const nutrientKeys = [
  "kcal",
  "protein",
  "carbohydrates",
  "fat",
  "saturates",
  "sugars",
  "fibre",
  "salt",
] as const;
export type NutrientKey = (typeof nutrientKeys)[number];
export type Nutrients = Record<NutrientKey, string | null>;
export const nutrientLabels: Record<NutrientKey, string> = {
  kcal: "Calories",
  protein: "Protein",
  carbohydrates: "Carbs",
  fat: "Fat",
  saturates: "Saturates",
  sugars: "Sugars",
  fibre: "Fibre",
  salt: "Salt",
};
export const emptyNutrients = (): Nutrients =>
  Object.fromEntries(nutrientKeys.map((key) => [key, null])) as Nutrients;
export type Conversion = { unit: string; amount: string; basis_amount: string };
export type Serving = { quantity: string; unit: string };
export type FoodInput = {
  name: string;
  brand: string;
  category: string;
  preparation: string;
  aliases: string[];
  basis_amount: string;
  basis_unit: string;
  nutrients: Nutrients;
  conversions: Conversion[];
  usual_serving: Serving | null;
};
export type Food = FoodInput & {
  id: string;
  revision: number;
  archived: boolean;
};
export type FoodDraft = Partial<
  Omit<FoodInput, "basis_amount" | "basis_unit" | "nutrients">
> & {
  basis_amount?: string | null;
  basis_unit?: string | null;
  nutrients?: Partial<Nutrients>;
};
export type LineInput = {
  food_id: string;
  quantity: string | null;
  unit: string | null;
  entry_id?: string;
};
export type ReviewedLine = LineInput & {
  food_revision: number;
  entry_revision?: number;
};
export type PreviewLine = ReviewedLine & {
  food_name: string;
  brand: string;
  nutrients: Nutrients;
};
export type Preview = {
  date: string;
  items: PreviewLine[];
  totals: Nutrients;
  incomplete: string[];
};
export type Entry = {
  id: string;
  revision: number;
  batch_id: string;
  date: string;
  food_id: string;
  food_revision: number;
  food_name: string;
  brand: string;
  quantity: string;
  unit: string;
  nutrients: Nutrients;
  created_at: string;
};
export type Day = {
  date: string;
  entries: Entry[];
  totals: Nutrients;
  incomplete: string[];
  remaining?: Nutrients;
};
export type Settings = { revision: number; timezone: string; goals: Nutrients };
export type Provider = "chatgpt" | "llama" | "openrouter";
/** Display order and names: OpenRouter first, your own endpoint last (an advanced option). */
export const providerLabels: Record<Provider, string> = {
  openrouter: "OpenRouter",
  chatgpt: "ChatGPT",
  llama: "Custom endpoint (llama.cpp)",
};
export type AIStatus = {
  provider: Provider;
  model: string;
  llama_base_url: string;
  providers: { id: Provider; connected: boolean; message: string }[];
  auth_pending: boolean;
  /** True on the public demo: one hosted model, fixed settings, no ChatGPT sign-in. */
  demo?: boolean;
};
export type AIModel = { id: string; name: string; input_modalities: string[] };
export type ProposedLine = {
  status: "matched" | "clarification" | "no_match";
  food_id: string | null;
  quantity: string | null;
  unit: string | null;
  question: string;
  candidates: string[];
};
export type Interpretation = {
  provider: Provider;
  model: string;
  outcome: "proposal" | "clarification" | "no_match";
  message: string;
  items: ProposedLine[];
};
export type ConfirmBody = {
  request_id: string;
  date: string;
  items: ReviewedLine[];
};
export type DraftLine = ProposedLine & { key: string };
export type LoggingDraft = {
  date: string;
  text: string;
  image_data_url: string | null;
  image_name: string;
  provider: Provider;
  model: string;
  items: DraftLine[];
  preview: Preview | null;
  request_id: string;
  pending: ConfirmBody | null;
};

export function newDraft(
  date: string,
  provider: Provider = "chatgpt",
  model = "",
): LoggingDraft {
  return {
    date,
    text: "",
    image_data_url: null,
    image_name: "",
    provider,
    model,
    items: [],
    preview: null,
    request_id: createUUID(),
    pending: null,
  };
}

export function foodUnits(food?: Food): string[] {
  if (!food) return ["g", "pack", "piece", "serving", "ml"];
  const units = [food.basis_unit, ...food.conversions.map((item) => item.unit)];
  if (["g", "kg"].includes(food.basis_unit)) units.push("g", "kg");
  if (["ml", "l"].includes(food.basis_unit)) units.push("ml", "l");
  return [...new Set(units)];
}

export function localDate(timezone = "Europe/London"): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

// Formatting only. All nutrition calculations and totals come from the backend.
export function numberText(value: string | null | undefined): string {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? new Intl.NumberFormat("en-GB", { maximumFractionDigits: 2 }).format(
        numeric,
      )
    : value;
}

// Display rounding for the headline totals only; the exact value stays in the title tooltip.
export function wholeNumberText(value: string | null | undefined): string {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 }).format(
        numeric,
      )
    : value;
}

export function blankFood(draft?: FoodDraft): FoodInput {
  return {
    name: draft?.name ?? "",
    brand: draft?.brand ?? "",
    category: draft?.category ?? "",
    preparation: draft?.preparation ?? "",
    aliases: draft?.aliases ?? [],
    basis_amount: draft?.basis_amount ?? "",
    basis_unit: draft?.basis_unit ?? "",
    nutrients: { ...emptyNutrients(), ...draft?.nutrients },
    conversions: draft?.conversions ?? [],
    usual_serving: draft?.usual_serving ?? null,
  };
}
