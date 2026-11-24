import { useEffect, useState } from "react";
import { ImagePicker } from "../components/ImagePicker";
import {
  ArrowRight,
  Check,
  ClipboardList,
  Pencil,
  Plus,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { ApiError, api, errorMessage, post, put, readImage } from "../api";
import { loadDraft, saveDraft } from "../draft";
import { pastedImage } from "../clipboard";
import { createUUID } from "../id";
import { Busy, Modal, Notice, NutrientGrid } from "../components/ui";
import {
  foodUnits,
  newDraft,
  nutrientLabels,
  numberText,
  wholeNumberText,
  type AIStatus,
  type Day,
  type DraftLine,
  type Entry,
  type Food,
  type Interpretation,
  type LoggingDraft,
  type NutrientKey,
  type Preview,
  type Provider,
  providerLabels,
  type Settings,
} from "../types";

type Props = {
  date: string;
  day: Day;
  foods: Food[];
  settings: Settings;
  ai: AIStatus;
  loading: boolean;
  onProviderChange: (provider: Provider) => Promise<AIStatus>;
  onSettings: () => void;
  onCatalogue: () => void;
  onRefresh: (quiet?: boolean) => Promise<void>;
  onDate: (date: string) => void;
  onSaved: (day: Day, requestId: string) => void;
};

export default function Today({
  date,
  day,
  foods,
  settings,
  ai,
  loading,
  onProviderChange,
  onSettings,
  onCatalogue,
  onRefresh,
  onDate,
  onSaved,
}: Props) {
  const [draft, setDraft] = useState<LoggingDraft>(() => newDraft(date));
  const [hydrated, setHydrated] = useState(false);
  const [error, setError] = useState("");
  const [storageError, setStorageError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");
  const [manualFood, setManualFood] = useState("");
  const [manualQuantity, setManualQuantity] = useState("");
  const [manualUnit, setManualUnit] = useState("g");
  const [manualOpen, setManualOpen] = useState(false);
  const [editing, setEditing] = useState<Entry | null>(null);
  const [deleting, setDeleting] = useState<Entry | null>(null);
  const hasContent = Boolean(
    draft.text || draft.image_data_url || draft.items.length || draft.pending,
  );
  const locked = Boolean(busy || draft.pending || !hydrated);

  useEffect(() => {
    let active = true;
    loadDraft()
      .then((saved) => {
        if (active && saved) setDraft(saved);
      })
      .catch((err) => {
        if (active) setStorageError(errorMessage(err));
      })
      .finally(() => {
        if (active) setHydrated(true);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (hydrated)
      void saveDraft(draft).catch((err) => setStorageError(errorMessage(err)));
  }, [draft, hydrated]);

  useEffect(() => {
    if (hydrated && !hasContent)
      setDraft((previous) => ({ ...previous, date }));
  }, [date, hydrated, hasContent]);

  useEffect(() => {
    if (
      hydrated &&
      !draft.pending &&
      (!hasContent || (draft.provider === ai.provider && !draft.model))
    ) {
      setDraft((previous) => ({
        ...previous,
        provider: ai.provider,
        model: ai.model,
      }));
    }
  }, [
    ai.provider,
    ai.model,
    hydrated,
    draft.pending,
    draft.provider,
    draft.model,
    hasContent,
  ]);

  function patch(change: Partial<LoggingDraft>) {
    setDraft((previous) => ({
      ...previous,
      ...change,
      preview: null,
      request_id: createUUID(),
    }));
    setError("");
    setMessage("");
  }

  async function attach(file?: File) {
    if (!file) return;
    try {
      patch({ image_data_url: await readImage(file), image_name: file.name });
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  async function switchProvider(provider: Provider) {
    setBusy("provider");
    setError("");
    try {
      const status = await onProviderChange(provider);
      patch({ provider: status.provider, model: status.model });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function interpret() {
    if (!draft.model) {
      onSettings();
      return;
    }
    setBusy("interpret");
    setError("");
    setMessage("");
    try {
      const result = await post<Interpretation>("/ai/interpret", {
        text: draft.text,
        ...(draft.image_data_url
          ? { image_data_url: draft.image_data_url }
          : {}),
        provider: draft.provider,
        model: draft.model,
      });
      setDraft((previous) => ({
        ...previous,
        items: result.items.map((item) => ({
          ...item,
          key: createUUID(),
        })),
        preview: null,
        request_id: createUUID(),
      }));
      setMessage(
        result.message ||
          (result.items.length
            ? "Check each food and amount below."
            : "No foods matched. Choose a saved food manually or add it to your catalogue."),
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  function updateLine(key: string, change: Partial<DraftLine>) {
    patch({
      items: draft.items.map((item) =>
        item.key === key ? { ...item, ...change } : item,
      ),
    });
  }

  function addManual() {
    const food = foods.find((item) => item.id === manualFood);
    if (!food) return;
    patch({
      items: [
        ...draft.items,
        {
          key: createUUID(),
          status: "matched",
          food_id: food.id,
          quantity: manualQuantity || null,
          unit: manualQuantity ? manualUnit : null,
          question: "",
          candidates: [],
        },
      ],
    });
    setManualQuantity("");
    setManualOpen(false);
  }

  async function review() {
    setBusy("preview");
    setError("");
    try {
      const preview = await post<Preview>("/preview", {
        date: draft.date,
        items: draft.items.map((item) => ({
          food_id: item.food_id,
          quantity: item.quantity || null,
          unit: item.unit || null,
        })),
      });
      setDraft((previous) => ({
        ...previous,
        preview,
        request_id: createUUID(),
      }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function confirm() {
    if (!draft.preview && !draft.pending) return;
    setBusy("confirm");
    setError("");
    const body = draft.pending ?? {
      request_id: draft.request_id,
      date: draft.preview!.date,
      items: draft.preview!.items.map(
        ({ food_id, food_revision, quantity, unit }) => ({
          food_id,
          food_revision,
          quantity,
          unit,
        }),
      ),
    };
    const pendingDraft = { ...draft, pending: body };
    let acceptedByBackend = false;
    try {
      // Preserve the exact submitted operation before sending. Retrying this body cannot duplicate a save.
      await saveDraft(pendingDraft);
      setDraft(pendingDraft);
      await post<Day>("/diary/confirm", body);
      acceptedByBackend = true;
      // An idempotent retry can return the original receipt. Fetch current diary state
      // so intervening edits/deletions cannot be visually replaced by that receipt.
      const result = await api<Day>(`/diary?date=${body.date}`);
      const cleared = newDraft(date, draft.provider, draft.model);
      await saveDraft(cleared);
      setDraft(cleared);
      setMessage("");
      onSaved(result, body.request_id);
      if (result.date !== date) onDate(result.date);
    } catch (err) {
      setError(errorMessage(err));
      if (
        !acceptedByBackend &&
        err instanceof ApiError &&
        err.status >= 400 &&
        err.status < 500
      ) {
        setDraft((previous) => ({
          ...previous,
          pending: null,
          ...(err.status === 409 ? { preview: null } : {}),
        }));
      }
    } finally {
      setBusy("");
    }
  }

  async function discard() {
    setBusy("discard");
    const cleared = newDraft(date, draft.provider, draft.model);
    try {
      await saveDraft(cleared);
      setDraft(cleared);
      setError("");
      setMessage("");
      setStorageError("");
    } catch (err) {
      setStorageError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function deleteEntry() {
    if (!deleting) return;
    setBusy("delete");
    setError("");
    try {
      await api(
        `/entries/${encodeURIComponent(deleting.id)}?revision=${deleting.revision}`,
        { method: "DELETE" },
      );
      setDeleting(null);
      await onRefresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  const unresolved = draft.items.some(
    (item) =>
      item.status !== "matched" ||
      !item.food_id ||
      (!item.quantity &&
        !foods.find((food) => food.id === item.food_id)?.usual_serving) ||
      (item.quantity && !item.unit),
  );
  const selectedManual = foods.find((food) => food.id === manualFood);
  const metrics: NutrientKey[] = ["kcal", "protein", "carbohydrates", "fat"];

  return (
    <>
      <section
        className="daily-overview"
        aria-label="Daily nutrition totals"
        aria-busy={loading}
      >
        <div className="metrics-grid">
          {metrics.map((key, index) => (
            <article
              className={`metric-card ${index === 0 ? "metric-primary" : ""}`}
              key={key}
            >
              <div className="metric-label">
                <span className={`metric-marker marker-${key}`} />
                {nutrientLabels[key]}
                <span>{key === "kcal" ? "kcal" : "grams"}</span>
              </div>
              <p className="metric-value" title={day.totals[key] ?? "Unknown"}>
                {loading ? "—" : wholeNumberText(day.totals[key])}
                {day.incomplete.includes(key) && (
                  <sup title="Incomplete total">*</sup>
                )}
              </p>
              {settings.goals[key] ? (
                <div className="metric-target">
                  <meter
                    min="0"
                    max={settings.goals[key]!}
                    value={day.totals[key] ?? "0"}
                    aria-label={`${nutrientLabels[key]} target progress`}
                  />
                  <span>
                    of {numberText(settings.goals[key])}{" "}
                    {key === "kcal" ? "kcal" : "g"} target
                  </span>
                  {day.remaining?.[key] != null && (
                    <span className="remaining-value">
                      {wholeNumberText(day.remaining[key]!.replace(/^-/, ""))}{" "}
                      {key === "kcal" ? "kcal" : "g"}{" "}
                      {day.remaining[key]!.startsWith("-")
                        ? "over target"
                        : "remaining"}
                    </span>
                  )}
                </div>
              ) : (
                <div className="metric-target">
                  <span className="empty-meter" />
                  <span>
                    Daily total <span className="target-separator">·</span> No
                    target set
                  </span>
                </div>
              )}
            </article>
          ))}
        </div>
        <div className="overview-meta">
          <span>
            {day.entries.length}{" "}
            {day.entries.length === 1 ? "entry" : "entries"} recorded{" "}
            {day.incomplete.length > 0 && (
              <span className="incomplete-label">
                · * Some nutrient values are missing
              </span>
            )}
          </span>
        </div>
      </section>

      <div className="today-columns">
        <section
          className="panel capture-panel"
          aria-labelledby="capture-heading"
          onPaste={(event) => {
            if (locked) return;
            const image = pastedImage(event.clipboardData);
            if (!image) return;
            event.preventDefault();
            event.stopPropagation();
            void attach(image);
          }}
        >
          <div className="panel-heading">
            <div className="section-icon">
              <Plus size={20} />
            </div>
            <div>
              <h2 id="capture-heading">What did you have?</h2>
              <p>A few words or a photo of your scale.</p>
            </div>
            <span className="quiet-tag">QUICK ENTRY</span>
          </div>
          {storageError && <Notice>{storageError}</Notice>}
          {draft.pending && (
            <Notice kind="info">
              This save has not been verified. Retry the same save below; it
              will not create a duplicate. Keep this draft until the result is
              confirmed.
            </Notice>
          )}
          {draft.date !== date && hasContent && (
            <Notice kind="info">
              Your unsaved draft is for <strong>{draft.date}</strong>.{" "}
              {!locked && (
                <button
                  className="inline-button"
                  onClick={() => patch({ date })}
                >
                  Move it to {date}
                </button>
              )}
            </Notice>
          )}
          <div
            className="composer-box paste-target"
            role="region"
            aria-label="Food image input"
            aria-describedby="capture-paste-hint"
            tabIndex={0}
          >
            <textarea
              value={draft.text}
              onChange={(event) => patch({ text: event.target.value })}
              disabled={locked}
              placeholder="e.g. 180g potato and 2 mango packs"
              aria-label="Describe your food and quantity"
              rows={3}
            />
            {draft.image_data_url && (
              <div className="photo-preview">
                <img
                  src={draft.image_data_url}
                  alt="Your attached food or scale photo"
                />
                <div>
                  <strong>{draft.image_name || "Attached photo"}</strong>
                  <small>Kept only until you confirm or cancel.</small>
                </div>
                <button
                  className="icon-button"
                  onClick={() =>
                    patch({ image_data_url: null, image_name: "" })
                  }
                  disabled={locked}
                  aria-label="Remove attached photo"
                >
                  <X size={16} />
                </button>
              </div>
            )}
            <div className="composer-toolbar">
              <ImagePicker
                onImage={(file) => void attach(file)}
                disabled={locked}
              />
            </div>
          </div>
          <p className="paste-hint" id="capture-paste-hint">
            Or paste a screenshot here with <kbd>Ctrl+V</kbd> / <kbd>⌘V</kbd>.
          </p>
          <div className="provider-row">
            <span className="provider-label">
              <Sparkles size={14} />
              Interpret with
            </span>
            {ai.demo ? (
              <span className="model-label" title={ai.model}>
                the demo’s hosted model
              </span>
            ) : (
              <>
                <select
                  aria-label="AI provider for this entry"
                  value={draft.provider}
                  onChange={(event) =>
                    void switchProvider(event.target.value as Provider)
                  }
                  disabled={locked}
                >
                  {(Object.keys(providerLabels) as Provider[])
                    .filter(
                      (id) =>
                        id !== "openrouter" ||
                        ai.providers.some(
                          (item) => item.id === id && item.connected,
                        ),
                    )
                    .map((id) => (
                      <option key={id} value={id}>
                        {providerLabels[id]}
                      </option>
                    ))}
                </select>
                <button
                  className="model-label"
                  onClick={onSettings}
                  title={draft.model || "Choose a model in Settings"}
                >
                  {draft.model || "Choose model"}
                  <ArrowRight size={12} />
                </button>
              </>
            )}
          </div>
          {ai.demo && (
            <p className="privacy-note">
              Demo: your text or photo is sent to a hosted AI model. Please
              don’t enter personal information.
            </p>
          )}
          {!ai.demo && draft.provider === "chatgpt" && (
            <p className="privacy-note">
              Your text, photo and food catalogue are sent to ChatGPT when you
              interpret.
            </p>
          )}
          {!ai.demo && draft.provider === "llama" && (
            <p className="privacy-note">
              Your input is sent only to your configured custom endpoint.
            </p>
          )}
          {!ai.demo && draft.provider === "openrouter" && (
            <p className="privacy-note">
              Your text, photo and food catalogue are sent to OpenRouter and the
              chosen model’s provider when you interpret.
            </p>
          )}
          <div className="capture-actions">
            <button
              className="button button-primary"
              onClick={() => void interpret()}
              disabled={locked || (!draft.text.trim() && !draft.image_data_url)}
            >
              {busy === "interpret" ? (
                <Busy>Interpreting…</Busy>
              ) : (
                <>
                  <Sparkles size={16} />
                  {draft.model ? "Interpret entry" : "Choose a model"}
                </>
              )}
            </button>
            <button
              className="button button-quiet"
              onClick={() => setManualOpen(!manualOpen)}
              disabled={locked}
            >
              <ClipboardList size={16} />
              Choose a food
            </button>
          </div>

          {manualOpen && (
            <fieldset className="manual-entry" disabled={locked}>
              <label>
                Saved food
                <select
                  aria-label="Saved food"
                  value={manualFood}
                  onChange={(event) => {
                    const food = foods.find(
                      (item) => item.id === event.target.value,
                    );
                    setManualFood(event.target.value);
                    setManualUnit(
                      food?.usual_serving?.unit ?? food?.basis_unit ?? "g",
                    );
                  }}
                >
                  <option value="">Select a food…</option>
                  {foods.map((food) => (
                    <option key={food.id} value={food.id}>
                      {food.name}
                      {food.brand ? ` · ${food.brand}` : ""}
                    </option>
                  ))}
                </select>
              </label>
              <div className="form-row">
                <label>
                  Amount
                  <input
                    type="text"
                    inputMode="decimal"
                    value={manualQuantity}
                    placeholder={
                      selectedManual?.usual_serving
                        ? `Usual: ${selectedManual.usual_serving.quantity}`
                        : "e.g. 180"
                    }
                    onChange={(event) => setManualQuantity(event.target.value)}
                  />
                </label>
                <label>
                  Unit
                  <select
                    value={manualUnit}
                    onChange={(event) => setManualUnit(event.target.value)}
                  >
                    {foodUnits(selectedManual).map((unit) => (
                      <option key={unit}>{unit}</option>
                    ))}
                  </select>
                </label>
              </div>
              {selectedManual?.usual_serving && (
                <p className="field-hint">
                  Leave amount empty to use your saved serving:{" "}
                  {selectedManual.usual_serving.quantity}{" "}
                  {selectedManual.usual_serving.unit}.
                </p>
              )}
              <button
                className="button button-secondary"
                disabled={!manualFood}
                onClick={addManual}
              >
                <Plus size={15} />
                Add to review
              </button>
              {!foods.length && (
                <button className="text-button" onClick={onCatalogue}>
                  Add your first food
                  <ArrowRight size={15} />
                </button>
              )}
            </fieldset>
          )}
          {error && <Notice onClose={() => setError("")}>{error}</Notice>}
          {message && <p className="interpretation-message">{message}</p>}

          {draft.items.length > 0 && (
            <div className="review-section">
              <div className="review-heading">
                <h3>
                  {draft.preview
                    ? "Ready for your check"
                    : "Check the foods & amounts"}
                </h3>
                <span>
                  {draft.items.length}{" "}
                  {draft.items.length === 1 ? "food" : "foods"}
                </span>
              </div>
              {!draft.preview &&
                draft.items.map((item, index) => (
                  <div
                    className={`proposal-line ${item.status !== "matched" || !item.food_id ? "needs-attention" : ""}`}
                    key={item.key}
                  >
                    <div className="proposal-top">
                      <span className="row-number">
                        {String(index + 1).padStart(2, "0")}
                      </span>
                      <label
                        className="visually-hidden"
                        htmlFor={`food-${item.key}`}
                      >
                        Food {index + 1}
                      </label>
                      <select
                        id={`food-${item.key}`}
                        value={item.food_id ?? ""}
                        disabled={locked}
                        onChange={(event) => {
                          const food = foods.find(
                            (value) => value.id === event.target.value,
                          );
                          updateLine(item.key, {
                            food_id: event.target.value || null,
                            status: "matched",
                            question: "",
                            unit: item.quantity
                              ? (food?.basis_unit ?? null)
                              : null,
                          });
                        }}
                      >
                        <option value="">Choose the matching food…</option>
                        {foods.map((food) => (
                          <option key={food.id} value={food.id}>
                            {food.name}
                            {food.brand ? ` · ${food.brand}` : ""}
                          </option>
                        ))}
                      </select>
                      <button
                        className="icon-button"
                        onClick={() =>
                          patch({
                            items: draft.items.filter(
                              (row) => row.key !== item.key,
                            ),
                          })
                        }
                        disabled={locked}
                        aria-label={`Remove proposed food ${index + 1}`}
                      >
                        <X size={16} />
                      </button>
                    </div>
                    {item.question && (
                      <p className="field-hint attention-text">
                        {item.question}
                      </p>
                    )}
                    {item.status !== "matched" &&
                      item.candidates.length > 0 && (
                        <div
                          className="food-suggestions"
                          role="group"
                          aria-label={`Suggested foods for item ${index + 1}`}
                        >
                          <span className="field-hint">
                            Possible matches — check before using
                          </span>
                          {item.candidates.map((id, rank) => {
                            const candidate = foods.find(
                              (food) => food.id === id,
                            );
                            if (!candidate) return null;
                            return (
                              <button
                                type="button"
                                className="button button-secondary"
                                key={id}
                                disabled={locked}
                                aria-pressed={item.food_id === id}
                                onClick={() =>
                                  updateLine(item.key, {
                                    food_id: id,
                                    status: "clarification",
                                    unit:
                                      item.unit &&
                                      foodUnits(candidate).includes(item.unit)
                                        ? item.unit
                                        : null,
                                  })
                                }
                              >
                                <span>
                                  {candidate.name}
                                  {candidate.preparation
                                    ? ` · ${candidate.preparation}`
                                    : ""}
                                </span>
                                {rank === 0 && <small>Best guess</small>}
                              </button>
                            );
                          })}
                        </div>
                      )}
                    <div className="proposal-amount">
                      <label>
                        Amount
                        <input
                          type="text"
                          inputMode="decimal"
                          value={item.quantity ?? ""}
                          placeholder="Ask / usual serving"
                          disabled={locked}
                          onChange={(event) =>
                            updateLine(item.key, {
                              quantity: event.target.value || null,
                              unit:
                                item.unit ??
                                foods.find((food) => food.id === item.food_id)
                                  ?.basis_unit ??
                                null,
                            })
                          }
                        />
                      </label>
                      <label>
                        Unit
                        <select
                          value={item.unit ?? ""}
                          disabled={locked}
                          onChange={(event) =>
                            updateLine(item.key, {
                              unit: event.target.value || null,
                            })
                          }
                        >
                          <option value="">Saved serving</option>
                          {foodUnits(
                            foods.find((food) => food.id === item.food_id),
                          ).map((unit) => (
                            <option key={unit}>{unit}</option>
                          ))}
                        </select>
                      </label>
                    </div>
                    {!item.quantity &&
                      foods.find((food) => food.id === item.food_id)
                        ?.usual_serving && (
                        <p className="field-hint">
                          Your explicit usual serving will be shown in the
                          preview.
                        </p>
                      )}
                    {item.status !== "matched" && item.food_id && (
                      <button
                        className="text-button resolve-match"
                        disabled={
                          locked ||
                          (!item.quantity &&
                            !foods.find((food) => food.id === item.food_id)
                              ?.usual_serving) ||
                          Boolean(item.quantity && !item.unit)
                        }
                        onClick={() =>
                          updateLine(item.key, {
                            status: "matched",
                            question: "",
                          })
                        }
                      >
                        <Check size={14} />
                        I've checked this food and amount
                      </button>
                    )}
                  </div>
                ))}
              {draft.preview && (
                <>
                  <div className="preview-lines">
                    {draft.preview.items.map((item, index) => (
                      <div
                        className="preview-line"
                        key={`${item.food_id}-${index}`}
                      >
                        <div>
                          <strong>{item.food_name}</strong>
                          <span>
                            {item.brand && `${item.brand} · `}
                            {item.quantity} {item.unit}
                          </span>
                        </div>
                        <b>
                          {numberText(item.nutrients.kcal)}
                          <small>kcal</small>
                        </b>
                      </div>
                    ))}
                  </div>
                  <div className="preview-totals">
                    <span className="eyebrow">THIS ENTRY</span>
                    <NutrientGrid
                      nutrients={draft.preview.totals}
                      incomplete={draft.preview.incomplete}
                      compact
                    />
                  </div>
                  <p className="field-hint">
                    Calculated from your saved food data.{" "}
                    {draft.preview.incomplete.length > 0 &&
                      "* Some nutrient values are unknown."}
                  </p>
                </>
              )}
              <div className="review-actions">
                {draft.preview || draft.pending ? (
                  <>
                    <button
                      className="button button-primary"
                      onClick={() => void confirm()}
                      disabled={Boolean(busy)}
                    >
                      {busy === "confirm" ? (
                        <Busy>Saving…</Busy>
                      ) : (
                        <>
                          <Check size={17} />
                          {draft.pending ? "Retry this save" : "Confirm & save"}
                        </>
                      )}
                    </button>
                    <button
                      className="button button-quiet"
                      disabled={locked}
                      onClick={() =>
                        setDraft((previous) => ({ ...previous, preview: null }))
                      }
                    >
                      <Pencil size={15} />
                      Edit
                    </button>
                  </>
                ) : (
                  <button
                    className="button button-primary"
                    onClick={() => void review()}
                    disabled={locked || unresolved}
                  >
                    {busy === "preview" ? (
                      <Busy>Calculating…</Busy>
                    ) : (
                      <>
                        Preview nutrition
                        <ArrowRight size={16} />
                      </>
                    )}
                  </button>
                )}
              </div>
              {unresolved && !draft.preview && (
                <p className="field-hint attention-text">
                  Resolve each food and amount, or remove its row, before
                  previewing.
                </p>
              )}
            </div>
          )}
          {hasContent && !draft.pending && (
            <div className="draft-footer">
              <span>
                <span className="status-dot" />
                Unsaved draft{storageError ? "" : " · kept on this device"}
              </span>
              <button
                className="text-button"
                disabled={locked}
                onClick={() => void discard()}
              >
                Cancel draft
              </button>
            </div>
          )}
        </section>

        <section className="panel diary-panel" aria-labelledby="diary-heading">
          <div className="panel-heading">
            <div>
              <h2 id="diary-heading">Your diary</h2>
              <p>Everything that makes up your day.</p>
            </div>
            <span className="count-badge">{day.entries.length}</span>
          </div>
          {loading ? (
            <div className="empty-state">
              <Busy>Loading your diary…</Busy>
            </div>
          ) : day.entries.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-icon">
                <ClipboardList size={28} strokeWidth={1.4} />
              </div>
              <h3>A fresh page for the day.</h3>
              <p>
                Your confirmed foods will appear here.
                <br />
                Start with something you’ve eaten.
              </p>
              <span className="empty-state-rule" />
            </div>
          ) : (
            <div className="diary-list">
              {day.entries.map((entry) => (
                <article className="diary-entry" key={entry.id}>
                  <div className="entry-top">
                    <div>
                      <h3>{entry.food_name}</h3>
                      <p>
                        {entry.brand && `${entry.brand} · `}
                        {entry.quantity} {entry.unit}
                      </p>
                    </div>
                    <div className="entry-kcal">
                      {numberText(entry.nutrients.kcal)}
                      <span>kcal</span>
                    </div>
                  </div>
                  <div className="entry-bottom">
                    <div className="entry-macros">
                      <span>
                        <i className="marker-protein" />P{" "}
                        <b>{numberText(entry.nutrients.protein)}</b>g
                      </span>
                      <span>
                        <i className="marker-carbohydrates" />C{" "}
                        <b>{numberText(entry.nutrients.carbohydrates)}</b>g
                      </span>
                      <span>
                        <i className="marker-fat" />F{" "}
                        <b>{numberText(entry.nutrients.fat)}</b>g
                      </span>
                    </div>
                    <div className="entry-actions">
                      <button
                        className="icon-button"
                        onClick={() => setEditing(entry)}
                        aria-label={`Edit ${entry.food_name}`}
                      >
                        <Pencil size={14} />
                      </button>
                      <button
                        className="icon-button danger-hover"
                        onClick={() => setDeleting(entry)}
                        aria-label={`Delete ${entry.food_name}`}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </div>
                  <details className="entry-detail">
                    <summary>All nutrients</summary>
                    <NutrientGrid nutrients={entry.nutrients} compact />
                  </details>
                </article>
              ))}
            </div>
          )}
          <div className="diary-footer">
            <span>Based on your approved catalogue</span>
            <BookOpenLink onClick={onCatalogue} />
          </div>
        </section>
      </div>
      {editing && (
        <EditEntry
          entry={editing}
          foods={foods}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await onRefresh();
          }}
        />
      )}
      {deleting && (
        <Modal title="Remove this entry?" onClose={() => setDeleting(null)}>
          <p className="modal-copy">
            <strong>{deleting.food_name}</strong> · {deleting.quantity}{" "}
            {deleting.unit}
            <br />
            This removes it from the diary and recalculates this day’s totals.
          </p>
          <div className="modal-actions">
            <button
              className="button button-quiet"
              onClick={() => setDeleting(null)}
            >
              Keep entry
            </button>
            <button
              className="button button-danger"
              disabled={busy === "delete"}
              onClick={() => void deleteEntry()}
            >
              {busy === "delete" ? <Busy /> : "Remove entry"}
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}

function BookOpenLink({ onClick }: { onClick: () => void }) {
  return (
    <button className="text-button" onClick={onClick}>
      My foods
      <ArrowRight size={14} />
    </button>
  );
}

function EditEntry({
  entry,
  foods,
  onClose,
  onSaved,
}: {
  entry: Entry;
  foods: Food[];
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [foodId, setFoodId] = useState(entry.food_id);
  const [quantity, setQuantity] = useState(entry.quantity);
  const [unit, setUnit] = useState(entry.unit);
  const [date, setDate] = useState(entry.date);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [stale, setStale] = useState(false);
  const matchingFood = foods.find((food) => food.id === foodId);
  const units = [...new Set([unit, ...foodUnits(matchingFood)])];

  async function review() {
    setBusy(true);
    setError("");
    try {
      const result = await post<Preview>("/preview", {
        date,
        items: [{ entry_id: entry.id, food_id: foodId, quantity, unit }],
      });
      if (result.items[0].entry_revision !== entry.revision) {
        setStale(true);
        throw new Error(
          "This entry changed while you were editing. Close and refresh before reviewing the latest version.",
        );
      }
      setPreview(result);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    if (!preview) return;
    setBusy(true);
    setError("");
    try {
      const row = preview.items[0];
      await put(`/entries/${encodeURIComponent(entry.id)}`, {
        revision: entry.revision,
        date: preview.date,
        food_id: row.food_id,
        food_revision: row.food_revision,
        quantity: row.quantity,
        unit: row.unit,
      });
      await onSaved();
    } catch (err) {
      setError(errorMessage(err));
      if (err instanceof ApiError && err.status === 409) setPreview(null);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Edit diary entry" onClose={onClose}>
      <div className="form-stack">
        <p className="field-hint">
          Changing the amount uses the entry’s original nutrition facts.
          Choosing another food uses that food’s current facts.
        </p>
        <label>
          Food
          <select
            value={foodId}
            onChange={(event) => {
              setFoodId(event.target.value);
              setUnit(
                foods.find((food) => food.id === event.target.value)
                  ?.basis_unit ?? entry.unit,
              );
              setPreview(null);
            }}
          >
            {!foods.some((food) => food.id === entry.food_id) && (
              <option value={entry.food_id}>
                {entry.food_name} (archived)
              </option>
            )}
            {foods.map((food) => (
              <option key={food.id} value={food.id}>
                {food.name}
              </option>
            ))}
          </select>
        </label>
        <div className="form-row">
          <label>
            Amount
            <input
              value={quantity}
              inputMode="decimal"
              onChange={(event) => {
                setQuantity(event.target.value);
                setPreview(null);
              }}
            />
          </label>
          <label>
            Unit
            <select
              value={unit}
              onChange={(event) => {
                setUnit(event.target.value);
                setPreview(null);
              }}
            >
              {units.map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
          </label>
        </div>
        <label>
          Diary date
          <input
            type="date"
            value={date}
            onChange={(event) => {
              setDate(event.target.value);
              setPreview(null);
            }}
          />
        </label>
        {error && <Notice>{error}</Notice>}
        {preview && (
          <NutrientGrid
            nutrients={preview.totals}
            incomplete={preview.incomplete}
            compact
          />
        )}
        <div className="modal-actions">
          <button className="button button-quiet" onClick={onClose}>
            Cancel
          </button>
          {stale ? (
            <button
              className="button button-primary"
              onClick={() => void onSaved()}
            >
              Close & refresh
            </button>
          ) : (
            <button
              className="button button-primary"
              disabled={busy || !quantity || !date}
              onClick={() => void (preview ? save() : review())}
            >
              {busy ? <Busy /> : preview ? "Save changes" : "Preview changes"}
            </button>
          )}
        </div>
      </div>
    </Modal>
  );
}
