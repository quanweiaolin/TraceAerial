import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test } from "vitest";
import { AuthProvider, useAuth } from "./AuthContext";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

test("login存token到localStorage，logout清空", () => {
  const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider });
  act(() => result.current.login("abc123"));
  expect(localStorage.getItem("location_review_token")).toBe("abc123");
  act(() => result.current.logout());
  expect(localStorage.getItem("location_review_token")).toBeNull();
});
