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

test("展示命名漂移提示，确认后从列表消失", async () => {
  currentAlerts = [
    {
      id: 1,
      alert_type: "name_drift",
      city: "dongguan",
      location_ids: [22],
      raw_site_votes: { guanfogaosuchuzadao: 48, guanfugaosuchuzadao: 2 },
      suggested_value: "guanfogaosuchuzadao",
      status: "pending",
      created_at: "2026-11-27T00:00:00Z",
    },
  ];
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

  expect(await screen.findByText(/guanfogaosuchuzadao/)).toBeInTheDocument();
  expect(screen.getByText(/48/)).toBeInTheDocument();
  expect(screen.getByText(/guanfugaosuchuzadao/)).toBeInTheDocument();
  expect(screen.getByText(/2/)).toBeInTheDocument();

  const input = screen.getByRole("textbox");
  expect(input).toHaveValue("guanfogaosuchuzadao");

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "确认" }));

  await waitFor(() => {
    expect(capturedBody).toEqual({ chosen_value: "guanfogaosuchuzadao" });
  });

  await waitFor(() => {
    expect(screen.queryByText(/guanfogaosuchuzadao/)).not.toBeInTheDocument();
  });
});

test("展示合并候选提示，点忽略后从列表消失", async () => {
  currentAlerts = [
    {
      id: 2,
      alert_type: "merge_candidate",
      city: "dongguan",
      location_ids: [22, 23],
      raw_site_votes: { guanfogaosuchuzadao: { "22": 50, "23": 1 } },
      suggested_value: "guanfogaosuchuzadao",
      status: "pending",
      created_at: "2026-11-27T00:00:00Z",
    },
  ];
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

  expect(await screen.findByText(/22/)).toBeInTheDocument();
  expect(screen.getByText(/23/)).toBeInTheDocument();
  expect(screen.getByText(/guanfogaosuchuzadao/)).toBeInTheDocument();

  const link = screen.getByRole("link", { name: /去Groups页面合并/ });
  expect(link).toHaveAttribute("href", "/groups");

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "忽略" }));

  await waitFor(() => {
    expect(dismissCalled).toBe(true);
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
