import { AlertCircle, Check, LoaderCircle, X } from "lucide-react";
import {
  useEffect,
  useRef,
  type ClipboardEventHandler,
  type ReactNode,
} from "react";
import {
  nutrientKeys,
  nutrientLabels,
  numberText,
  type Nutrients,
} from "../types";

export function Notice({
  children,
  kind = "error",
  onClose,
}: {
  children: ReactNode;
  kind?: "error" | "success" | "info";
  onClose?: () => void;
}) {
  return (
    <div
      className={`notice notice-${kind}`}
      role={kind === "error" ? "alert" : "status"}
    >
      {kind === "success" ? <Check size={17} /> : <AlertCircle size={17} />}
      <div>{children}</div>
      {onClose && (
        <button className="icon-button" onClick={onClose} aria-label="Dismiss">
          <X size={15} />
        </button>
      )}
    </div>
  );
}

export function Busy({ children = "Working…" }: { children?: ReactNode }) {
  return (
    <>
      <LoaderCircle className="spin" size={16} />
      <span>{children}</span>
    </>
  );
}

export function NutrientGrid({
  nutrients,
  incomplete = [],
  compact = false,
}: {
  nutrients: Nutrients;
  incomplete?: string[];
  compact?: boolean;
}) {
  return (
    <dl className={`nutrient-grid ${compact ? "compact" : ""}`}>
      {nutrientKeys.map((key) => (
        <div key={key}>
          <dt>{nutrientLabels[key]}</dt>
          <dd title={nutrients[key] ?? "Unknown"}>
            {numberText(nutrients[key])}
            <span>{key === "kcal" ? "kcal" : "g"}</span>
            {incomplete.includes(key) && (
              <small title="At least one food has no value for this nutrient">
                *
              </small>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function Modal({
  title,
  children,
  onClose,
  wide = false,
  onPaste,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
  onPaste?: ClipboardEventHandler<HTMLDialogElement>;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      className={`modal ${wide ? "modal-wide" : ""}`}
      onPaste={onPaste}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === ref.current) onClose();
      }}
    >
      <div className="modal-heading">
        <h2>{title}</h2>
        <button
          className="icon-button"
          onClick={onClose}
          aria-label="Close dialog"
        >
          <X />
        </button>
      </div>
      {children}
    </dialog>
  );
}

export function SectionHeading({
  eyebrow,
  title,
  children,
}: {
  eyebrow?: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="section-heading">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h2>{title}</h2>
      </div>
      {children}
    </div>
  );
}
