import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { ReviewDetailPage } from "./ReviewDetailPage";
import type { ReviewItem } from "../api/types";

const baseItem: ReviewItem = {
  id: 1,
  status: "pending",
  video_sha256: "a".repeat(64),
  candidate_location_id: null,
  screenshot_paths: ["/frames/x.jpg"],
  candidate: null,
  gps_distance_meters: null,
  video_filename: "x.mp4",
  video_city: "成都",
  video_collected_at: "2026-01-01T00:00:00Z",
  video_location_confirmed: false,
  visual_match_result: null,
  llm_verdict: null,
  human_decision: null,
};

const locations = [
  { id: 10, city: "成都", site: "武侯祠", representative_frame_path: "/frames/y.jpg", location_group_id: 5 },
];

const server = setupServer(
  http.get("*/location-review/locations", () => HttpResponse.json(locations)),
  http.post("*/location-review/queue/:id/decide", () => HttpResponse.json({ ok: true })),
  http.post("*/location-review/videos/:sha256/unlock", () => HttpResponse.json({ ok: true })),
);

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function renderPage(item: ReviewItem) {
  server.use(
    http.get("*/location-review/queue/:id", () => HttpResponse.json(item)),
  );
  return render(
    <MemoryRouter initialEntries={[`/queue/${item.id}`]}>
      <Routes>
        <Route path="/queue/:itemId" element={<ReviewDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

test("候选为null时显示新地点候选提示，不渲染null路径的img", async () => {
  renderPage(baseItem);

  expect(await screen.findByText("新地点候选，无参考图")).toBeInTheDocument();
  const images = screen.queryAllByRole("img");
  for (const img of images) {
    expect(img.getAttribute("src")).not.toBe("null");
    expect(img.getAttribute("src")).toBeTruthy();
  }
});

test("screenshot_paths为空时显示截图提取失败", async () => {
  renderPage({ ...baseItem, screenshot_paths: [] });

  expect(await screen.findByText("截图提取失败")).toBeInTheDocument();
});

test("video_location_confirmed为true时显示解锁按钮，否则不显示", async () => {
  renderPage({ ...baseItem, video_location_confirmed: true });

  expect(await screen.findByRole("button", { name: "解锁视频" })).toBeInTheDocument();
});

test("video_location_confirmed为false时不显示解锁按钮", async () => {
  renderPage({ ...baseItem, video_location_confirmed: false });

  await screen.findByText("新地点候选，无参考图");
  expect(screen.queryByRole("button", { name: "解锁视频" })).not.toBeInTheDocument();
});

test("确认同一地点需要先选target_location_id才能提交", async () => {
  const user = userEvent.setup();
  renderPage(baseItem);

  await screen.findByText("新地点候选，无参考图");
  const confirmButton = screen.getByRole("button", { name: "确认同一地点" });
  await user.click(confirmButton);

  expect(await screen.findByRole("alert")).toHaveTextContent("请先选择目标地点");
});

test("409冲突时显示后端返回的错误信息", async () => {
  server.use(
    http.post("*/location-review/queue/:id/decide", () =>
      HttpResponse.json({ detail: "视频已被锁定" }, { status: 409 }),
    ),
  );
  const user = userEvent.setup();
  renderPage(baseItem);

  await screen.findByText("新地点候选，无参考图");
  const differentButton = screen.getByRole("button", { name: "不是这个候选" });
  await user.click(differentButton);

  expect(await screen.findByRole("alert")).toHaveTextContent(/视频已被锁定/);
});
