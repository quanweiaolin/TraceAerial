import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { MemoryRouter } from "react-router-dom";
import { render, screen } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { AgreementRatePage } from "./AgreementRatePage";
import type { AgreementRate } from "../api/types";

let currentRate: AgreementRate = {
  total: 0,
  agreed: 0,
  disagreed: 0,
  ready_for_automation: false,
};

const server = setupServer(
  http.get("*/location-review/agreement-rate", () => HttpResponse.json(currentRate)),
);

beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
});
afterAll(() => server.close());

test("加载后显示total/agreed/disagreed四个数字", async () => {
  currentRate = { total: 100, agreed: 90, disagreed: 10, ready_for_automation: true };
  render(
    <MemoryRouter>
      <AgreementRatePage />
    </MemoryRouter>,
  );

  expect(await screen.findByText("100")).toBeInTheDocument();
  expect(await screen.findByText("90")).toBeInTheDocument();
  expect(await screen.findByText("10")).toBeInTheDocument();
});

test("ready_for_automation=false时显示明显的'未达标'提示", async () => {
  currentRate = { total: 50, agreed: 20, disagreed: 30, ready_for_automation: false };
  render(
    <MemoryRouter>
      <AgreementRatePage />
    </MemoryRouter>,
  );

  expect(await screen.findByText(/未达标/)).toBeInTheDocument();
});

test("ready_for_automation=true时不显示'未达标'提示", async () => {
  currentRate = { total: 50, agreed: 48, disagreed: 2, ready_for_automation: true };
  render(
    <MemoryRouter>
      <AgreementRatePage />
    </MemoryRouter>,
  );

  expect(await screen.findByText("50")).toBeInTheDocument();
  expect(screen.queryByText(/未达标/)).not.toBeInTheDocument();
});

test("接口报错时显示错误信息", async () => {
  server.use(
    http.get("*/location-review/agreement-rate", () =>
      HttpResponse.json({ detail: "服务器错误" }, { status: 500 }),
    ),
  );

  render(
    <MemoryRouter>
      <AgreementRatePage />
    </MemoryRouter>,
  );

  expect(await screen.findByText("服务器错误")).toBeInTheDocument();
});
