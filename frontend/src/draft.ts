import type { LoggingDraft } from "./types";

let database: Promise<IDBDatabase> | undefined;
let queue: Promise<unknown> = Promise.resolve();

function openDatabase(): Promise<IDBDatabase> {
  database ??= new Promise((resolve, reject) => {
    const request = indexedDB.open("kcallm-drafts", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("drafts");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(
        new Error(
          "This browser could not open draft storage. Keep this page open until you save.",
        ),
      );
  });
  return database;
}

export async function loadStoredDraft<T>(key: string): Promise<T | undefined> {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const request = db
      .transaction("drafts", "readonly")
      .objectStore("drafts")
      .get(key);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export function saveStoredDraft(key: string, draft: unknown): Promise<void> {
  const write = async () => {
    const db = await openDatabase();
    await new Promise<void>((resolve, reject) => {
      const transaction = db.transaction("drafts", "readwrite");
      transaction.objectStore("drafts").put(draft, key);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () =>
        reject(
          new Error(
            "Your browser could not preserve the draft. Free some storage and retry.",
          ),
        );
      transaction.onabort = () =>
        reject(new Error("Draft storage was interrupted. Please retry."));
    });
  };
  const result = queue.then(write, write);
  queue = result.catch(() => undefined);
  return result;
}

export const loadDraft = (): Promise<LoggingDraft | undefined> =>
  loadStoredDraft<LoggingDraft>("capture");
export const saveDraft = (draft: LoggingDraft): Promise<void> =>
  saveStoredDraft("capture", draft);
