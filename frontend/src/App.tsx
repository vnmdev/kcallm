import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowUpRight,
  BookOpen,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  LayoutDashboard,
  Leaf,
  Settings2,
  Undo2,
} from "lucide-react";
import { api, errorMessage, post, put } from "./api";
import { Notice } from "./components/ui";
import Today from "./pages/Today";
import Catalogue from "./pages/Catalogue";
import SettingsPage from "./pages/Settings";
import {
  emptyNutrients,
  localDate,
  type AIStatus,
  type Day,
  type Food,
  type Provider,
  type Settings,
} from "./types";

type Tab = "today" | "foods" | "settings";
const initialStatus: AIStatus = {
  provider: "chatgpt",
  model: "",
  llama_base_url: "",
  providers: [],
  auth_pending: false,
};

export default function App() {
  const [tab, setTab] = useState<Tab>("today");
  const [date, setDate] = useState(localDate());
  const selectedDate = useRef(date);
  const refreshSequence = useRef(0);
  const [foods, setFoods] = useState<Food[]>([]);
  const [day, setDay] = useState<Day>({
    date,
    entries: [],
    totals: emptyNutrients(),
    incomplete: [],
  });
  const [settings, setSettings] = useState<Settings>({
    revision: 0,
    timezone: "Europe/London",
    goals: emptyNutrients(),
  });
  const [ai, setAI] = useState(initialStatus);
  const [online, setOnline] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState<{ id: string; date: string } | null>(null);

  const refresh = useCallback(
    async (quiet = false) => {
      const requestSequence = ++refreshSequence.current;
      const results = await Promise.allSettled([
        api<Food[]>("/foods"),
        api<Day>(`/diary?date=${date}`),
        api<Settings>("/settings"),
        api<AIStatus>("/ai/status"),
      ]);
      if (
        requestSequence !== refreshSequence.current ||
        date !== selectedDate.current
      )
        return;
      const [foodResult, dayResult, settingsResult, aiResult] = results;
      if (foodResult.status === "fulfilled") setFoods(foodResult.value);
      if (dayResult.status === "fulfilled") setDay(dayResult.value);
      if (settingsResult.status === "fulfilled")
        setSettings(settingsResult.value);
      if (aiResult.status === "fulfilled") setAI(aiResult.value);
      const backendAvailable = dayResult.status === "fulfilled";
      setOnline(backendAvailable);
      if (!quiet && !backendAvailable)
        setError(
          errorMessage(
            dayResult.status === "rejected" ? dayResult.reason : null,
          ),
        );
      if (backendAvailable) setError("");
      setLoading(false);
    },
    [date],
  );

  useEffect(() => {
    setLoading(true);
    void refresh();
    const onFocus = () => {
      if (document.visibilityState === "visible") void refresh(true);
    };
    const timer = window.setInterval(onFocus, 15000);
    document.addEventListener("visibilitychange", onFocus);
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onFocus);
      window.removeEventListener("focus", onFocus);
    };
  }, [refresh]);

  async function chooseProvider(provider: Provider): Promise<AIStatus> {
    const next = await put<AIStatus>("/ai/settings", { provider, model: "" });
    setAI(next);
    return next;
  }

  function changeDate(offset: number) {
    const next = new Date(`${date}T12:00:00Z`);
    next.setUTCDate(next.getUTCDate() + offset);
    selectDate(next.toISOString().slice(0, 10));
  }

  function selectDate(next: string) {
    // Invalidate immediately, before React renders or the next effect starts.
    selectedDate.current = next;
    refreshSequence.current += 1;
    setDate(next);
  }

  async function undo() {
    if (!saved) return;
    try {
      await post(`/batches/${encodeURIComponent(saved.id)}/undo`, {});
      setSaved(null);
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  const isToday = date === localDate(settings.timezone);
  const dateLabel = new Intl.DateTimeFormat("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: "UTC",
  }).format(new Date(`${date}T12:00:00Z`));

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a
          href="#today"
          className="brand"
          onClick={(event) => {
            event.preventDefault();
            setTab("today");
          }}
          aria-label="KcaLLM home"
        >
          <span className="brand-icon">
            <Leaf size={22} strokeWidth={1.8} />
          </span>
          <span>
            Kca<span className="brand-ending">LLM</span>
          </span>
        </a>
        <nav aria-label="Main navigation">
          <button
            className={tab === "today" ? "active" : ""}
            onClick={() => setTab("today")}
          >
            <LayoutDashboard size={19} />
            Today
            <span className="nav-indicator" />
          </button>
          <button
            className={tab === "foods" ? "active" : ""}
            onClick={() => setTab("foods")}
          >
            <BookOpen size={19} />
            My foods<span className="nav-count">{foods.length}</span>
          </button>
          <button
            className={tab === "settings" ? "active" : ""}
            onClick={() => setTab("settings")}
          >
            <Settings2 size={19} />
            Settings
          </button>
        </nav>
        <div className="sidebar-bottom">
          <button className="text-button" onClick={() => setTab("settings")}>
            <CircleHelp size={15} />
            Connections & backup
            <ArrowUpRight size={14} />
          </button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <span className="breadcrumb">
            {tab === "today"
              ? "Daily diary"
              : tab === "foods"
                ? "Food catalogue"
                : "Settings"}
          </span>
          <span
            className={`connection-label ${online === false ? "offline" : ""}`}
          >
            <span className="status-dot" />
            {online === null
              ? "Connecting"
              : online
                ? "Backend connected"
                : "Backend unavailable"}
          </span>
        </header>
        <div className="page-content">
          {tab === "today" && (
            <div className="page-heading">
              <div />
              <div className="date-control">
                <button
                  className="icon-button"
                  onClick={() => changeDate(-1)}
                  aria-label="Previous day"
                >
                  <ChevronLeft size={17} />
                </button>
                <label>
                  <span>{isToday ? "Today" : dateLabel}</span>
                  <input
                    type="date"
                    aria-label="Diary date"
                    value={date}
                    onChange={(event) =>
                      event.target.value && selectDate(event.target.value)
                    }
                  />
                </label>
                <button
                  className="icon-button"
                  onClick={() => changeDate(1)}
                  aria-label="Next day"
                >
                  <ChevronRight size={17} />
                </button>
              </div>
            </div>
          )}
          {error && <Notice onClose={() => setError("")}>{error}</Notice>}
          <div hidden={tab !== "today"}>
            <Today
              date={date}
              day={day}
              foods={foods}
              settings={settings}
              ai={ai}
              loading={loading}
              onProviderChange={chooseProvider}
              onSettings={() => setTab("settings")}
              onCatalogue={() => setTab("foods")}
              onRefresh={refresh}
              onDate={selectDate}
              onSaved={(next, id) => {
                if (next.date === selectedDate.current) setDay(next);
                setSaved({ id, date: next.date });
              }}
            />
          </div>
          {tab === "foods" && (
            <Catalogue
              foods={foods}
              ai={ai}
              onRefresh={refresh}
              onSettings={() => setTab("settings")}
            />
          )}
          {tab === "settings" && (
            <SettingsPage
              settings={settings}
              ai={ai}
              onSettings={setSettings}
              onAI={setAI}
              onRefresh={refresh}
            />
          )}
        </div>
      </main>
      {saved && (
        <div className="save-toast" role="status">
          <span className="toast-check">
            <Check size={17} />
          </span>
          <span>
            Added to your diary<small>{saved.date}</small>
          </span>
          <button onClick={() => void undo()}>
            <Undo2 size={16} />
            Undo
          </button>
          <button
            className="toast-dismiss"
            onClick={() => setSaved(null)}
            aria-label="Dismiss saved message"
          >
            ×
          </button>
        </div>
      )}
    </div>
  );
}
