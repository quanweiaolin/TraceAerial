import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { MemoryRouter } from "react-router-dom";
import { render, screen } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { QueuePage } from "./QueuePage";
import type { ReviewItem } from "../api/types";

const baseItem: ReviewItem = {
  id: 1,
  status: "pending",
  screenshot_paths: [],
  candidate: null,
  gps_distance_meters: null,
  video_filename: "x.mp4",
  video_city: "成都",
  video_collected_at: "2026-01-01T00:00:00Z",
  video_location_confirmed: true,
  visual_match_result: null,
  llm_verdict: null,
  human_decision: null,
} as unknown as ReviewItem;

const server = setupServer(
  http.get("*/location-review/queue", () =>
    HttpResponse.json([
      { ...baseItem, id: 1, video_filename: "x.mp4", video_location_confirmed: true },
      { ...baseItem, id: 2, video_filename: "y.mp4", video_location_confirmed: false },
    ]),
  ),
);

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

test("已锁定视频的陪审item显示灰显标记", async () => {
  render(
    <MemoryRouter>
      <QueuePage />
    </MemoryRouter>,
  );
  expect(await screen.findByText(/已通过其它条目处理/)).toBeInTheDocument();
});
