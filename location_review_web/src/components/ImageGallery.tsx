import { useEffect, useState } from "react";
import { apiFetchBlob } from "../api/client";

interface ImageGalleryProps {
  images: string[];
}

interface LoadedImage {
  path: string;
  objectUrl: string | null;
  error: string | null;
}

export function ImageGallery({ images }: ImageGalleryProps) {
  const [loaded, setLoaded] = useState<LoadedImage[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // object URL不会被浏览器自动回收——images变化/组件卸载时必须手动
    // revoke，否则每次切换候选图都会泄漏一个blob(见Finding C1)。
    // `urls`只收集"在cancelled变true之前已经拿到手"的object URL；之后
    // 才resolve的fetch会在下面的catch分支里直接自行revoke，不进这个
    // 数组——避免cleanup先于fetch完成时漏掉一批没来得及revoke的URL。
    const urls: string[] = [];

    if (images.length === 0) {
      setLoaded([]);
      setLoading(false);
      return () => {
        cancelled = true;
      };
    }

    setLoading(true);
    setLoaded([]);

    Promise.all(
      images.map(async (path): Promise<LoadedImage | null> => {
        try {
          const objectUrl = await apiFetchBlob(
            `/location-review/frame?path=${encodeURIComponent(path)}`,
          );
          if (cancelled) {
            URL.revokeObjectURL(objectUrl);
            return null;
          }
          urls.push(objectUrl);
          return { path, objectUrl, error: null };
        } catch (err) {
          return {
            path,
            objectUrl: null,
            error: err instanceof Error ? err.message : "图片加载失败",
          };
        }
      }),
    ).then((results) => {
      if (!cancelled) {
        setLoaded(results.filter((result): result is LoadedImage => result !== null));
        setLoading(false);
      }
    });

    return () => {
      cancelled = true;
      for (const url of urls) {
        URL.revokeObjectURL(url);
      }
    };
    // 按内容(而不是数组引用)算依赖——调用方(ReviewDetailPage)每次渲染都会
    // new一个`[item.screenshot_paths[0]]`数组，引用必然不同，用images本身
    // 当依赖会导致父组件每次渲染都重新拉一遍图片。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [images.join("|")]);

  if (loading) {
    return <p>图片加载中...</p>;
  }

  return (
    <div style={{ display: "flex", gap: 8, overflowX: "auto" }}>
      {loaded.map((image) =>
        image.objectUrl ? (
          <img
            key={image.path}
            src={image.objectUrl}
            alt="复核截图"
            style={{ maxHeight: 240 }}
          />
        ) : (
          <p key={image.path} role="alert">
            {image.error ?? "图片加载失败"}
          </p>
        ),
      )}
    </div>
  );
}

export default ImageGallery;
