import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { MemoryRouter } from "react-router-dom";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { NamingAuditPage } from "./NamingAuditPage";
import type { NamingAlert } from "../api/types";

let currentAlerts: NamingAlert[] = [];

const server = setupServer(
  http.get("*/location-review/naming-alerts", () => HttpResponse.json(currentAlerts)),
);

beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  currentAlerts = [];
});
afterAll(() => server.close());

function driftAlert(): NamingAlert {
  return {
    id: 1,
    alert_type: "name_drift",
    city: "dongguan",
    location_ids: [22],
    // 后端会带上这个地点当前的city/site——注意site带着扫描器加的消歧后缀
    locations: [{ id: 22, city: "dongguan", site: "guanfogaosuchuzadao_1" }],
    raw_site_votes: { guanfogaosuchuzadao: 48, guanfugaosuchuzadao: 2 },
    suggested_value: "guanfogaosuchuzadao",
    status: "pending",
    created_at: "2026-11-27T00:00:00Z",
  };
}

function mergeAlert(): NamingAlert {
  return {
    id: 2,
    alert_type: "merge_candidate",
    city: "dongguan",
    location_ids: [22, 23],
    locations: [
      { id: 22, city: "dongguan", site: "guanfogaosuchuzadao_1" },
      { id: 23, city: "dongguan", site: "guanfogaosuchuzadao_2" },
    ],
    raw_site_votes: { guanfogaosuchuzadao: { "22": 50, "23": 1 } },
    suggested_value: "guanfogaosuchuzadao",
    status: "pending",
    created_at: "2026-11-27T00:00:00Z",
  };
}

test("展示命名漂移提示，确认后从列表消失", async () => {
  currentAlerts = [driftAlert()];
  let capturedBody: unknown = null;
  server.use(
    http.post("*/location-review/naming-alerts/1/confirm", async ({ request }) => {
      capturedBody = await request.json();
      currentAlerts = [];
      return HttpResponse.json({ id: 1, status: "confirmed" });
    }),
  );

  render(
    <MemoryRouter>
      <NamingAuditPage />
    </MemoryRouter>,
  );

  expect(await screen.findByText(/guanfogaosuchuzadao：48票/)).toBeInTheDocument();
  expect(screen.getByText(/guanfugaosuchuzadao：2票/)).toBeInTheDocument();
  // 涉及地点要显示当前的city/site，不只是裸id
  expect(screen.getByText(/#22\s+dongguan \/ guanfogaosuchuzadao_1/)).toBeInTheDocument();

  const input = screen.getByRole("textbox");
  // 预填的是这个地点当前的site(带_1后缀)，不是裸的suggested_value
  expect(input).toHaveValue("guanfogaosuchuzadao_1");

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "确认" }));

  await waitFor(() => {
    expect(capturedBody).toEqual({ chosen_value: "guanfogaosuchuzadao_1" });
  });

  await waitFor(() => {
    expect(screen.queryByText(/guanfogaosuchuzadao：48票/)).not.toBeInTheDocument();
  });
});

test("命名漂移提示可以点忽略(误报)，不改写地点名称", async () => {
  currentAlerts = [driftAlert()];
  let dismissCalled = false;
  server.use(
    http.post("*/location-review/naming-alerts/1/dismiss", () => {
      dismissCalled = true;
      currentAlerts = [];
      return HttpResponse.json({ id: 1, status: "dismissed" });
    }),
  );

  render(
    <MemoryRouter>
      <NamingAuditPage />
    </MemoryRouter>,
  );

  expect(await screen.findByText(/guanfogaosuchuzadao：48票/)).toBeInTheDocument();

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "忽略(误报)" }));

  await waitFor(() => {
    expect(dismissCalled).toBe(true);
  });

  await waitFor(() => {
    expect(screen.queryByText(/guanfogaosuchuzadao：48票/)).not.toBeInTheDocument();
  });
});

test("展示合并候选提示，点忽略后从列表消失", async () => {
  currentAlerts = [mergeAlert()];
  let dismissCalled = false;
  server.use(
    http.post("*/location-review/naming-alerts/2/dismiss", () => {
      dismissCalled = true;
      currentAlerts = [];
      return HttpResponse.json({ id: 2, status: "dismissed" });
    }),
  );

  render(
    <MemoryRouter>
      <NamingAuditPage />
    </MemoryRouter>,
  );

  // 两个地点各自当前的city/site都要展示出来，人才判断得了是不是同一个地方
  expect(
    await screen.findByText(/#22\s+dongguan \/ guanfogaosuchuzadao_1/),
  ).toBeInTheDocument();
  expect(screen.getByText(/#23\s+dongguan \/ guanfogaosuchuzadao_2/)).toBeInTheDocument();
  expect(screen.getByText(/推荐名称：guanfogaosuchuzadao/)).toBeInTheDocument();

  const link = screen.getByRole("link", { name: /去Groups页面合并/ });
  expect(link).toHaveAttribute("href", "/groups");

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "忽略(误报)" }));

  await waitFor(() => {
    expect(dismissCalled).toBe(true);
  });

  await waitFor(() => {
    expect(screen.queryByText(/去Groups页面合并/)).not.toBeInTheDocument();
  });
});

test("合并候选提示可以标记为已处理(走confirm，不是忽略)", async () => {
  currentAlerts = [mergeAlert()];
  let confirmCalled = false;
  server.use(
    http.post("*/location-review/naming-alerts/2/confirm", () => {
      confirmCalled = true;
      currentAlerts = [];
      return HttpResponse.json({ id: 2, status: "confirmed" });
    }),
  );

  render(
    <MemoryRouter>
      <NamingAuditPage />
    </MemoryRouter>,
  );

  expect(await screen.findByText(/去Groups页面合并/)).toBeInTheDocument();

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "已处理(已去Groups合并)" }));

  await waitFor(() => {
    expect(confirmCalled).toBe(true);
  });

  await waitFor(() => {
    expect(screen.queryByText(/去Groups页面合并/)).not.toBeInTheDocument();
  });
});

test("没有待处理提示时显示空态", async () => {
  currentAlerts = [];

  render(
    <MemoryRouter>
      <NamingAuditPage />
    </MemoryRouter>,
  );

  expect(await screen.findByText("暂无待处理的命名提示")).toBeInTheDocument();
});
