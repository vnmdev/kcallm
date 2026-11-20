/** UUID v4 also works when HTTP prevents the native randomUUID helper from being exposed. */
export function createUUID(): string {
  const secureCrypto = globalThis.crypto;
  if (typeof secureCrypto?.randomUUID === "function") {
    return secureCrypto.randomUUID();
  }
  if (typeof secureCrypto?.getRandomValues !== "function") {
    throw new Error(
      "This browser cannot create secure request IDs. Use an up-to-date browser.",
    );
  }

  const bytes = secureCrypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
