import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "vitest";
import { TOKEN_STORAGE_KEY } from "../auth/AuthContext";
import { apiFetch } from "./client";

const server = setupServer(
  http.get("/location-review/queue", () => {
    return new HttpResponse(null, { status: 401 });
  }),
);

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(TOKEN_STORAGE_KEY, "sometoken");
});

test("401响应后清空token", async () => {
  await expect(apiFetch("/location-review/queue")).rejects.toThrow();
  expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
});
