import { useRef } from "react";
import { Camera, Images } from "lucide-react";

/** Separate inputs let phones choose existing images without forcing the camera. */
export function ImagePicker({
  onImage,
  disabled = false,
}: {
  onImage: (file: File) => void;
  disabled?: boolean;
}) {
  const camera = useRef<HTMLInputElement>(null);
  const gallery = useRef<HTMLInputElement>(null);
  return (
    <div className="image-picker">
      <input
        ref={camera}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        capture="environment"
        aria-label="Take a photo"
        className="visually-hidden"
        disabled={disabled}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onImage(file);
          e.target.value = "";
        }}
      />
      <input
        ref={gallery}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        aria-label="Choose from gallery"
        className="visually-hidden"
        disabled={disabled}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onImage(file);
          e.target.value = "";
        }}
      />
      <button
        type="button"
        className="button button-secondary"
        disabled={disabled}
        onClick={() => camera.current?.click()}
      >
        <Camera size={18} />
        Camera
      </button>
      <button
        type="button"
        className="button button-secondary"
        disabled={disabled}
        onClick={() => gallery.current?.click()}
      >
        <Images size={18} />
        Gallery
      </button>
    </div>
  );
}
