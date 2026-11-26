import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { render, screen, waitFor } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, expect, test, vi } from "vitest";
import { ImageGallery } from "./ImageGallery";

const server = setupServer();

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

test("加载中先显示loading，拿到字节后渲染img(不是原始服务器路径)", async () => {
  server.use(
    http.get("*/location-review/frame", () =>
      new HttpResponse("fake-jpeg-bytes", { headers: { "Content-Type": "image/jpeg" } }),
    ),
  );

  render(<ImageGallery images={["location_review_frames/abc_video.jpg"]} />);

  expect(screen.getByText("图片加载中...")).toBeInTheDocument();

  const img = await screen.findByRole("img", {}, { timeout: 3000 });
  expect(img.getAttribute("src")).not.toBe("location_review_frames/abc_video.jpg");
  expect(img.getAttribute("src")).toMatch(/^blob:/);
});

test("单张图片拉取失败时显示错误提示，不渲染破损img", async () => {
  server.use(
    http.get("*/location-review/frame", () => HttpResponse.json({ detail: "文件不存在" }, { status: 404 })),
  );

  render(<ImageGallery images={["location_review_frames/missing.jpg"]} />);

  expect(await screen.findByRole("alert")).toHaveTextContent("文件不存在");
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

test("images为空数组时不渲染任何图片也不请求", async () => {
  render(<ImageGallery images={[]} />);

  await waitFor(() => {
    expect(screen.queryByText("图片加载中...")).not.toBeInTheDocument();
  });
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

test("卸载时revoke已创建的object URL，避免内存泄漏", async () => {
  server.use(
    http.get("*/location-review/frame", () =>
      new HttpResponse("fake-jpeg-bytes", { headers: { "Content-Type": "image/jpeg" } }),
    ),
  );
  const revokeSpy = vi.spyOn(URL, "revokeObjectURL");

  const { unmount } = render(<ImageGallery images={["location_review_frames/abc_video.jpg"]} />);
  await screen.findByRole("img", {}, { timeout: 3000 });

  unmount();

  expect(revokeSpy).toHaveBeenCalled();
  revokeSpy.mockRestore();
});
