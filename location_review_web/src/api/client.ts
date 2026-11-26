import { TOKEN_STORAGE_KEY } from "../auth/AuthContext";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

export async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = localStorage.getItem(TOKEN_STORAGE_KEY);

  const headers = new Headers(options.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
  });

  if (response.status === 401) {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
    if (typeof window !== "undefined") {
      window.location.href = "/login";
    }
    throw new ApiError("未授权，请重新登录", 401);
  }

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    // 后端FastAPI的HTTPException统一返回{"detail": "..."}这个JSON形状——
    // 优先把detail提出来当错误信息，不是把整段JSON原文丢给用户看
    // (比如409冲突时应该显示"视频已被锁定"而不是'{"detail":"视频已被锁定"}')。
    // 不是JSON/没有detail字段时退回原始文本。
    let message = text;
    try {
      const parsed = JSON.parse(text) as { detail?: unknown };
      if (typeof parsed.detail === "string" && parsed.detail) {
        message = parsed.detail;
      }
    } catch {
      // 不是JSON，用原始文本
    }
    throw new ApiError(message || `请求失败: ${response.status}`, response.status);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}
