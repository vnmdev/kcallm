/** Read only the data supplied by a user-initiated paste event. No clipboard permission is needed. */
export function pastedImage(clipboard: DataTransfer): File | undefined {
  for (const item of Array.from(clipboard.items)) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      const image = item.getAsFile();
      if (image) return image;
    }
  }
  return Array.from(clipboard.files).find((file) =>
    file.type.startsWith("image/"),
  );
}
