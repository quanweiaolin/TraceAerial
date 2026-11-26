import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { MemoryRouter } from "react-router-dom";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { FolderAuditPage } from "./FolderAuditPage";
import type { FolderAuditVideo } from "../api/types";

const unpickedVideo: FolderAuditVideo = {
  video_sha256: "a".repeat(64),
  storage_path: "/data/videos/a.mp4",
  location_id: null,
  is_stable: null,
  collect_lon: null,
  collect_lat: null,
};

const pickedVideo: FolderAuditVideo = {
  video_sha256: "b".repeat(64),
  storage_path: "/data/videos/b.mp4",
  location_id: 7,
  is_stable: true,
  collect_lon: 104.0,
  collect_lat: 30.0,
};

let currentResponse: FolderAuditVideo[] = [];

const server = setupServer(
  http.get("*/location-review/folder-audit", () => HttpResponse.json(currentResponse)),
  http.post("*/location-review/queue/generate", () => HttpResponse.json({ created: true })),
);

beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  currentResponse = [];
});
afterAll(() => server.close());

async function submitPrefix(prefix: string) {
  const user = userEvent.setup();
  const input = screen.getByLabelText("存储路径前缀：");
  await user.type(input, prefix);
  await user.click(screen.getByRole("button", { name: "查询" }));
}

test("空结果时显示'这个前缀下没有视频'", async () => {
  currentResponse = [];
  render(
    <MemoryRouter>
      <FolderAuditPage />
    </MemoryRouter>,
  );
  await submitPrefix("/data/videos");
  expect(await screen.findByText("这个前缀下没有视频")).toBeInTheDocument();
});

test("未被地点收录的视频跟已收录的视频有视觉区分", async () => {
  currentResponse = [unpickedVideo, pickedVideo];
  render(
    <MemoryRouter>
      <FolderAuditPage />
    </MemoryRouter>,
  );
  await submitPrefix("/data/videos");

  const unpickedRow = await screen.findByTestId(`folder-audit-row-${unpickedVideo.video_sha256}`);
  const pickedRow = await screen.findByTestId(`folder-audit-row-${pickedVideo.video_sha256}`);

  expect(unpickedRow.getAttribute("data-unpicked")).toBe("true");
  expect(pickedRow.getAttribute("data-unpicked")).toBe("false");
});

test("点击生成候选调用POST /location-review/queue/generate并带上video_path", async () => {
  currentResponse = [unpickedVideo];
  let capturedBody: unknown = null;
  server.use(
    http.post("*/location-review/queue/generate", async ({ request }) => {
      capturedBody = await request.json();
      return HttpResponse.json({ created: true });
    }),
  );

  render(
    <MemoryRouter>
      <FolderAuditPage />
    </MemoryRouter>,
  );
  await submitPrefix("/data/videos");

  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "生成候选" }));

  await waitFor(() => {
    expect(capturedBody).toEqual({
      video_sha256: unpickedVideo.video_sha256,
      video_path: unpickedVideo.storage_path,
    });
  });
});
