interface ImageGalleryProps {
  images: string[];
}

export function ImageGallery({ images }: ImageGalleryProps) {
  return (
    <div style={{ display: "flex", gap: 8, overflowX: "auto" }}>
      {images.map((src) => (
        <img key={src} src={src} alt="" style={{ maxHeight: 240 }} />
      ))}
    </div>
  );
}

export default ImageGallery;
