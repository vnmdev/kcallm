export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(message: string, status: number, code = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function detailMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail
      .map((item) => {
        const field = item.loc
          ?.filter((part: string) => part !== "body")
          .join(" · ");
        return `${field ? `${field}: ` : ""}${item.msg ?? "Invalid value"}`;
      })
      .join(". ");
  if (detail && typeof detail === "object" && "message" in detail)
    return String(detail.message);
  return "The request could not be completed.";
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers,
      },
    });
  } catch {
    throw new ApiError(
      "The backend could not be reached. Your draft is still here; reconnect and try again.",
      0,
    );
  }
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(
      data?.error?.message ?? detailMessage(data?.detail),
      response.status,
      data?.error?.code ?? "",
    );
  }
  return data as T;
}

export function post<T>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: "POST", body: JSON.stringify(body) });
}
export function put<T>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: "PUT", body: JSON.stringify(body) });
}
export function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";
}

export async function readImage(file: File): Promise<string> {
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
    throw new Error(
      "Choose a JPEG, PNG or WebP photo. Convert HEIC images before uploading.",
    );
  }
  if (file.size > 10 * 1024 * 1024)
    throw new Error("This photo is larger than 10 MB. Choose a smaller image.");
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () =>
      reject(new Error("The photo could not be read. Please try again."));
    reader.readAsDataURL(file);
  });
}
