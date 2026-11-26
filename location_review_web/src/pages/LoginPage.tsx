import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [tokenInput, setTokenInput] = useState("");

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = tokenInput.trim();
    if (!trimmed) {
      return;
    }
    login(trimmed);
    navigate("/", { replace: true });
  }

  return (
    <div style={{ display: "flex", justifyContent: "center", marginTop: "10vh" }}>
      <form onSubmit={handleSubmit} style={{ width: 320 }}>
        <h1>地址复核登录</h1>
        <p>请输入通过后端 CLI 脚本获取的访问令牌。</p>
        <label htmlFor="token-input">访问令牌</label>
        <input
          id="token-input"
          type="password"
          value={tokenInput}
          onChange={(event) => setTokenInput(event.target.value)}
          placeholder="粘贴 token"
          style={{ width: "100%", display: "block", marginTop: 4, marginBottom: 12 }}
        />
        <button type="submit">登录</button>
      </form>
    </div>
  );
}

export default LoginPage;
