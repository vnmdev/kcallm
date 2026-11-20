import { useEffect, useRef, useState } from "react";
import {
  ArrowDownToLine,
  ArrowUpFromLine,
  Check,
  ChevronRight,
  ExternalLink,
  Link2,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  Unplug,
} from "lucide-react";
import { api, errorMessage, post, put } from "../api";
import { Busy, Modal, Notice, SectionHeading } from "../components/ui";
import {
  nutrientKeys,
  nutrientLabels,
  type AIModel,
  type AIStatus,
  type Provider,
  type Settings,
} from "../types";

type Props = {
  settings: Settings;
  ai: AIStatus;
  onSettings: (settings: Settings) => void;
  onAI: (ai: AIStatus) => void;
  onRefresh: (quiet?: boolean) => Promise<void>;
};
type Connection = {
  authorization_url: string;
  callback_uri: string;
  instructions: unknown;
};

export default function SettingsPage({
  settings,
  ai,
  onSettings,
  onAI,
  onRefresh,
}: Props) {
  const [ordinary, setOrdinary] = useState(settings);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [provider, setProvider] = useState<Provider>(ai.provider);
  const [model, setModel] = useState(ai.model);
  const [endpoint, setEndpoint] = useState(ai.llama_base_url);
  const [aiDirty, setAIDirty] = useState(false);
  const [models, setModels] = useState<AIModel[]>([]);
  const [modelsLoaded, setModelsLoaded] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [connection, setConnection] = useState<Connection | null>(null);
  const [restoreData, setRestoreData] = useState<unknown>(null);
  const [restoreCounts, setRestoreCounts] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [restoreName, setRestoreName] = useState("");
  const [replaceChecked, setReplaceChecked] = useState(false);
  const restoreInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!settingsDirty) setOrdinary(settings);
  }, [settings, settingsDirty]);
  useEffect(() => {
    if (!aiDirty) {
      setProvider(ai.provider);
      setModel(ai.model);
      setEndpoint(ai.llama_base_url);
    }
  }, [ai.provider, ai.model, ai.llama_base_url, aiDirty]);

  function begin(action: string) {
    setBusy(action);
    setError("");
    setSuccess("");
  }

  async function saveOrdinary() {
    begin("settings");
    try {
      const result = await put<Settings>("/settings", ordinary);
      setOrdinary(result);
      setSettingsDirty(false);
      onSettings(result);
      await onRefresh(true);
      setSuccess("Your targets and date settings are saved.");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function saveAI() {
    begin("ai");
    try {
      const status = await put<AIStatus>("/ai/settings", {
        provider,
        model,
        llama_base_url: endpoint,
      });
      onAI(status);
      setAIDirty(false);
      setSuccess("Your AI connection settings are saved.");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function discoverModels() {
    begin("models");
    setModelsLoaded(false);
    try {
      const result = await api<{ models: AIModel[] }>(
        `/ai/models?provider=${provider}`,
      );
      setModels(result.models);
      setModelsLoaded(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function connect() {
    begin("connect");
    try {
      setConnection(await post<Connection>("/ai/chatgpt/connect", {}));
      await onRefresh(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function disconnect() {
    begin("disconnect");
    try {
      const status = await post<AIStatus>("/ai/chatgpt/disconnect", {});
      onAI(status);
      setConnection(null);
      setSuccess("This app’s ChatGPT connection has been removed.");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function download() {
    begin("export");
    try {
      const data = await api<unknown>("/export");
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `kcallm-backup-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setSuccess(
        "Your backup was prepared for download. It contains no provider credentials or photos.",
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function inspectRestore(file?: File) {
    if (!file) return;
    begin("restore-preview");
    try {
      if (file.size > 50 * 1024 * 1024)
        throw new Error("Choose a backup smaller than 50 MB.");
      const data: unknown = JSON.parse(await file.text());
      const counts = await post<Record<string, unknown>>("/restore/preview", {
        data,
      });
      setRestoreData(data);
      setRestoreCounts(counts);
      setRestoreName(file.name);
      setReplaceChecked(false);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function restore() {
    if (!replaceChecked || !restoreData) return;
    begin("restore");
    try {
      await post("/restore", { replace: true, data: restoreData });
      setRestoreData(null);
      setRestoreCounts(null);
      setSettingsDirty(false);
      await onRefresh();
      setSuccess(
        "Backup restored. Your catalogue, diary and ordinary settings have been replaced.",
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  const chatgpt = ai.providers.find((item) => item.id === "chatgpt");
  const llama = ai.providers.find((item) => item.id === "llama");
  const selectedModel = models.find((item) => item.id === model);

  return (
    <div className="settings-page">
      {error && <Notice onClose={() => setError("")}>{error}</Notice>}
      {success && (
        <Notice kind="success" onClose={() => setSuccess("")}>
          {success}
        </Notice>
      )}
      <div className="settings-grid">
        <section className="panel settings-panel">
          <SectionHeading
            eyebrow="OPTIONAL, ALWAYS YOUR CHOICE"
            title="Daily targets"
          />
          <p className="section-copy">
            Enter targets you already use, or leave them blank. KcaLLM records
            your food without setting goals for you.
          </p>
          <div className="nutrient-inputs">
            {nutrientKeys.map((key) => (
              <label key={key}>
                {nutrientLabels[key]}
                <div className="unit-input">
                  <input
                    aria-label={`${nutrientLabels[key]} daily target`}
                    inputMode="decimal"
                    value={ordinary.goals[key] ?? ""}
                    placeholder="Not set"
                    onChange={(event) => {
                      setSettingsDirty(true);
                      setOrdinary((previous) => ({
                        ...previous,
                        goals: {
                          ...previous.goals,
                          [key]: event.target.value || null,
                        },
                      }));
                    }}
                  />
                  <span>{key === "kcal" ? "kcal" : "g"}</span>
                </div>
              </label>
            ))}
          </div>
          <div className="form-section compact-section">
            <label>
              Home timezone
              <input
                value={ordinary.timezone}
                placeholder="Europe/London"
                onChange={(event) => {
                  setSettingsDirty(true);
                  setOrdinary((previous) => ({
                    ...previous,
                    timezone: event.target.value,
                  }));
                }}
              />
              <span className="field-hint">
                Used to identify today. Existing diary entries keep their
                assigned dates.
              </span>
            </label>
          </div>
          <button
            className="button button-primary"
            disabled={Boolean(busy) || !settingsDirty}
            onClick={() => void saveOrdinary()}
          >
            {busy === "settings" ? (
              <Busy />
            ) : (
              <>
                <Check size={16} />
                Save targets & timezone
              </>
            )}
          </button>
        </section>

        <section className="panel settings-panel">
          <SectionHeading
            eyebrow="BRING YOUR OWN MODEL"
            title="AI connections"
          />
          <p className="section-copy">
            AI proposes foods and quantities. Your catalogue supplies the
            nutrition. Manual logging works without a model.
          </p>
          <div className="connection-cards">
            <div className="connection-card">
              <div className="connection-card-heading">
                <span className="provider-monogram">C</span>
                <strong>ChatGPT</strong>
                <span
                  className={`connection-chip ${chatgpt?.connected ? "connected" : ""}`}
                >
                  {chatgpt?.connected
                    ? "Connected"
                    : ai.auth_pending
                      ? "Awaiting sign-in"
                      : "Not connected"}
                </span>
              </div>
              <p>
                {chatgpt?.message ||
                  "Connect an eligible ChatGPT account using the documented sign-in flow."}
              </p>
              <div className="connection-actions">
                <button
                  className="button button-secondary"
                  onClick={() => void connect()}
                  disabled={Boolean(busy)}
                >
                  {busy === "connect" ? (
                    <Busy />
                  ) : (
                    <>
                      <Link2 size={15} />
                      {chatgpt?.connected ? "Reconnect" : "Connect ChatGPT"}
                    </>
                  )}
                </button>
                {chatgpt?.connected && (
                  <button
                    className="text-button"
                    onClick={() => void disconnect()}
                    disabled={Boolean(busy)}
                  >
                    <Unplug size={14} />
                    Disconnect
                  </button>
                )}
              </div>
            </div>
            <div className="connection-card">
              <div className="connection-card-heading">
                <span className="provider-monogram llama-monogram">L</span>
                <strong>llama.cpp</strong>
                <span
                  className={`connection-chip ${llama?.connected ? "connected" : ""}`}
                >
                  {llama?.connected ? "Configured" : "Not ready"}
                </span>
              </div>
              <p>
                {llama?.message ||
                  "Use your own OpenAI-compatible llama.cpp endpoint. Image support depends on its loaded model."}
              </p>
            </div>
          </div>
          <div className="form-stack">
            <label>
              Default provider
              <select
                value={provider}
                onChange={(event) => {
                  setProvider(event.target.value as Provider);
                  setModel("");
                  setModels([]);
                  setModelsLoaded(false);
                  setAIDirty(true);
                }}
              >
                <option value="chatgpt">ChatGPT</option>
                <option value="llama">llama.cpp</option>
              </select>
            </label>
            <label>
              llama.cpp base URL
              <input
                type="url"
                value={endpoint}
                onChange={(event) => {
                  setEndpoint(event.target.value);
                  setAIDirty(true);
                }}
                placeholder="http://127.0.0.1:8080/v1"
              />
              <span className="field-hint">
                Save a changed endpoint before loading its models. API keys are
                configured privately on the host.
              </span>
            </label>
            <div className="model-discovery">
              <label>
                Model
                <input
                  value={model}
                  onChange={(event) => {
                    setModel(event.target.value);
                    setAIDirty(true);
                  }}
                  placeholder="Choose an available model"
                  list="provider-models"
                />
                <datalist id="provider-models">
                  {models.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </datalist>
              </label>
              <button
                className="button button-secondary"
                disabled={Boolean(busy)}
                onClick={() => void discoverModels()}
              >
                {busy === "models" ? (
                  <LoaderCircle className="spin" size={16} />
                ) : (
                  <RefreshCw size={16} />
                )}
                Load models
              </button>
            </div>
            {modelsLoaded &&
              (models.length ? (
                <div className="model-options">
                  {models.map((item) => (
                    <button
                      key={item.id}
                      className={`model-option ${model === item.id ? "selected" : ""}`}
                      onClick={() => {
                        setModel(item.id);
                        setAIDirty(true);
                      }}
                    >
                      <span>
                        {item.name || item.id}
                        <small>
                          {item.input_modalities?.includes("image")
                            ? "Text + images advertised"
                            : "Image support not advertised"}
                        </small>
                      </span>
                      <ChevronRight size={14} />
                    </button>
                  ))}
                </div>
              ) : (
                <p className="field-hint">
                  The provider returned no available models.
                </p>
              ))}
            {selectedModel && (
              <p className="field-hint">
                Capability metadata is a hint; a real photo request still needs
                to succeed.
              </p>
            )}
            <button
              className="button button-primary"
              disabled={Boolean(busy) || !aiDirty}
              onClick={() => void saveAI()}
            >
              {busy === "ai" ? (
                <Busy />
              ) : (
                <>
                  <Check size={16} />
                  Save AI settings
                </>
              )}
            </button>
          </div>
          <p className="privacy-note">
            <ShieldCheck size={14} />
            No automatic provider switch or paid API fallback.
          </p>
        </section>
      </div>

      <section className="panel backup-panel">
        <div>
          <SectionHeading
            eyebrow="A COPY YOU CONTROL"
            title="Backup & restore"
          />
          <p className="section-copy">
            Export your complete catalogue, diary and ordinary settings as
            versioned JSON. Provider credentials and photos are excluded.
          </p>
        </div>
        <div className="backup-actions">
          <button
            className="button button-secondary"
            disabled={Boolean(busy)}
            onClick={() => void download()}
          >
            {busy === "export" ? (
              <Busy />
            ) : (
              <>
                <ArrowDownToLine size={17} />
                Export backup
              </>
            )}
          </button>
          <input
            ref={restoreInput}
            className="visually-hidden"
            type="file"
            accept="application/json,.json"
            onChange={(event) => {
              void inspectRestore(event.target.files?.[0]);
              event.target.value = "";
            }}
          />
          <button
            className="button button-quiet"
            disabled={Boolean(busy)}
            onClick={() => restoreInput.current?.click()}
          >
            {busy === "restore-preview" ? (
              <Busy />
            ) : (
              <>
                <ArrowUpFromLine size={17} />
                Review a backup
              </>
            )}
          </button>
        </div>
      </section>
      <div className="settings-footnote">
        <ShieldCheck size={20} />
        <p>
          <strong>A private, connected app.</strong>
          <span>
            Your database lives on your host. A backend connection is required
            to save; an unsaved capture stays in this browser until you confirm
            or cancel.
          </span>
        </p>
      </div>

      {connection && (
        <Modal
          title="Connect your ChatGPT account"
          onClose={() => setConnection(null)}
          wide
        >
          <div className="form-stack">
            <Notice kind="info">
              Complete this setup from the desktop browser used for your host’s
              sign-in callback. Your phone can use the connection afterwards.
            </Notice>
            <p className="modal-copy">
              Follow the host instructions below, then open the sign-in page.
              Tokens stay on the application host.
            </p>
            <pre className="connection-instructions">
              {typeof connection.instructions === "string"
                ? connection.instructions
                : JSON.stringify(connection.instructions, null, 2)}
            </pre>
            <p className="field-hint">
              Callback: <code>{connection.callback_uri}</code>
            </p>
            <a
              className="button button-primary"
              href={connection.authorization_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              Open ChatGPT sign-in
              <ExternalLink size={16} />
            </a>
            <button
              className="button button-secondary"
              onClick={() => void onRefresh()}
            >
              <RefreshCw size={16} />
              Check connection
            </button>
            {chatgpt?.connected && (
              <Notice kind="success">
                ChatGPT is connected. Close this dialog and load the models
                available to your account.
              </Notice>
            )}
          </div>
        </Modal>
      )}
      {restoreCounts && (
        <Modal
          title="Review this restore"
          onClose={() => {
            setRestoreCounts(null);
            setRestoreData(null);
          }}
        >
          <div className="form-stack">
            <p className="modal-copy">
              <strong>{restoreName}</strong>
              <br />
              The backend has validated this backup. Restoring replaces your
              current catalogue, diary and ordinary settings.
            </p>
            <pre className="restore-counts">
              {JSON.stringify(restoreCounts, null, 2)}
            </pre>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={replaceChecked}
                onChange={(event) => setReplaceChecked(event.target.checked)}
              />
              <span>Replace the current data with this backup.</span>
            </label>
            {error && <Notice>{error}</Notice>}
            <div className="modal-actions">
              <button
                className="button button-quiet"
                onClick={() => {
                  setRestoreCounts(null);
                  setRestoreData(null);
                }}
              >
                Cancel
              </button>
              <button
                className="button button-danger"
                disabled={!replaceChecked || Boolean(busy)}
                onClick={() => void restore()}
              >
                {busy === "restore" ? <Busy /> : "Replace & restore"}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
