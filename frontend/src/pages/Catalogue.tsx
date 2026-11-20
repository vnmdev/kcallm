import { useEffect, useRef, useState } from "react";
import { ImagePicker } from "../components/ImagePicker";
import {
  ArrowRight,
  BookOpen,
  Camera,
  Check,
  FileUp,
  Pencil,
  Plus,
  Search,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { api, errorMessage, post, put, readImage } from "../api";
import { loadStoredDraft, saveStoredDraft } from "../draft";
import { pastedImage } from "../clipboard";
import { Busy, Modal, Notice, SectionHeading } from "../components/ui";
import {
  blankFood,
  nutrientKeys,
  nutrientLabels,
  numberText,
  type AIStatus,
  type Food,
  type FoodDraft,
  type FoodInput,
} from "../types";

type Props = {
  foods: Food[];
  ai: AIStatus;
  onRefresh: (quiet?: boolean) => Promise<void>;
  onSettings: () => void;
};
type FormDraft = {
  form: FoodInput;
  editing: Food | null;
  aliasesText: string;
  photo: string | null;
  photoName: string;
  warnings: string[];
};
type CatalogueState = { current: FormDraft | null; queue: FoodDraft[] };

function draftForm(food?: FoodDraft, editing: Food | null = null): FormDraft {
  const note = (food as (FoodDraft & { review_note?: string }) | undefined)
    ?.review_note;
  return {
    form: blankFood(food),
    editing,
    aliasesText: food?.aliases?.join(", ") ?? "",
    photo: null,
    photoName: "",
    warnings: note ? [note] : [],
  };
}

function validateImportedDraft(value: unknown, index: number): FoodDraft {
  const invalid = () => {
    throw new Error(
      `Food draft ${index + 1} has an invalid structure. Use the documented catalogue JSON format; decimal values must be strings.`,
    );
  };
  if (!value || typeof value !== "object" || Array.isArray(value))
    return invalid();
  const item = value as Record<string, unknown>;
  for (const key of [
    "name",
    "brand",
    "category",
    "preparation",
    "basis_amount",
    "basis_unit",
  ]) {
    if (item[key] != null && typeof item[key] !== "string") return invalid();
  }
  if (
    item.aliases != null &&
    (!Array.isArray(item.aliases) ||
      item.aliases.some((alias) => typeof alias !== "string"))
  )
    return invalid();
  if (item.nutrients != null) {
    if (typeof item.nutrients !== "object" || Array.isArray(item.nutrients))
      return invalid();
    const values = item.nutrients as Record<string, unknown>;
    if (
      nutrientKeys.some(
        (key) => values[key] != null && typeof values[key] !== "string",
      )
    )
      return invalid();
  }
  if (
    item.conversions != null &&
    (!Array.isArray(item.conversions) ||
      item.conversions.some(
        (conversion) =>
          !conversion ||
          typeof conversion !== "object" ||
          ["unit", "amount", "basis_amount"].some(
            (key) => typeof conversion[key] !== "string",
          ),
      ))
  )
    return invalid();
  if (
    item.usual_serving != null &&
    (typeof item.usual_serving !== "object" ||
      Array.isArray(item.usual_serving) ||
      ["quantity", "unit"].some(
        (key) =>
          typeof (item.usual_serving as Record<string, unknown>)[key] !==
          "string",
      ))
  )
    return invalid();
  return item as FoodDraft;
}

export default function Catalogue({ foods, ai, onRefresh, onSettings }: Props) {
  const [search, setSearch] = useState("");
  const [state, setState] = useState<CatalogueState>({
    current: null,
    queue: [],
  });
  const [hydrated, setHydrated] = useState(false);
  const [starters, setStarters] = useState<FoodDraft[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [archiving, setArchiving] = useState<Food | null>(null);
  const importInput = useRef<HTMLInputElement>(null);
  const current = state.current;
  const form = current?.form;

  useEffect(() => {
    let active = true;
    loadStoredDraft<CatalogueState>("catalogue")
      .then((saved) => {
        if (active && saved) setState(saved);
      })
      .catch((err) => {
        if (active) setError(errorMessage(err));
      })
      .finally(() => {
        if (active) setHydrated(true);
      });
    api<FoodDraft[]>("/starter-drafts")
      .then((items) => {
        if (active) setStarters(items);
      })
      .catch(() => {
        /* Private starter data is optional. */
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (hydrated)
      void saveStoredDraft("catalogue", state).catch((err) =>
        setError(errorMessage(err)),
      );
  }, [state, hydrated]);

  function updateForm(change: Partial<FoodInput>) {
    setState((previous) =>
      previous.current
        ? {
            ...previous,
            current: {
              ...previous.current,
              form: { ...previous.current.form, ...change },
            },
          }
        : previous,
    );
  }
  function updateCurrent(change: Partial<FormDraft>) {
    setState((previous) =>
      previous.current
        ? { ...previous, current: { ...previous.current, ...change } }
        : previous,
    );
  }
  function openFood(food?: Food) {
    setError("");
    setSuccess("");
    setState((previous) => ({
      ...previous,
      current: draftForm(food, food ?? null),
    }));
  }
  function nextDraft(queue: FoodDraft[]) {
    setState({
      current: queue.length ? draftForm(queue[0]) : null,
      queue: queue.slice(1),
    });
    setError("");
  }
  async function attach(file?: File) {
    if (!file) return;
    try {
      updateCurrent({ photo: await readImage(file), photoName: file.name });
    } catch (err) {
      setError(errorMessage(err));
    }
  }
  async function extract() {
    if (!current?.photo) return;
    if (!ai.model) {
      onSettings();
      return;
    }
    setBusy("extract");
    setError("");
    try {
      const result = await post<{ draft: FoodDraft; warnings: string[] }>(
        "/ai/extract-food",
        {
          text: "Read the product identity, nutrition basis, nutrients and any explicit pack weight from this label. Leave unclear facts unknown.",
          image_data_url: current.photo,
          provider: ai.provider,
          model: ai.model,
        },
      );
      updateCurrent({
        form: blankFood(result.draft),
        aliasesText: result.draft.aliases?.join(", ") ?? "",
        warnings: result.warnings,
      });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }
  async function save() {
    if (!current) return;
    setBusy("save");
    setError("");
    const payload = {
      ...current.form,
      aliases: current.aliasesText
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
    };
    try {
      if (current.editing)
        await put(`/foods/${encodeURIComponent(current.editing.id)}`, {
          ...payload,
          revision: current.editing.revision,
        });
      else await post("/foods", payload);
      const next = {
        current: state.queue.length ? draftForm(state.queue[0]) : null,
        queue: state.queue.slice(1),
      };
      await saveStoredDraft("catalogue", next);
      setState(next);
      setSuccess(`${payload.name} is approved and ready to use.`);
      await onRefresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }
  async function importDrafts(file?: File) {
    if (!file) return;
    try {
      if (file.size > 5 * 1024 * 1024)
        throw new Error("Choose a catalogue JSON file smaller than 5 MB.");
      const parsed: unknown = JSON.parse(await file.text());
      const items = Array.isArray(parsed)
        ? parsed
        : parsed && typeof parsed === "object" && "foods" in parsed
          ? parsed.foods
          : null;
      if (
        !Array.isArray(items) ||
        !items.length ||
        items.length > 1000 ||
        items.some(
          (item) => !item || typeof item !== "object" || Array.isArray(item),
        )
      )
        throw new Error(
          "Use a JSON array of food drafts, or an object with a foods array (up to 1,000 foods).",
        );
      nextDraft(items.map(validateImportedDraft));
      setSuccess(
        `${items.length} food drafts loaded. Review and approve each one before use.`,
      );
    } catch (err) {
      setError(errorMessage(err));
    }
  }
  async function archive() {
    if (!archiving) return;
    setBusy("archive");
    setError("");
    try {
      await api(
        `/foods/${encodeURIComponent(archiving.id)}?revision=${archiving.revision}`,
        { method: "DELETE" },
      );
      setArchiving(null);
      await onRefresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  const visibleFoods = foods.filter((food) =>
    `${food.name} ${food.brand} ${food.aliases.join(" ")} ${food.category}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const unreviewedStarters = starters.filter(
    (draft) =>
      !foods.some(
        (food) =>
          food.name === draft.name && food.brand === (draft.brand ?? ""),
      ),
  );

  return (
    <div className="catalogue-page">
      {error && !current && (
        <Notice onClose={() => setError("")}>{error}</Notice>
      )}
      {success && (
        <Notice kind="success" onClose={() => setSuccess("")}>
          {success}
        </Notice>
      )}
      {unreviewedStarters.length > 0 && !current && (
        <div className="starter-banner">
          <div className="section-icon">
            <BookOpen size={21} />
          </div>
          <div>
            <h3>A few familiar foods to start with.</h3>
            <p>
              {unreviewedStarters.length} private starter drafts are ready for
              review. Confirm their labels and portion basis before approving.
            </p>
          </div>
          <button
            className="button button-secondary"
            onClick={() => nextDraft(unreviewedStarters)}
          >
            Review drafts
            <ArrowRight size={16} />
          </button>
        </div>
      )}
      <div className="panel catalogue-panel">
        <div className="catalogue-toolbar">
          <div className="search-input">
            <Search size={17} />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Find a food, brand or alias…"
              aria-label="Search your foods"
            />
          </div>
          <div className="toolbar-actions">
            <input
              ref={importInput}
              className="visually-hidden"
              type="file"
              accept="application/json,.json"
              onChange={(event) => {
                void importDrafts(event.target.files?.[0]);
                event.target.value = "";
              }}
            />
            <button
              className="button button-quiet"
              onClick={() => importInput.current?.click()}
              disabled={Boolean(current)}
            >
              <FileUp size={16} />
              Import drafts
            </button>
            <button
              className="button button-primary"
              onClick={() => openFood()}
              disabled={Boolean(current) || !hydrated}
            >
              <Plus size={17} />
              Add food
            </button>
          </div>
        </div>
        {visibleFoods.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state-icon">
              <BookOpen size={28} strokeWidth={1.4} />
            </div>
            <h3>
              {search ? "No foods found." : "Your catalogue starts here."}
            </h3>
            <p>
              {search
                ? "Try another name, brand or alias."
                : "Add a regular food, or photograph its nutrition label. You decide which facts to approve."}
            </p>
            {!search && (
              <button
                className="button button-secondary"
                onClick={() => openFood()}
                disabled={Boolean(current)}
              >
                <Plus size={16} />
                Add your first food
              </button>
            )}
          </div>
        ) : (
          <div className="table-scroll">
            <table className="food-table">
              <thead>
                <tr>
                  <th>Food</th>
                  <th>Nutrition basis</th>
                  <th>Calories</th>
                  <th>Protein</th>
                  <th>Carbs</th>
                  <th>Fat</th>
                  <th>
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visibleFoods.map((food) => (
                  <tr key={food.id}>
                    <td>
                      <strong>{food.name}</strong>
                      <small>
                        {[food.brand, food.category]
                          .filter(Boolean)
                          .join(" · ") || "Your approved food"}
                      </small>
                    </td>
                    <td>
                      <span className="basis-pill">
                        {food.basis_amount} {food.basis_unit}
                      </span>
                      {food.preparation && <small>{food.preparation}</small>}
                    </td>
                    <td className="table-numeric" data-label="Calories">
                      {numberText(food.nutrients.kcal)}
                      <small>kcal</small>
                    </td>
                    <td className="table-numeric" data-label="Protein">
                      {numberText(food.nutrients.protein)}
                      <small>g</small>
                    </td>
                    <td className="table-numeric" data-label="Carbs">
                      {numberText(food.nutrients.carbohydrates)}
                      <small>g</small>
                    </td>
                    <td className="table-numeric" data-label="Fat">
                      {numberText(food.nutrients.fat)}
                      <small>g</small>
                    </td>
                    <td>
                      <div className="entry-actions">
                        <button
                          className="icon-button"
                          onClick={() => openFood(food)}
                          disabled={Boolean(current)}
                          aria-label={`Edit ${food.name}`}
                        >
                          <Pencil size={16} />
                        </button>
                        <button
                          className="icon-button danger-hover"
                          onClick={() => setArchiving(food)}
                          aria-label={`Archive ${food.name}`}
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="table-footer">
          <span>
            {visibleFoods.length} approved{" "}
            {visibleFoods.length === 1 ? "food" : "foods"}
          </span>
          <span>— means unknown, never zero</span>
        </div>
      </div>

      {current && form && (
        <Modal
          title={
            current.editing
              ? "Edit approved food"
              : state.queue.length
                ? `Review food · ${state.queue.length + 1} remaining`
                : "Add a food to your catalogue"
          }
          onClose={() => {
            setState({ current: null, queue: [] });
            setError("");
          }}
          wide
          onPaste={(event) => {
            if (busy) return;
            const image = pastedImage(event.clipboardData);
            if (!image) return;
            event.preventDefault();
            event.stopPropagation();
            void attach(image);
          }}
        >
          <form
            className="food-form"
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
          >
            <div
              className="label-extraction paste-target"
              role="region"
              aria-label="Nutrition label image input"
              aria-describedby="label-paste-hint"
              tabIndex={0}
            >
              <div>
                <Camera size={19} />
                <div>
                  <h3>Start with the label</h3>
                  <p>AI can fill a draft. You approve the facts below.</p>
                </div>
              </div>
              <ImagePicker
                onImage={(file) => void attach(file)}
                disabled={Boolean(busy)}
              />
            </div>
            <p className="paste-hint" id="label-paste-hint">
              Copy a screenshot, focus this box and press <kbd>Ctrl+V</kbd> /{" "}
              <kbd>⌘V</kbd> to paste.
            </p>
            {current.photo && (
              <div className="label-photo-row">
                <img src={current.photo} alt="Nutrition label to review" />
                <div>
                  <strong>{current.photoName}</strong>
                  <p className="field-hint">
                    Using {ai.provider === "chatgpt" ? "ChatGPT" : "llama.cpp"}
                    {ai.model ? ` · ${ai.model}` : ""}. This image is discarded
                    when you approve or cancel.
                  </p>
                  <button
                    type="button"
                    className="button button-secondary"
                    disabled={Boolean(busy)}
                    onClick={() => void extract()}
                  >
                    {busy === "extract" ? (
                      <Busy>Reading label…</Busy>
                    ) : (
                      <>
                        <Sparkles size={15} />
                        {ai.model ? "Read this label" : "Choose a model"}
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    className="text-button"
                    onClick={() =>
                      updateCurrent({ photo: null, photoName: "" })
                    }
                  >
                    Remove photo
                  </button>
                </div>
              </div>
            )}
            {current.warnings.length > 0 && (
              <Notice kind="info">
                <strong>Check before approving</strong>
                <ul>
                  {current.warnings.map((warning, index) => (
                    <li key={index}>{warning}</li>
                  ))}
                </ul>
              </Notice>
            )}
            <div className="form-section">
              <SectionHeading
                eyebrow="01 / IDENTITY"
                title="Which food is this?"
              />
              <div className="form-row">
                <label>
                  Food name <span className="required">*</span>
                  <input
                    required
                    value={form.name}
                    onChange={(event) =>
                      updateForm({ name: event.target.value })
                    }
                    placeholder="e.g. Mango slices"
                  />
                </label>
                <label>
                  Brand
                  <input
                    value={form.brand}
                    onChange={(event) =>
                      updateForm({ brand: event.target.value })
                    }
                    placeholder="e.g. Aldi"
                  />
                </label>
              </div>
              <div className="form-row">
                <label>
                  Category
                  <input
                    value={form.category}
                    onChange={(event) =>
                      updateForm({ category: event.target.value })
                    }
                    placeholder="e.g. Fruit"
                  />
                </label>
                <label>
                  Preparation
                  <input
                    value={form.preparation}
                    onChange={(event) =>
                      updateForm({ preparation: event.target.value })
                    }
                    placeholder="e.g. Raw, peeled / cooked"
                  />
                </label>
              </div>
              <label>
                Names you use
                <input
                  value={current.aliasesText}
                  onChange={(event) =>
                    updateCurrent({ aliasesText: event.target.value })
                  }
                  placeholder="mango, mango pack, aldi mango"
                />
                <span className="field-hint">
                  Separate aliases with commas. These help match your own words.
                </span>
              </label>
            </div>
            <div className="form-section">
              <SectionHeading
                eyebrow="02 / LABEL FACTS"
                title="What are the numbers based on?"
              />
              <div className="basis-fields">
                <span>Nutrition below is for</span>
                <label>
                  <span className="visually-hidden">
                    Nutrition basis amount
                  </span>
                  <input
                    required
                    inputMode="decimal"
                    value={form.basis_amount}
                    onChange={(event) =>
                      updateForm({ basis_amount: event.target.value })
                    }
                    placeholder="100"
                  />
                </label>
                <label>
                  <span className="visually-hidden">Nutrition basis unit</span>
                  <input
                    required
                    value={form.basis_unit}
                    onChange={(event) =>
                      updateForm({ basis_unit: event.target.value })
                    }
                    placeholder="g, ml, pack…"
                    list="basis-units"
                  />
                </label>
                <datalist id="basis-units">
                  <option value="g" />
                  <option value="ml" />
                  <option value="pack" />
                  <option value="piece" />
                  <option value="serving" />
                </datalist>
              </div>
              <p className="field-hint">
                Use the actual label basis. Leave unknown nutrients blank; type
                0 only when the value is zero.
              </p>
              <div className="nutrient-inputs">
                {nutrientKeys.map((key) => (
                  <label key={key}>
                    {nutrientLabels[key]}
                    {key === "kcal" && <span className="required"> *</span>}
                    <div className="unit-input">
                      <input
                        required={key === "kcal"}
                        inputMode="decimal"
                        value={form.nutrients[key] ?? ""}
                        onChange={(event) =>
                          updateForm({
                            nutrients: {
                              ...form.nutrients,
                              [key]: event.target.value || null,
                            },
                          })
                        }
                        placeholder={key === "kcal" ? "Required" : "Unknown"}
                      />
                      <span>{key === "kcal" ? "kcal" : "g"}</span>
                    </div>
                  </label>
                ))}
              </div>
            </div>
            <div className="form-section">
              <SectionHeading
                eyebrow="03 / PORTIONS"
                title="Make the next entry easier"
              />
              <p className="field-hint">
                Only add conversions you have checked. For example, 1 pack = 240
                g when the basis unit is g.
              </p>
              {form.conversions.map((conversion, index) => (
                <div className="conversion-row" key={index}>
                  <label>
                    Amount
                    <input
                      inputMode="decimal"
                      value={conversion.amount}
                      onChange={(event) =>
                        updateForm({
                          conversions: form.conversions.map((item, row) =>
                            row === index
                              ? { ...item, amount: event.target.value }
                              : item,
                          ),
                        })
                      }
                    />
                  </label>
                  <label>
                    Named unit
                    <input
                      value={conversion.unit}
                      onChange={(event) =>
                        updateForm({
                          conversions: form.conversions.map((item, row) =>
                            row === index
                              ? { ...item, unit: event.target.value }
                              : item,
                          ),
                        })
                      }
                      placeholder="pack"
                    />
                  </label>
                  <span className="equals-sign">=</span>
                  <label>
                    {form.basis_unit || "Basis units"}
                    <input
                      inputMode="decimal"
                      value={conversion.basis_amount}
                      onChange={(event) =>
                        updateForm({
                          conversions: form.conversions.map((item, row) =>
                            row === index
                              ? { ...item, basis_amount: event.target.value }
                              : item,
                          ),
                        })
                      }
                    />
                  </label>
                  <button
                    type="button"
                    className="icon-button"
                    onClick={() =>
                      updateForm({
                        conversions: form.conversions.filter(
                          (_, row) => row !== index,
                        ),
                      })
                    }
                    aria-label={`Remove conversion ${index + 1}`}
                  >
                    <X size={16} />
                  </button>
                </div>
              ))}
              <button
                type="button"
                className="text-button"
                onClick={() =>
                  updateForm({
                    conversions: [
                      ...form.conversions,
                      { amount: "1", unit: "", basis_amount: "" },
                    ],
                  })
                }
              >
                <Plus size={15} />
                Add a checked conversion
              </button>
              <div className="usual-serving">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={Boolean(form.usual_serving)}
                    onChange={(event) =>
                      updateForm({
                        usual_serving: event.target.checked
                          ? { quantity: "", unit: form.basis_unit }
                          : null,
                      })
                    }
                  />
                  <span>
                    Use an explicit usual serving when I leave the amount out
                  </span>
                </label>
                {form.usual_serving && (
                  <div className="form-row">
                    <label>
                      Usual amount
                      <input
                        inputMode="decimal"
                        required
                        value={form.usual_serving.quantity}
                        onChange={(event) =>
                          updateForm({
                            usual_serving: {
                              ...form.usual_serving!,
                              quantity: event.target.value,
                            },
                          })
                        }
                      />
                    </label>
                    <label>
                      Unit
                      <input
                        required
                        value={form.usual_serving.unit}
                        onChange={(event) =>
                          updateForm({
                            usual_serving: {
                              ...form.usual_serving!,
                              unit: event.target.value,
                            },
                          })
                        }
                      />
                    </label>
                  </div>
                )}
              </div>
            </div>
            {error && <Notice>{error}</Notice>}
            <div className="food-form-footer">
              <p>
                <Check size={15} />
                Approving saves these facts to your catalogue.
              </p>
              <div className="modal-actions">
                {state.queue.length > 0 && (
                  <button
                    type="button"
                    className="button button-quiet"
                    onClick={() => nextDraft(state.queue)}
                    disabled={Boolean(busy)}
                  >
                    Skip draft
                  </button>
                )}
                <button
                  type="button"
                  className="button button-quiet"
                  onClick={() => {
                    setState({ current: null, queue: [] });
                    setError("");
                  }}
                  disabled={Boolean(busy)}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="button button-primary"
                  disabled={Boolean(busy)}
                >
                  {busy === "save" ? (
                    <Busy>Saving…</Busy>
                  ) : (
                    <>
                      <Check size={16} />
                      {current.editing
                        ? "Save reviewed changes"
                        : "Approve food"}
                    </>
                  )}
                </button>
              </div>
            </div>
          </form>
        </Modal>
      )}
      {archiving && (
        <Modal title="Archive this food?" onClose={() => setArchiving(null)}>
          <p className="modal-copy">
            <strong>{archiving.name}</strong> will leave the active catalogue.
            Its existing diary entries and historical nutrition are preserved.
          </p>
          {error && <Notice>{error}</Notice>}
          <div className="modal-actions">
            <button
              className="button button-quiet"
              onClick={() => setArchiving(null)}
            >
              Keep food
            </button>
            <button
              className="button button-danger"
              onClick={() => void archive()}
              disabled={Boolean(busy)}
            >
              {busy === "archive" ? <Busy /> : "Archive food"}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
